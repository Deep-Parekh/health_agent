"""Tests for equipment handling — especially exclusion ("everything except barbell").

    python -m unittest tests.test_equipment -v
"""

from __future__ import annotations

import sqlite3
import unittest

from healthva.workout_tools import (
    WORKOUTS_DB_PATH,
    build_weekly_plan,
    exercise_search,
)


class TestEquipmentExclusion(unittest.TestCase):
    def test_search_excludes_equipment(self):
        results = exercise_search(muscle="chest", exclude_equipment=["barbell"], max_results=50)
        self.assertTrue(results)
        for ex in results:
            self.assertNotEqual(ex["equipment"], "barbell", ex["name"])

    def test_barbell_only_exercises_disappear(self):
        with_barbell = exercise_search(keyword="Barbell Bench Press", max_results=10)
        self.assertTrue(with_barbell, "sanity: barbell exercises exist")
        without = exercise_search(
            keyword="Barbell Bench Press", exclude_equipment=["barbell"], max_results=10
        )
        self.assertEqual([], [e["name"] for e in without])

    def test_exclusion_is_null_safe(self):
        """Exercises with no equipment recorded must survive a NOT IN filter."""
        conn = sqlite3.connect(WORKOUTS_DB_PATH)
        null_count = conn.execute(
            "SELECT COUNT(*) FROM exercises WHERE equipment IS NULL"
        ).fetchone()[0]
        conn.close()
        if null_count == 0:
            self.skipTest("dataset has no NULL-equipment rows")
        results = exercise_search(exclude_equipment=["barbell"], max_results=1000)
        self.assertTrue(any(ex["equipment"] is None for ex in results))

    def test_weekly_plan_respects_exclusion(self):
        plan = build_weekly_plan(days_per_week=3, exclude_equipment=["barbell", "machine"])
        prescribed = [ex for day in plan["days"] for ex in day["exercises"]]
        self.assertTrue(prescribed)
        for ex in prescribed:
            self.assertNotIn(ex["equipment"], ("barbell", "machine"), ex["name"])

    def test_include_and_exclude_compose(self):
        results = exercise_search(
            muscle="quadriceps",
            equipment=["barbell", "dumbbell"],
            exclude_equipment=["barbell"],
            max_results=50,
        )
        self.assertTrue(results)
        for ex in results:
            self.assertIn(ex["equipment"], ("dumbbell", "body only"), ex["name"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
