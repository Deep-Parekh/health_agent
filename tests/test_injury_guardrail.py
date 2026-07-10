"""Tests for the injury guardrail: classification, exclusion integrity, and refusals.

Run with the diet agent's environment (needs langchain + the built workouts.db):

    python -m unittest test_injury_guardrail -v
"""

from __future__ import annotations

import sqlite3
import unittest

from healthva.workout_tools import (
    INJURY_EXCLUSIONS,
    WORKOUTS_DB_PATH,
    BuildWeeklyPlanTool,
    BuildWorkoutTool,
    ExerciseSearchTool,
    build_weekly_plan,
    classify_injury,
    exercise_search,
    resolve_injury_exclusions,
)

# Natural-language injury phrases and the body areas they must classify to.
CLASSIFICATION_CASES = [
    # ligaments -> knee
    ("torn ACL", ["knee"]),
    ("acl tear", ["knee"]),
    ("MCL sprain", ["knee"]),
    ("partially torn pcl", ["knee"]),
    ("LCL injury", ["knee"]),
    ("meniscus tear", ["knee"]),
    ("patellar tendonitis", ["knee"]),
    ("runner's knee", ["knee"]),
    # shoulder
    ("rotator cuff tear", ["shoulder"]),
    ("torn labrum", ["shoulder"]),
    ("shoulder impingement", ["shoulder"]),
    ("frozen shoulder", ["shoulder"]),
    ("dislocated shoulder", ["shoulder"]),
    # spine / back
    ("spinal injury", ["lower back", "neck"]),
    ("herniated disc", ["lower back"]),
    ("bulging disc in my back", ["lower back"]),
    ("sciatica", ["lower back"]),
    ("lumbar strain", ["lower back"]),
    # elbow / wrist
    ("tennis elbow", ["elbow"]),
    ("golfer's elbow", ["elbow"]),
    ("UCL sprain", ["elbow"]),
    ("carpal tunnel", ["wrist"]),
    ("sprained wrist", ["wrist"]),
    # ankle / foot
    ("achilles tendonitis", ["ankle"]),
    ("sprained ankle", ["ankle"]),
    ("plantar fasciitis", ["ankle"]),
    # hip
    ("hip labral tear", ["hip"]),
    ("hip flexor strain", ["hip"]),
    ("groin pull", ["hip"]),
    # neck
    ("whiplash", ["neck"]),
    ("cervical strain", ["neck"]),
    # canonical names pass through
    ("knee", ["knee"]),
    ("lower back", ["lower back"]),
    # multiple injuries in one phrase
    ("torn acl and rotator cuff", ["knee", "shoulder"]),
]

# Phrases that must NOT classify (agent should escalate to a professional).
UNCLASSIFIABLE_CASES = ["mystery pain", "broken femur", "hernia", "concussion", ""]


def _exercise_facts(name: str) -> dict:
    """Ground truth for one exercise straight from the database."""
    conn = sqlite3.connect(WORKOUTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, category, force FROM exercises WHERE name = ?", (name,)
    ).fetchone()
    muscles = [
        r["muscle"]
        for r in conn.execute(
            "SELECT muscle FROM exercise_muscles WHERE exercise_id = ?", (row["id"],)
        )
    ]
    conn.close()
    return {"category": row["category"], "force": row["force"], "muscles": muscles}


def violates_exclusions(facts: dict, exclusions: dict) -> bool:
    """Shared ground-truth check used by both tests and the eval harness."""
    if set(facts["muscles"]) & set(exclusions["muscles"]):
        return True
    if facts["category"] in exclusions["categories"]:
        return True
    for rule in exclusions["force_rules"]:
        if facts["force"] in rule["forces"] and set(facts["muscles"]) & set(rule["muscles"]):
            return True
    return False


