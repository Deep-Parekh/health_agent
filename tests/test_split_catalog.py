"""Tests for database-grounded workout splits and cardio planning."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import healthva.workout_tools as workout_tools
from healthva.workout_tools import (
    WORKOUTS_DB_PATH,
    BuildWeeklyPlanTool,
    build_weekly_plan,
    load_splits,
    resolve_injury_exclusions,
)
from tests.test_injury_guardrail import violates_exclusions


REQUIRED_WEEKLY_MUSCLES = {
    "abdominals",
    "biceps",
    "calves",
    "chest",
    "glutes",
    "hamstrings",
    "lats",
    "middle back",
    "quadriceps",
    "shoulders",
    "triceps",
}


def _exercise_facts(name: str) -> dict:
    with sqlite3.connect(WORKOUTS_DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, category, force FROM exercises WHERE name = ?", (name,)
        ).fetchone()
        muscles = {
            muscle
            for (muscle,) in conn.execute(
                "SELECT muscle FROM exercise_muscles WHERE exercise_id = ?",
                (row[0],),
            )
        }
    return {"category": row[1], "force": row[2], "muscles": muscles}


class TestSplitCatalog(unittest.TestCase):
    def test_each_frequency_has_one_default(self):
        catalog = load_splits()
        self.assertEqual(13, len(catalog))
        for frequency in range(1, 7):
            defaults = [
                split
                for split in catalog.values()
                if split["days_per_week"] == frequency and split["is_default"]
            ]
            self.assertEqual(1, len(defaults), f"{frequency}-day defaults")

    def test_every_split_covers_major_muscles_and_core(self):
        for split_id, split in load_splits().items():
            with self.subTest(split=split_id):
                plan = build_weekly_plan(
                    split["days_per_week"],
                    split=split_id,
                    exercises_per_muscle=2,
                )
                covered = {
                    muscle
                    for day in plan["days"]
                    for exercise in day["exercises"]
                    for muscle in _exercise_facts(exercise["name"])["muscles"]
                }
                self.assertTrue(
                    REQUIRED_WEEKLY_MUSCLES <= covered,
                    f"{split_id} misses {sorted(REQUIRED_WEEKLY_MUSCLES - covered)}",
                )
                core_days = sum(
                    "abdominals" in day["muscles"] for day in split["days"]
                )
                required_core_days = 1 if split["days_per_week"] == 1 else 2
                self.assertGreaterEqual(core_days, required_core_days)

    def test_unknown_and_frequency_mismatched_splits_are_refused(self):
        unknown = BuildWeeklyPlanTool()._run(days_per_week=3, split="not_a_split")
        self.assertIn("Unknown split", unknown)
        self.assertIn("ppl — Push / Pull / Legs (3d/week)", unknown)
        self.assertIn("ul_cardio — Upper / Lower / Cardio (3d/week)", unknown)

        mismatch = BuildWeeklyPlanTool()._run(days_per_week=4, split="ppl")
        self.assertIn("requires 3 training days, not 4", mismatch)
        self.assertIn("Available 4-day splits", mismatch)
        self.assertNotIn("ul_cardio —", mismatch)

    def test_display_name_accepts_natural_with_wording(self):
        output = BuildWeeklyPlanTool()._run(
            days_per_week=3,
            split="Upper/Lower with cardio",
            exercises_per_muscle=1,
        )
        self.assertIn("Weekly plan: Upper / Lower / Cardio", output)

    def test_cardio_day_contains_only_cardio_and_core_work(self):
        plan = build_weekly_plan(
            days_per_week=3, split="ul_cardio", exercises_per_muscle=1
        )
        cardio_day = next(day for day in plan["days"] if day["is_cardio"])
        self.assertTrue(
            any(exercise["muscle"] == "cardio" for exercise in cardio_day["exercises"])
        )
        for exercise in cardio_day["exercises"]:
            if exercise["muscle"] == "cardio":
                self.assertEqual("cardio", exercise["category"])
            else:
                self.assertEqual("abdominals", exercise["muscle"])

    def test_named_ppl_preserves_injury_exclusions(self):
        plan = build_weekly_plan(
            days_per_week=3,
            split="ppl",
            injuries=["torn acl"],
            exercises_per_muscle=1,
        )
        exclusions = resolve_injury_exclusions(["torn acl"])
        for day in plan["days"]:
            for exercise in day["exercises"]:
                self.assertFalse(
                    violates_exclusions(_exercise_facts(exercise["name"]), exclusions),
                    exercise["name"],
                )
        self.assertTrue(any("quadriceps" in note for note in plan["notes"]))
        self.assertTrue(any("NOT medical advice" in note for note in plan["notes"]))

    def test_old_database_uses_compatibility_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = Path(tmp) / "workouts.db"
            sqlite3.connect(old_db).close()
            load_splits.cache_clear()
            try:
                with mock.patch.object(workout_tools, "WORKOUTS_DB_PATH", old_db):
                    catalog = load_splits()
            finally:
                load_splits.cache_clear()
        self.assertEqual(6, len(catalog))
        self.assertEqual("Push / Pull / Legs", catalog["ppl"]["name"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
