"""Evaluation harness for the injury guardrail. Generates EVALUATION.md.

Measures four things:
1. Classification accuracy — natural-language injury phrases (ligaments, discs,
   tendons) onto canonical body areas, against a labeled set.
2. Exclusion integrity — sweep injuries x levels x equipment x frequencies,
   verify against the database that no planned exercise touches an excluded
   muscle or category (violation rate).
3. Disclaimer coverage — every injury-bearing plan must carry the medical
   disclaimer.
4. Coverage retention — how much of the exercise library survives each
   injury's conservative exclusions (documents the safety/utility tradeoff).

Usage:
    python eval_injury_guardrail.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import sqlite3
from datetime import date
from itertools import product
from pathlib import Path

from healthva.workout_tools import (
    INJURY_DISCLAIMER,
    INJURY_EXCLUSIONS,
    LEVELS,
    WORKOUTS_DB_PATH,
    build_weekly_plan,
    classify_injury,
    resolve_injury_exclusions,
)
from tests.test_injury_guardrail import (
    CLASSIFICATION_CASES,
    UNCLASSIFIABLE_CASES,
    violates_exclusions,
)

REPORT_PATH = Path(__file__).resolve().parents[1] / "docs" / "EVALUATION.md"

EQUIPMENT_CONFIGS = [
    ("any equipment", None),
    ("home: dumbbells only", ["dumbbell"]),
    ("gym: barbell + cable + machine", ["barbell", "cable", "machine"]),
]
FREQUENCIES = [2, 3, 4, 6]


def _exercise_facts(conn: sqlite3.Connection, name: str) -> dict:
    row = conn.execute(
        "SELECT id, category, force FROM exercises WHERE name = ?", (name,)
    ).fetchone()
    muscles = [
        r[0]
        for r in conn.execute(
            "SELECT muscle FROM exercise_muscles WHERE exercise_id = ?", (row[0],)
        )
    ]
    return {"category": row[1], "force": row[2], "muscles": muscles}


def eval_classification() -> dict:
    correct, failures = 0, []
    for phrase, expected in CLASSIFICATION_CASES:
        got = classify_injury(phrase)
        if got == sorted(expected):
            correct += 1
        else:
            failures.append((phrase, expected, got))
    refused, refusal_failures = 0, []
    for phrase in UNCLASSIFIABLE_CASES:
        if classify_injury(phrase) == []:
            refused += 1
        else:
            refusal_failures.append(phrase)
    return {
        "total": len(CLASSIFICATION_CASES),
        "correct": correct,
        "failures": failures,
        "refusal_total": len(UNCLASSIFIABLE_CASES),
        "refused": refused,
        "refusal_failures": refusal_failures,
    }


def eval_exclusion_integrity() -> dict:
    conn = sqlite3.connect(WORKOUTS_DB_PATH)
    configs = exercises_checked = 0
    violations = []
    disclaimer_missing = []
    per_injury_rows = []
    for injury in INJURY_EXCLUSIONS:
        exclusions = resolve_injury_exclusions([injury])
        injury_checked = injury_violations = 0
        for level, (eq_label, equipment), days in product(
            LEVELS, EQUIPMENT_CONFIGS, FREQUENCIES
        ):
            configs += 1
            plan = build_weekly_plan(
                days_per_week=days, level=level, equipment=equipment,
                injuries=[injury],
            )
            if INJURY_DISCLAIMER not in plan["notes"]:
                disclaimer_missing.append((injury, level, eq_label, days))
            for day in plan["days"]:
                for ex in day["exercises"]:
                    exercises_checked += 1
                    injury_checked += 1
                    facts = _exercise_facts(conn, ex["name"])
                    if violates_exclusions(facts, exclusions):
                        injury_violations += 1
                        violations.append(
                            (injury, level, eq_label, days, ex["name"], facts)
                        )
        per_injury_rows.append((injury, injury_checked, injury_violations))
    conn.close()
    return {
        "configs": configs,
        "exercises_checked": exercises_checked,
        "violations": violations,
        "disclaimer_missing": disclaimer_missing,
        "per_injury": per_injury_rows,
    }


def eval_coverage_retention() -> list:
    """Count surviving exercises per injury using the same ground-truth checker
    the integrity sweep uses (muscles + categories + joint load-path rules)."""
    conn = sqlite3.connect(WORKOUTS_DB_PATH)
    names = [r[0] for r in conn.execute("SELECT name FROM exercises")]
    all_facts = [_exercise_facts(conn, n) for n in names]
    conn.close()
    total = len(all_facts)
    rows = []
    for injury in INJURY_EXCLUSIONS:
        exclusions = resolve_injury_exclusions([injury])
        remaining = sum(
            1 for facts in all_facts if not violates_exclusions(facts, exclusions)
        )
        rows.append((injury, remaining, total, 100 * remaining / total))
    return rows


def write_report(cls: dict, integrity: dict, coverage: list) -> None:
    cls_acc = 100 * cls["correct"] / cls["total"]
    refusal_acc = 100 * cls["refused"] / cls["refusal_total"]
    viol_rate = 100 * len(integrity["violations"]) / max(1, integrity["exercises_checked"])
    disclaimer_cov = 100 * (
        1 - len(integrity["disclaimer_missing"]) / max(1, integrity["configs"])
    )

    lines = [
        "# Injury Guardrail Evaluation",
        "",
        f"*Generated by `eval_injury_guardrail.py` on {date.today().isoformat()}. "
        "All checks run against the local `data/workouts.db` "
        "(free-exercise-db, 873 exercises).*",
        "",
        "## Summary",
        "",
        "| Metric | Result |",
        "|--------|--------|",
        f"| Injury phrase classification accuracy | **{cls_acc:.1f}%** ({cls['correct']}/{cls['total']}) |",
        f"| Unknown-injury refusal rate | **{refusal_acc:.1f}%** ({cls['refused']}/{cls['refusal_total']}) |",
        f"| Exclusion violations | **{len(integrity['violations'])}** in {integrity['exercises_checked']:,} prescribed exercises ({viol_rate:.2f}%) |",
        f"| Plan configurations swept | {integrity['configs']} (8 injuries x 3 levels x 3 equipment setups x 4 frequencies) |",
        f"| Medical-disclaimer coverage | **{disclaimer_cov:.1f}%** of injury-bearing plans |",
        "",
        "## 1. Injury phrase classification",
        "",
        "Free-text injury descriptions (ligaments, discs, tendons, common "
        "diagnoses) are classified onto canonical body areas by a deterministic, "
        "auditable alias layer in `workout_tools.py` — the safety-critical mapping "
        "never depends on the LLM. Examples from the labeled set: 'torn ACL' -> "
        "knee, 'spinal injury' -> lower back + neck, 'hip labral tear' -> hip "
        "(not shoulder, despite the shared term 'labral').",
        "",
    ]
    if cls["failures"]:
        lines.append("**Misclassifications:**")
        for phrase, expected, got in cls["failures"]:
            lines.append(f"- '{phrase}': expected {expected}, got {got}")
    else:
        lines.append("All labeled phrases classified correctly.")
    lines += [
        "",
        "Phrases outside the supported taxonomy (e.g. 'broken femur', "
        "'concussion') are refused with a prompt to consult a medical "
        "professional, rather than silently ignored"
        + ("." if not cls["refusal_failures"] else
           f" — EXCEPT: {cls['refusal_failures']}."),
        "",
        "## 2. Exclusion integrity sweep",
        "",
        "For every combination of injury, difficulty level, equipment setup, and "
        "training frequency, a weekly plan was generated and every prescribed "
        "exercise was verified against the database on three rules: it must not "
        "touch an excluded muscle (as primary **or** secondary), must not belong "
        "to an excluded category (e.g. plyometrics for knee/ankle injuries), and "
        "must not violate a **joint load-path rule** — for shoulder, elbow, and "
        "wrist injuries, all upper-body push/pull movements are excluded because "
        "they transmit load through the whole arm (a bench press or flye "
        "stresses the elbow even though the dataset lists only chest as the "
        "working muscle).",
        "",
        "| Injury | Exercises prescribed | Violations |",
        "|--------|---------------------|------------|",
    ]
    for injury, checked, viols in integrity["per_injury"]:
        lines.append(f"| {injury} | {checked:,} | {viols} |")
    if integrity["violations"]:
        lines += ["", "**Violation details:**"]
        for v in integrity["violations"][:20]:
            lines.append(f"- {v}")
    lines += [
        "",
        "## 3. Coverage retention (safety/utility tradeoff)",
        "",
        "The guardrail is deliberately conservative: an exercise is dropped if "
        "the injured area appears even as a secondary muscle. This table "
        "documents how much of the 873-exercise library remains usable per "
        "injury — the cost of failing safe.",
        "",
        "| Injury | Exercises still available | % of library |",
        "|--------|--------------------------|--------------|",
    ]
    for injury, remaining, total, pct in coverage:
        lines.append(f"| {injury} | {remaining} / {total} | {pct:.0f}% |")
    lines += [
        "",
        "## Method notes & limitations",
        "",
        "- Classification is exact-term matching (word-boundary, longest-first, "
        "span-consuming), not semantic: misspellings ('roator cuff') and unlisted "
        "terms fall through to refusal — the safe failure mode, since refusal "
        "escalates to a professional rather than producing an unsafe plan.",
        "- The load-path rule covers push/pull forces; static weight-bearing "
        "holds (e.g. planks on an injured elbow) are not yet caught because the "
        "dataset lists them under core muscles with force='static'. Known gap, "
        "candidate for a follow-up rule.",
        "- Exclusion rules are area-level heuristics reviewed for plausibility, "
        "not clinically validated protocols; every injury-bearing output carries "
        "a consult-a-professional disclaimer enforced in the tool layer, so the "
        "LLM cannot omit it.",
        "- The eval is fully deterministic and re-runnable "
        "(`python eval_injury_guardrail.py`); the companion unit tests "
        "(`python -m unittest test_injury_guardrail`) gate regressions in CI.",
    ]
    REPORT_PATH.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    print("Evaluating classification ...")
    cls = eval_classification()
    print(f"  accuracy {cls['correct']}/{cls['total']}, "
          f"refusals {cls['refused']}/{cls['refusal_total']}")
    print("Sweeping exclusion integrity (this builds a few hundred plans) ...")
    integrity = eval_exclusion_integrity()
    print(f"  {integrity['configs']} configs, "
          f"{integrity['exercises_checked']:,} exercises checked, "
          f"{len(integrity['violations'])} violations, "
          f"{len(integrity['disclaimer_missing'])} missing disclaimers")
    print("Computing coverage retention ...")
    coverage = eval_coverage_retention()
    write_report(cls, integrity, coverage)
    print(f"Report written to {REPORT_PATH}")
