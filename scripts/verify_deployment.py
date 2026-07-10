"""Deployment verification for HealthVA. Safe to run anywhere — prints NO secrets.

Checks, in order:
1. Which secrets are present (names only, never values)
2. Database connectivity + users-table schema init (Postgres if DATABASE_URL, else SQLite)
3. Profile write/read roundtrip against the real store
4. LLM backend construction for the resolved backend
5. (--live) one real agent turn per route family, including the cross-domain prompt

Usage:
    python verify_deployment.py          # checks 1-4, no LLM calls
    python verify_deployment.py --live   # adds real agent turns (costs a few cents max)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import os
import sys

from dotenv import load_dotenv

load_dotenv()

PASS, FAIL, WARN = "✅", "❌", "⚠️"
failures = 0


def report(ok: bool, label: str, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"{PASS if ok else FAIL} {label}" + (f" — {detail}" if detail else ""))


def sanitized(exc: Exception) -> str:
    """Exception summary that cannot leak credentials embedded in URLs."""
    text = f"{type(exc).__name__}"
    msg = str(exc)
    # keep only the part after the last '@' is unsafe too; keep class + generic hints
    for hint in ("password", "timeout", "resolve", "refused", "authentication", "SSL", "does not exist"):
        if hint.lower() in msg.lower():
            text += f" (mentions: {hint})"
    return text


def check_env() -> str:
    openai, groq, db = (bool(os.getenv(k)) for k in ("OPENAI_API_KEY", "GROQ_API_KEY", "DATABASE_URL"))
    backend = os.getenv("AGENT_BACKEND") or ("openai" if openai else "groq" if groq else "ollama")
    report(openai or groq or backend == "ollama", "LLM key present", f"resolved backend: {backend}")
    report(db, "DATABASE_URL present", "Postgres" if db else "will fall back to local SQLite")
    if os.getenv("SPACE_ID") and not db:
        report(False, "HF Space without DATABASE_URL", "profiles would be ephemeral")
    return backend


def check_store():
    from healthva.memory import UserStore

    try:
        store = UserStore()
        report(True, f"UserStore initialized (backend: {store.backend}, schema ensured)")
    except Exception as exc:
        report(False, "UserStore initialization", sanitized(exc))
        return None
    try:
        probe = store.scope("_verify_deployment_probe")
        probe.update_profile({"age": 30, "injuries": ["torn acl"]})
        profile = probe.get_profile()
        ok = profile.get("age") == 30 and profile.get("injuries") == ["torn acl"]
        report(ok, "Profile write/read roundtrip", f"backend: {store.backend}")
        check_isolation(store)
        probe.delete_profile()
    except Exception as exc:
        report(False, "Profile roundtrip", sanitized(exc))
    return store


def check_isolation(store) -> None:
    """Adversarial check: user B must not be able to see or modify user A's row,
    even via raw SQL under B's identity (Postgres row-level security)."""
    if store.backend != "postgres":
        report(True, "Cross-user isolation", "SQLite dev mode: structural scoping only (no RLS)")
        return
    victim = "_verify_deployment_probe"  # written by the roundtrip check above
    attacker = store.scope("_verify_rls_attacker")
    try:
        with store._connect() as conn:
            # Under the attacker's transaction identity, address the victim's row directly.
            read = attacker._execute(
                conn, "SELECT profile FROM users WHERE username = %s", (victim,)
            ).fetchall()
            update = attacker._execute(
                conn, "UPDATE users SET profile = '{}' WHERE username = %s", (victim,)
            ).rowcount
            conn.rollback()
        with store._connect() as conn:
            # App role with no identity set -> the policy must hide every row.
            conn.execute(f"SET LOCAL ROLE {store.app_role}")
            bare = conn.execute("SELECT count(*) FROM users").fetchone()[0]
            conn.rollback()
        report(
            store.app_role is not None and len(read) == 0 and update == 0 and bare == 0,
            "Cross-user isolation (Postgres RLS)",
            f"role: {store.app_role}, cross-read rows: {len(read)}, "
            f"cross-update rows: {update}, no-identity visible rows: {bare}",
        )
    except Exception as exc:
        report(False, "Cross-user isolation (Postgres RLS)", sanitized(exc))


def check_llm(backend: str):
    from healthva.common import HealthAgentConfig, build_llm

    models = {"openai": "gpt-4o-mini", "groq": "llama-3.3-70b-versatile", "ollama": "qwen2.5:3b"}
    config = HealthAgentConfig(backend=backend, model_id=models.get(backend, "gpt-4o-mini"))
    try:
        build_llm(config)
        report(True, f"LLM backend constructs ({backend}/{config.model_id})")
        return config
    except Exception as exc:
        report(False, "LLM backend construction", sanitized(exc))
        return None


def check_live(config, store) -> None:
    from healthva.common import ToolLoggingHandler, TOOL_LOG_PATH
    from healthva.agents import HealthAgent

    agent = HealthAgent(config, store=store)
    log = ToolLoggingHandler(TOOL_LOG_PATH)
    user = "_verify_live_probe"

    turns = [
        ("workout + memory", "I'm a 28 year old male, 180cm, 80kg with a torn ACL, dumbbells only, "
         "3 days a week. Save my info and build my weekly plan with your planning tool."),
        ("diet + recall", "Suggest a high-protein vegetarian dinner for me."),
        ("cross-domain (both)", "Plan tomorrow: a workout that suits me and what I should eat after it."),
    ]
    for label, prompt in turns:
        log.clear_thought_process()
        try:
            reply, route = agent.chat(user, prompt, [], log)
            tools = [l.split("`")[1] for l in log.thought_process if l.startswith("🛠️")]
            grounding = any(l.startswith("🚨") for l in log.thought_process)
            report(bool(reply and reply != "[No response]"),
                   f"Live turn [{label}]",
                   f"route={route}, tools={tools}" + (", grounding retry fired" if grounding else ""))
        except Exception as exc:
            report(False, f"Live turn [{label}]", sanitized(exc))
    try:
        scope = store.scope(user)
        profile = scope.get_profile()
        report("injuries" in profile, "Live profile persisted", f"fields: {sorted(profile)}")
        scope.delete_profile()
    except Exception as exc:
        report(False, "Live profile check", sanitized(exc))


if __name__ == "__main__":
    print("— HealthVA deployment verification —")
    backend = check_env()
    store = check_store()
    config = check_llm(backend)
    if "--live" in sys.argv and config and store:
        check_live(config, store)
    elif "--live" in sys.argv:
        print(f"{WARN} skipping live checks: prerequisites failed")
    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)