class TestInjuryClassification(unittest.TestCase):
    def test_phrases_classify_to_expected_areas(self):
        for phrase, expected in CLASSIFICATION_CASES:
            with self.subTest(phrase=phrase):
                self.assertEqual(classify_injury(phrase), sorted(expected))

    def test_unclassifiable_phrases_return_empty(self):
        for phrase in UNCLASSIFIABLE_CASES:
            with self.subTest(phrase=phrase):
                self.assertEqual(classify_injury(phrase), [])

    def test_unclassifiable_injury_raises_with_guidance(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_injury_exclusions(["broken femur"])
        self.assertIn("medical professional", str(ctx.exception))


class TestExclusionIntegrity(unittest.TestCase):
    """No planned exercise may touch an excluded muscle or category."""

    def _assert_plan_clean(self, plan: dict, exclusions: dict):
        for day in plan["days"]:
            for ex in day["exercises"]:
                facts = _exercise_facts(ex["name"])
                self.assertFalse(
                    violates_exclusions(facts, exclusions),
                    f"{ex['name']} violates exclusions for this injury: {facts}",
                )

    def test_every_injury_produces_clean_weekly_plans(self):
        for injury in INJURY_EXCLUSIONS:
            exclusions = resolve_injury_exclusions([injury])
            for days in (2, 3, 5):
                with self.subTest(injury=injury, days=days):
                    plan = build_weekly_plan(
                        days_per_week=days, level="beginner", injuries=[injury]
                    )
                    self._assert_plan_clean(plan, exclusions)

    def test_alias_input_produces_same_exclusions_as_canonical(self):
        for phrase, expected_areas in CLASSIFICATION_CASES:
            with self.subTest(phrase=phrase):
                via_alias = resolve_injury_exclusions([phrase])
                via_canonical = resolve_injury_exclusions(expected_areas)
                self.assertEqual(via_alias["muscles"], via_canonical["muscles"])
                self.assertEqual(via_alias["categories"], via_canonical["categories"])

    def test_search_respects_injury_exclusions(self):
        results = exercise_search(
            muscle="chest",
            exclude_muscles=INJURY_EXCLUSIONS["shoulder"]["muscles"],
            max_results=50,
        )
        for ex in results:
            touched = set(ex["primary_muscles"] + ex["secondary_muscles"])
            self.assertFalse(touched & {"shoulders", "traps"}, ex["name"])

    def test_arm_load_path_blocks_pressing_for_arm_joint_injuries(self):
        """A bench press or flye loads the elbow/wrist/shoulder even though the
        dataset lists only chest — the load-path rule must exclude them."""
        for injury in ("elbow", "wrist", "shoulder"):
            exclusions = resolve_injury_exclusions([injury])
            for name_kw in ("Bench Press", "Flyes", "Pushups"):
                with self.subTest(injury=injury, exercise=name_kw):
                    results = exercise_search(
                        keyword=name_kw,
                        exclude_muscles=exclusions["muscles"],
                        exclude_categories=exclusions["categories"],
                        exclude_force_rules=exclusions["force_rules"],
                        max_results=50,
                    )
                    self.assertEqual(
                        [], [e["name"] for e in results],
                        f"{name_kw} should be excluded for {injury} injury",
                    )

    def test_leg_work_survives_elbow_injury(self):
        """The load-path rule must not over-fire: squats are fine with a bad elbow."""
        exclusions = resolve_injury_exclusions(["tennis elbow"])
        results = exercise_search(
            muscle="quadriceps",
            exclude_muscles=exclusions["muscles"],
            exclude_categories=exclusions["categories"],
            exclude_force_rules=exclusions["force_rules"],
            max_results=10,
        )
        self.assertTrue(results, "quadriceps work should survive an elbow injury")


class TestToolGuardrails(unittest.TestCase):
    def test_disclaimer_always_present_with_injury(self):
        out = BuildWeeklyPlanTool()._run(days_per_week=3, injuries=["torn acl"])
        self.assertIn("NOT medical advice", out)
        out = BuildWorkoutTool()._run(muscles=["chest"], injuries=["rotator cuff tear"])
        self.assertIn("NOT medical advice", out)
        out = ExerciseSearchTool()._run(muscle="biceps", injuries=["tennis elbow"])
        self.assertIn("NOT medical advice", out)

    def test_interpretation_note_shows_mapping(self):
        out = BuildWeeklyPlanTool()._run(days_per_week=3, injuries=["torn acl"])
        self.assertIn("'torn acl' -> knee", out)

    def test_unknown_injury_is_refused_not_ignored(self):
        out = BuildWeeklyPlanTool()._run(days_per_week=3, injuries=["broken femur"])
        self.assertIn("Cannot classify", out)
        self.assertNotIn("Push", out)  # no plan produced

    def test_seven_days_refused(self):
        out = BuildWeeklyPlanTool()._run(days_per_week=7)
        self.assertIn("rest day", out)

    def test_no_disclaimer_without_injury(self):
        out = BuildWeeklyPlanTool()._run(days_per_week=3)
        self.assertNotIn("NOT medical advice", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
