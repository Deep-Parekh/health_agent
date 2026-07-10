"""Per-user memory for HealthVA.

Storage backend is chosen by environment:
- DATABASE_URL set (Supabase Postgres connection string) -> Postgres via psycopg.
  HF Spaces free tier has an ephemeral filesystem, so this is the deployed path.
- otherwise -> local SQLite (data/users.db) for development.

Profiles are stored as a JSON document per username, restricted to a whitelist
of health-relevant fields — the agent cannot invent new fields, and the store
never accepts names, emails, or other identifiers (input guardrails in
common.py block those upstream too).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Optional, Union

from pydantic import BaseModel, Field, PrivateAttr
from langchain.tools import BaseTool

from healthva.common import USERS_DB_PATH, logger

# The only fields a profile may contain. Everything else is rejected.
PROFILE_FIELDS = {
    "age": int,
    "sex": str,                    # male / female
    "height_cm": float,
    "weight_kg": float,
    "activity_level": str,         # sedentary / light / moderate / very_active / extra_active
    "goal": str,                   # lose_weight / maintain_weight / gain_weight
    "dietary_restrictions": list,  # e.g. ["vegetarian", "dairy_free"]
    "allergies": list,
    "injuries": list,              # user's words; workout tools classify them
    "equipment": list,             # e.g. ["dumbbell", "machine"]
    "days_per_week": int,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    profile    TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

# Postgres row-level security: every scoped query runs inside a transaction
# that (1) drops to a dedicated application role and (2) sets app.current_user
# to the session's user. Rows of any other user are then invisible and
# unwritable AT THE DATABASE, so a code bug or SQL injection in the app cannot
# cross users.
#
# The dedicated role exists because Supabase's `postgres` login role has
# BYPASSRLS — policies (even FORCEd) never apply to it. `SET LOCAL ROLE` is
# transaction-local, so pooled connections can't leak the role or identity,
# and the role is NOLOGIN, so no new password/secret is involved. The admin
# connection itself keeps full access (needed for schema setup) — the fence
# applies to the application data path, which always goes through UserScope.
# Preferred: our own NOLOGIN role (vanilla Postgres). Supabase blocks granting
# membership options on the `postgres` login role (the GRANT aborts server-side),
# so there we fall back to the built-in `authenticated` role, which postgres can
# SET ROLE into and which has no BYPASSRLS. Detection happens at startup.
APP_ROLE_CANDIDATES = ("health_agent_app", "authenticated")

_RLS_SETUP = [
    """
    DO $$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'health_agent_app') THEN
            CREATE ROLE health_agent_app NOLOGIN;
        END IF;
    END $$
    """,
    "GRANT USAGE ON SCHEMA public TO health_agent_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON users TO health_agent_app",
    # Supabase's built-in RLS-bound role, if this is a Supabase database
    """
    DO $$ BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
            GRANT USAGE ON SCHEMA public TO authenticated;
            GRANT SELECT, INSERT, UPDATE, DELETE ON public.users TO authenticated;
        END IF;
    END $$
    """,
    "ALTER TABLE users ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE users FORCE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS user_isolation ON users",
    """
    CREATE POLICY user_isolation ON users
        USING (username = current_setting('app.current_user', true))
        WITH CHECK (username = current_setting('app.current_user', true))
    """,
]


class UserStore:
    """Key-value profile store: one JSON profile per username.

    All reads/writes go through a UserScope (`store.scope(username)`) — the
    scope API has no username parameters, so application code cannot address
    another user's row, and on Postgres RLS enforces the same boundary in the
    database itself.
    """

    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or os.getenv("DATABASE_URL")
        self.backend = "postgres" if self.database_url else "sqlite"
        if self.backend == "sqlite":
            logger.info("UserStore: DATABASE_URL not set, using local SQLite at %s", USERS_DB_PATH)
        else:
            logger.info("UserStore: using Postgres")
        self._init_schema()

    def _connect(self):
        if self.backend == "postgres":
            import psycopg

            return psycopg.connect(self.database_url)
        return sqlite3.connect(USERS_DB_PATH)

    @property
    def _ph(self) -> str:
        return "%s" if self.backend == "postgres" else "?"

    def _init_schema(self) -> None:
        self.app_role: Optional[str] = None
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            if self.backend == "postgres":
                for stmt in _RLS_SETUP:
                    conn.execute(stmt)
            conn.commit()
        if self.backend == "postgres":
            self._grant_membership_best_effort()
            self.app_role = self._detect_app_role()
            if self.app_role:
                logger.info("UserStore: row-level security active via role %r", self.app_role)
            else:
                logger.warning(
                    "UserStore: no assumable RLS role — cross-user isolation is "
                    "enforced at the application layer only"
                )

    def _grant_membership_best_effort(self) -> None:
        """Vanilla Postgres: let the connecting role SET into health_agent_app.
        On Supabase this GRANT is blocked (the server aborts the connection);
        we tolerate that and fall back to the `authenticated` role instead."""
        try:
            with self._connect() as conn:
                conn.execute("GRANT health_agent_app TO CURRENT_USER WITH SET TRUE")
                conn.commit()
        except Exception as exc:
            logger.info("UserStore: membership grant unavailable (%s)", type(exc).__name__)

    def _detect_app_role(self) -> Optional[str]:
        for candidate in APP_ROLE_CANDIDATES:
            try:
                with self._connect() as conn:
                    conn.execute(f"SET LOCAL ROLE {candidate}")
                    conn.rollback()
                return candidate
            except Exception:
                continue
        return None

    @staticmethod
    def normalize_username(username: str) -> str:
        username = (username or "").strip().lower()
        if not username:
            raise ValueError("username must not be empty")
        return username

    def scope(self, username: str) -> "UserScope":
        """The only way to read or write profiles — bound to one user."""
        return UserScope(self, self.normalize_username(username))


class UserScope:
    """All operations of one session, fenced to one username.

    On Postgres, every transaction first sets `app.current_user` (transaction-
    local) so the RLS policy applies; on SQLite the explicit WHERE clause plus
    this class's lack of any username parameter provides the same guarantee
    structurally.
    """

    def __init__(self, store: UserStore, username: str):
        self._store = store
        self.username = username

    def _execute(self, conn, sql: str, params: tuple = ()):
        if self._store.backend == "postgres" and self._store.app_role:
            # Both are transaction-local: they evaporate at commit/rollback, so
            # pooled connections can't leak one user's identity into another's
            # query. The app role has no BYPASSRLS, so the policy binds.
            conn.execute(f"SET LOCAL ROLE {self._store.app_role}")
            conn.execute(
                "SELECT set_config('app.current_user', %s, true)", (self.username,)
            )
        return conn.execute(sql, params)

    def get_profile(self) -> Dict[str, Any]:
        ph = self._store._ph
        with self._store._connect() as conn:
            cur = self._execute(
                conn, f"SELECT profile FROM users WHERE username = {ph}", (self.username,)
            )
            row = cur.fetchone()
        return json.loads(row[0]) if row else {}

    def update_profile(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Merge validated updates into the stored profile and return it."""
        clean = validate_profile_updates(updates)
        profile = self.get_profile()
        profile.update(clean)
        now = datetime.now(timezone.utc).isoformat()
        ph = self._store._ph
        with self._store._connect() as conn:
            self._execute(
                conn,
                f"""
                INSERT INTO users (username, profile, updated_at)
                VALUES ({ph}, {ph}, {ph})
                ON CONFLICT (username) DO UPDATE
                SET profile = EXCLUDED.profile, updated_at = EXCLUDED.updated_at
                """,
                (self.username, json.dumps(profile), now),
            )
            conn.commit()
        return profile

    def delete_profile(self) -> None:
        ph = self._store._ph
        with self._store._connect() as conn:
            self._execute(
                conn, f"DELETE FROM users WHERE username = {ph}", (self.username,)
            )
            conn.commit()


