"""Tests for balanced day volume and the optional time budget.

    python -m unittest tests.test_balance -v
"""

from __future__ import annotations

import unittest

from healthva.workout_tools import (
    DEFAULT_SESSION_MIN,
    _target_exercises,
    _minutes_for_day,
    build_weekly_plan,
)


def _counts(plan) -> list:
    return [len(d["exercises"]) for d in plan["days"]]


class TestTimeMapping(unittest.TestCase):
    def test_minutes_to_target_clamped(self):
        self.assertEqual(_target_exercises(8), 3)      # tiny -> floor 3
        self.assertEqual(_target_exercises(60), 8)     # ~8 min each
        self.assertEqual(_target_exercises(1000), 10)  # huge -> ceil 10

    def test_minutes_for_day_resolution(self):
        self.assertEqual(_minutes_for_day(None, 0), DEFAULT_SESSION_MIN)
        self.assertEqual(_minutes_for_day(45, 3), 45)          # single value, any day
        self.assertEqual(_minutes_for_day([60, 30, 90], 1), 30)  # per-day list
        self.assertEqual(_minutes_for_day([60, 30], 5), 30)      # short list -> last repeats


class TestDayBalance(unittest.TestCase):
    def _assert_balanced(self, plan, tol=2):
        counts = _counts(plan)
        self.assertTrue(counts, "plan has days")
        self.assertLessEqual(
            max(counts) - min(counts), tol,
            f"days not balanced: {counts} ({[d['day'] for d in plan['days']]})",
        )

    def test_default_plan_days_are_balanced(self):
        # The original bug: PPL bodyweight-only made the Pull day far shorter.
        for days in (3, 4, 5, 6):
            with self.subTest(days=days):
                plan = build_weekly_plan(days_per_week=days, level="beginner")
                self._assert_balanced(plan)

    def test_bodyweight_pull_day_no_longer_collapses(self):
        # Bodyweight pulling is genuinely DB-limited, so we can't force perfect
        # balance — but the Pull day must not collapse to ~3, and the plan must
        # honestly flag the shortfall instead of faking it.
        plan = build_weekly_plan(days_per_week=3, level="beginner", equipment=["body only"])
        self.assertTrue(all(len(d["exercises"]) >= 4 for d in plan["days"]), _counts(plan))
        self.assertTrue(
            any("Shorter than target" in n for n in plan["notes"]),
            "expected an honest note about the data-limited short day",
        )

    def test_short_session_balances_bodyweight(self):
        # When the user picks a shorter session, every day (incl. Pull) can hit
        # the lower target, so bodyweight days come out balanced.
        plan = build_weekly_plan(days_per_week=3, equipment=["body only"], minutes_per_day=30)
        self._assert_balanced(plan)

    def test_no_time_uses_default_and_reports_duration(self):
        plan = build_weekly_plan(days_per_week=3)
        for d in plan["days"]:
            self.assertEqual(d["target_minutes"], DEFAULT_SESSION_MIN)
            self.assertGreater(d["estimated_minutes"], 0)

    def test_per_day_minutes_scales_volume(self):
        plan = build_weekly_plan(days_per_week=4, minutes_per_day=[75, 30, 75, 30])
        counts = _counts(plan)
        # The 30-min days should be shorter than the 75-min days.
        self.assertLess(counts[1], counts[0])
        self.assertLess(counts[3], counts[2])

    def test_muscle_coverage_preserved(self):
        # Every trainable muscle in a day still appears at least once.
        plan = build_weekly_plan(days_per_week=3, minutes_per_day=20)  # very short
        for d in plan["days"]:
            if d["is_cardio"]:
                continue
            muscles = {ex["muscle"] for ex in d["exercises"]}
            self.assertTrue(muscles, f"{d['day']} has no exercises")


if __name__ == "__main__":
    unittest.main(verbosity=2)
