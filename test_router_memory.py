"""Tests for the router's keyword path and the user memory store.

    python -m unittest test_router_memory -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents import keyword_route, route_query
from memory import UserStore, validate_profile_updates

ROUTE_CASES = [
    ("What should I eat for breakfast?", "diet"),
    ("Give me a high protein recipe", "diet"),
    ("How many calories in an egg?", "diet"),
    ("Build me a push day workout", "workout"),
    ("What's my 1RM if I bench 100kg for 5?", "workout"),
    ("I have a torn ACL, can I still train legs?", "workout"),
    ("Plan my workouts and meals for the week", "both"),
    ("I want to build muscle, what should I eat after the gym?", "both"),
    ("hello", None),          # ambiguous -> LLM or general
    ("thanks!", None),
]


class TestKeywordRouter(unittest.TestCase):
    def test_keyword_routes(self):
        for text, expected in ROUTE_CASES:
            with self.subTest(text=text):
                self.assertEqual(keyword_route(text), expected)

    def test_ambiguous_without_llm_defaults_to_general(self):
        domain, method = route_query("hello there", llm=None)
        self.assertEqual((domain, method), ("general", "default"))

    def test_clear_case_never_calls_llm(self):
        # llm=object() would blow up if invoked — keywords must short-circuit.
        domain, method = route_query("give me a chest workout", llm=object())
        self.assertEqual((domain, method), ("workout", "keywords"))


class TestUserStore(unittest.TestCase):
    def setUp(self):
        # Isolated SQLite DB per test run
        self._tmp = tempfile.TemporaryDirectory()
        import memory
        self._orig = memory.USERS_DB_PATH
        memory.USERS_DB_PATH = Path(self._tmp.name) / "users.db"
        import common
        common.USERS_DB_PATH = memory.USERS_DB_PATH
        # Tests must NEVER touch the real Postgres from .env — strip the env var
        # for the store's lifetime instead of overriding attributes post-hoc.
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop("DATABASE_URL", None)
        self.store = UserStore(database_url=None)
        self.assertEqual(self.store.backend, "sqlite")

    def tearDown(self):
        self._env.stop()
        import memory
        memory.USERS_DB_PATH = self._orig
        self._tmp.cleanup()

    def test_profile_roundtrip_and_merge(self):
        self.assertEqual(self.store.scope("deep").get_profile(), {})
        self.store.scope("deep").update_profile({"height_cm": 180, "weight_kg": 80})
        self.store.scope("Deep ").update_profile({"injuries": ["torn acl"]})  # case/space-insensitive
        profile = self.store.scope("deep").get_profile()
        self.assertEqual(profile["height_cm"], 180.0)
        self.assertEqual(profile["weight_kg"], 80.0)
        self.assertEqual(profile["injuries"], ["torn acl"])

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            validate_profile_updates({"email": "a@b.com"})
        with self.assertRaises(ValueError):
            self.store.scope("deep").update_profile({"ssn": "123"})

    def test_type_coercion_and_list_handling(self):
        clean = validate_profile_updates(
            {"age": "30", "dietary_restrictions": "vegetarian, dairy_free", "days_per_week": 3}
        )
        self.assertEqual(clean["age"], 30)
        self.assertEqual(clean["dietary_restrictions"], ["vegetarian", "dairy_free"])

    def test_users_are_isolated(self):
        self.store.scope("alice").update_profile({"weight_kg": 60})
        self.store.scope("bob").update_profile({"weight_kg": 90})
        self.assertEqual(self.store.scope("alice").get_profile()["weight_kg"], 60.0)
        self.assertEqual(self.store.scope("bob").get_profile()["weight_kg"], 90.0)

    def test_scope_api_cannot_address_other_users(self):
        """The session-facing API must have no parameter that names a user:
        cross-user access should be structurally impossible, not just forbidden."""
        import inspect
        from memory import UserScope
        for method in ("get_profile", "update_profile", "delete_profile"):
            params = inspect.signature(getattr(UserScope, method)).parameters
            self.assertNotIn("username", params, f"{method} must not accept a username")

    def test_store_has_no_direct_profile_methods(self):
        """All access must flow through scope() — direct store methods with a
        username parameter were removed and must not come back."""
        self.assertFalse(hasattr(self.store, "get_profile"))
        self.assertFalse(hasattr(self.store, "update_profile"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