def validate_profile_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist + type-coerce profile updates; reject anything else."""
    clean: Dict[str, Any] = {}
    for key, value in updates.items():
        if value is None:
            continue
        if key not in PROFILE_FIELDS:
            raise ValueError(
                f"'{key}' is not a valid profile field. Valid fields: {', '.join(PROFILE_FIELDS)}"
            )
        # Empty lists are treated as "no change", not "clear": models sometimes
        # pass [] for fields the user never mentioned, and silently wiping
        # injuries or allergies is the dangerous direction.
        if PROFILE_FIELDS[key] is list and not value:
            continue
        expected = PROFILE_FIELDS[key]
        try:
            if expected is list:
                if isinstance(value, str):
                    value = [v.strip() for v in value.split(",") if v.strip()]
                clean[key] = [str(v).strip().lower() for v in value]
            else:
                clean[key] = expected(value)
        except (ValueError, TypeError):
            raise ValueError(f"'{key}' must be of type {expected.__name__}")
    return clean


def format_profile(profile: Dict[str, Any]) -> str:
    if not profile:
        return "No profile stored yet for this user."
    lines = [f"- {k}: {v}" for k, v in sorted(profile.items())]
    return "Stored profile:\n" + "\n".join(lines)


# --- LangChain tools, bound to a (store, username) pair per session ---

class GetProfileArgs(BaseModel):
    pass


class UpdateProfileArgs(BaseModel):
    age: Optional[int] = Field(None, description="Age in years")
    sex: Optional[str] = Field(None, description="male or female")
    height_cm: Optional[float] = Field(None, description="Height in centimeters")
    weight_kg: Optional[float] = Field(None, description="Weight in kilograms")
    activity_level: Optional[str] = Field(
        None, description="sedentary, light, moderate, very_active, or extra_active"
    )
    goal: Optional[str] = Field(None, description="lose_weight, maintain_weight, or gain_weight")
    # List fields also accept a comma-separated string — small models often
    # pass "torn acl" instead of ["torn acl"], and rejecting that loses data.
    dietary_restrictions: Optional[Union[str, List[str]]] = Field(
        None, description="e.g. ['vegetarian', 'dairy_free'] — replaces the stored list"
    )
    allergies: Optional[Union[str, List[str]]] = Field(
        None, description="Food allergies — replaces the stored list"
    )
    injuries: Optional[Union[str, List[str]]] = Field(
        None, description="Current injuries in the user's words, e.g. ['torn acl'] — replaces the stored list"
    )
    equipment: Optional[Union[str, List[str]]] = Field(
        None, description="Workout equipment the user owns, e.g. ['dumbbell'] — replaces the stored list"
    )
    days_per_week: Optional[int] = Field(None, description="How many days per week the user can train")


class GetUserProfileTool(BaseTool):
    name: ClassVar[str] = "get_user_profile"
    description: ClassVar[str] = (
        "Fetch the current user's stored health profile (age, sex, height, weight, "
        "activity level, goal, dietary restrictions, allergies, injuries, equipment, "
        "training days). Call this before asking the user for information they may "
        "have already provided in past sessions."
    )
    args_schema: ClassVar[type[GetProfileArgs]] = GetProfileArgs

    _scope: "UserScope" = PrivateAttr()

    def __init__(self, scope: "UserScope"):
        super().__init__()
        self._scope = scope

    def _run(self) -> str:
        try:
            return format_profile(self._scope.get_profile())
        except Exception as exc:
            return f"Profile store error: {exc}"


class UpdateUserProfileTool(BaseTool):
    name: ClassVar[str] = "update_user_profile"
    description: ClassVar[str] = (
        "Save or update fields of the current user's health profile so they persist "
        "across sessions. Call this whenever the user shares a stable fact about "
        "themselves (measurements, restrictions, injuries, equipment, schedule). "
        "Only pass fields the user actually stated. Never store names, emails, or "
        "other identifiers."
    )
    args_schema: ClassVar[type[UpdateProfileArgs]] = UpdateProfileArgs

    _scope: "UserScope" = PrivateAttr()

    def __init__(self, scope: "UserScope"):
        super().__init__()
        self._scope = scope

    def _run(self, **updates) -> str:
        try:
            profile = self._scope.update_profile(updates)
        except Exception as exc:
            return f"Profile update error: {exc}"
        return "Profile saved. " + format_profile(profile)


def make_profile_tools(scope: "UserScope") -> List[BaseTool]:
    """Profile tools are loaded in EVERY domain — memory is cross-cutting.
    They bind to a UserScope, so the LLM has no way to name another user."""
    return [GetUserProfileTool(scope), UpdateUserProfileTool(scope)]
