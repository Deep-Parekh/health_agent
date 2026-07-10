"""Workout tools for the lifestyle agent.

Grounded in a local SQLite database (data/workouts.db) built from the
public-domain free-exercise-db dataset (873 exercises, 17 muscle groups).
Run data_prep.py first to build the database.

Follows the same BaseTool pattern as the diet agent (langchain_diet_agent/app.py)
so these tools can be appended directly to its tool list when merging.
"""

from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Union

from pydantic import BaseModel, Field
from langchain.tools import BaseTool

try:
    from healthva.common import WORKOUTS_DB_PATH  # Spaces-aware path (/tmp/data on HF)
except ImportError:  # standalone use (tests, eval) outside the app
    WORKOUTS_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "workouts.db"

# Canonical vocabulary from the dataset — returned to the model so it never
# has to guess valid filter values.
MUSCLE_GROUPS = [
    "abdominals", "abductors", "adductors", "biceps", "calves", "chest",
    "forearms", "glutes", "hamstrings", "lats", "lower back", "middle back",
    "neck", "quadriceps", "shoulders", "traps", "triceps",
]
LEVELS = ["beginner", "intermediate", "expert"]
CATEGORIES = [
    "cardio", "olympic weightlifting", "plyometrics", "powerlifting",
    "strength", "stretching", "strongman",
]
EQUIPMENT = [
    "bands", "barbell", "body only", "cable", "dumbbell", "e-z curl bar",
    "exercise ball", "foam roll", "kettlebells", "machine", "medicine ball",
    "other",
]

UPPER_BODY_MUSCLES = [
    "biceps", "chest", "forearms", "lats", "middle back", "neck",
    "shoulders", "traps", "triceps",
]

# Push/pull movements transmit load through every joint in the arm — a bench
# press or dumbbell flye stresses the elbow and wrist even when the dataset
# lists only "chest" as the working muscle. This rule closes that blind spot
# for arm-chain joints: exclude any push/pull exercise involving upper-body
# musculature, whatever muscles it nominally targets.
ARM_LOAD_PATH_RULE = {"forces": ["push", "pull"], "muscles": UPPER_BODY_MUSCLES}

# Conservative injury handling: an exercise is excluded if ANY of its primary
# or secondary muscles touches the injured area, plus whole categories that
# load the joint (e.g. plyometrics for knee/ankle), plus force_rules for
# joints in a load path (see ARM_LOAD_PATH_RULE). Over-excluding is the
# safe failure mode; the disclaimer always points to a professional.
INJURY_EXCLUSIONS: Dict[str, Dict[str, Any]] = {
    "shoulder": {
        "muscles": ["shoulders", "traps"],
        "categories": [],
        "force_rules": [ARM_LOAD_PATH_RULE],
    },
    "knee": {
        "muscles": ["quadriceps", "hamstrings", "adductors", "abductors"],
        "categories": ["plyometrics"],
        "force_rules": [],
    },
    "lower back": {
        "muscles": ["lower back"],
        "categories": ["olympic weightlifting", "powerlifting"],
        "force_rules": [],
    },
    "elbow": {
        "muscles": ["biceps", "triceps", "forearms"],
        "categories": [],
        "force_rules": [ARM_LOAD_PATH_RULE],
    },
    "wrist": {
        "muscles": ["forearms"],
        "categories": [],
        "force_rules": [ARM_LOAD_PATH_RULE],
    },
    "hip": {"muscles": ["glutes", "adductors", "abductors"], "categories": [], "force_rules": []},
    "ankle": {"muscles": ["calves"], "categories": ["plyometrics", "cardio"], "force_rules": []},
    "neck": {"muscles": ["neck", "traps"], "categories": [], "force_rules": []},
}

# Deterministic classification of specific injury terms (ligaments, discs,
# tendons, common diagnoses) onto the canonical body areas above. This keeps
# the safety-critical mapping in code — auditable and testable — instead of
# relying on the LLM to translate "torn ACL" correctly. An alias may map to
# several areas when the safe interpretation is broad (e.g. "spinal").
INJURY_ALIASES: Dict[str, List[str]] = {
    # knee ligaments and structures
    "acl": ["knee"], "mcl": ["knee"], "pcl": ["knee"], "lcl": ["knee"],
    "meniscus": ["knee"], "patella": ["knee"], "patellar": ["knee"],
    "jumper's knee": ["knee"], "runner's knee": ["knee"],
    "it band": ["knee", "hip"], "itb": ["knee", "hip"],
    # shoulder
    "rotator cuff": ["shoulder"], "labrum": ["shoulder"], "labral": ["shoulder"],
    "ac joint": ["shoulder"], "frozen shoulder": ["shoulder"],
    "impingement": ["shoulder"], "dislocated shoulder": ["shoulder"],
    # spine and back — "spinal"/"spine" conservatively cover both ends
    "spine": ["lower back", "neck"], "spinal": ["lower back", "neck"],
    "herniated disc": ["lower back"], "bulging disc": ["lower back"],
    "slipped disc": ["lower back"], "disc": ["lower back"],
    "sciatica": ["lower back"], "lumbar": ["lower back"],
    # elbow
    "tennis elbow": ["elbow"], "golfer's elbow": ["elbow"],
    "ucl": ["elbow"], "cubital tunnel": ["elbow"],
    # wrist and hand
    "carpal tunnel": ["wrist"], "tfcc": ["wrist"], "sprained wrist": ["wrist"],
    # ankle and foot
    "achilles": ["ankle"], "plantar fasciitis": ["ankle"],
    "sprained ankle": ["ankle"], "ankle sprain": ["ankle"],
    # hip and groin — longer aliases win over "labral"/"labrum" above
    "hip labrum": ["hip"], "hip labral": ["hip"],
    "hip flexor": ["hip"], "groin": ["hip"],
    # neck
    "cervical": ["neck"], "whiplash": ["neck"],
}

INJURY_DISCLAIMER = (
    "Injury reported: exercises involving the affected area were excluded as a "
    "precaution. This is general fitness information, NOT medical advice — "
    "consult a physician or physical therapist before training with an injury."
)

# Compatibility fallback for databases built before the split catalog existed.
_PUSH = ["chest", "shoulders", "triceps"]
_PULL = ["lats", "middle back", "biceps", "forearms"]
_LEGS = ["quadriceps", "hamstrings", "glutes", "calves"]
_UPPER = ["chest", "lats", "shoulders", "biceps", "triceps"]
_LOWER = ["quadriceps", "hamstrings", "glutes", "calves", "abdominals"]
_FULL = ["chest", "lats", "quadriceps", "hamstrings", "shoulders", "abdominals"]

SPLITS: Dict[int, List[tuple]] = {
    1: [("Full Body", _FULL)],
    2: [("Full Body A", _FULL), ("Full Body B", _FULL)],
    3: [("Push", _PUSH), ("Pull", _PULL), ("Legs", _LEGS)],
    4: [("Upper A", _UPPER), ("Lower A", _LOWER), ("Upper B", _UPPER), ("Lower B", _LOWER)],
    5: [("Push", _PUSH), ("Pull", _PULL), ("Legs", _LEGS), ("Upper", _UPPER), ("Lower", _LOWER)],
    6: [("Push A", _PUSH), ("Pull A", _PULL), ("Legs A", _LEGS),
        ("Push B", _PUSH), ("Pull B", _PULL), ("Legs B", _LEGS)],
}


# --- Pure logic (no LangChain dependency, unit-testable) ---

def _connect() -> sqlite3.Connection:
    if not WORKOUTS_DB_PATH.exists():
        raise FileNotFoundError(
            f"{WORKOUTS_DB_PATH} not found. Run data_prep.py to build it."
        )
    conn = sqlite3.connect(WORKOUTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fallback_split_catalog() -> Dict[str, dict]:
    """Represent the original hardcoded defaults in the catalog shape."""
    ids = {
        1: "full_body",
        2: "full_body_ab",
        3: "ppl",
        4: "upper_lower_4",
        5: "ppl_upper_lower",
        6: "ppl_6",
    }
    names = {
        1: "Full Body",
        2: "Full Body A/B",
        3: "Push / Pull / Legs",
        4: "Upper / Lower x2",
        5: "PPL + Upper / Lower",
        6: "PPL x2",
    }
    return {
        ids[frequency]: {
            "id": ids[frequency],
            "name": names[frequency],
            "days_per_week": frequency,
            "is_default": True,
            "description": "Built-in compatibility split for an older workout database.",
            "days": [
                {"day": day_name, "muscles": list(muscles), "is_cardio": False}
                for day_name, muscles in days
            ],
        }
        for frequency, days in SPLITS.items()
    }


@lru_cache(maxsize=1)
def load_splits() -> Dict[str, dict]:
    """Load the grounded split catalog, falling back for pre-catalog databases."""
    try:
        with _connect() as conn:
            split_rows = conn.execute(
                "SELECT id, name, days_per_week, is_default, description "
                "FROM splits ORDER BY days_per_week, is_default DESC, name"
            ).fetchall()
            if not split_rows:
                return _fallback_split_catalog()

            catalog: Dict[str, dict] = {}
            for row in split_rows:
                day_rows = conn.execute(
                    "SELECT day_name, muscles, is_cardio FROM split_days "
                    "WHERE split_id = ? ORDER BY day_order",
                    (row["id"],),
                ).fetchall()
                catalog[row["id"]] = {
                    "id": row["id"],
                    "name": row["name"],
                    "days_per_week": row["days_per_week"],
                    "is_default": bool(row["is_default"]),
                    "description": row["description"],
                    "days": [
                        {
                            "day": day["day_name"],
                            "muscles": json.loads(day["muscles"]),
                            "is_cardio": bool(day["is_cardio"]),
                        }
                        for day in day_rows
                    ],
                }
            return catalog
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return _fallback_split_catalog()


def _format_split_catalog(
    catalog: Dict[str, dict], days_per_week: Optional[int] = None
) -> str:
    options = [
        split
        for split in catalog.values()
        if days_per_week is None or split["days_per_week"] == days_per_week
    ]
    return "\n".join(
        f"{split['id']} — {split['name']} ({split['days_per_week']}d/week)"
        for split in sorted(options, key=lambda item: (item["days_per_week"], item["name"]))
    )


def _normalize_split_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower().replace("×", "x"))
    return " ".join(token for token in normalized.split() if token != "with")


def _select_split(days_per_week: int, requested: Optional[str]) -> dict:
    catalog = load_splits()
    if requested:
        requested_key = _normalize_split_name(requested)
        selected = next(
            (
                split
                for split in catalog.values()
                if requested_key
                in {
                    _normalize_split_name(split["id"]),
                    _normalize_split_name(split["name"]),
                }
            ),
            None,
        )
        if selected is None:
            raise ValueError(
                f"Unknown split '{requested}'. Available splits:\n"
                + _format_split_catalog(catalog)
            )
        if selected["days_per_week"] != days_per_week:
            raise ValueError(
                f"Split '{selected['name']}' requires {selected['days_per_week']} "
                f"training days, not {days_per_week}. Available {days_per_week}-day "
                "splits:\n"
                + _format_split_catalog(catalog, days_per_week)
            )
        return selected

    defaults = [
        split
        for split in catalog.values()
        if split["days_per_week"] == days_per_week and split["is_default"]
    ]
    if len(defaults) != 1:
        raise ValueError(
            f"Expected exactly one default split for {days_per_week} training days; "
            f"found {len(defaults)}."
        )
    return defaults[0]


def _normalize_equipment(equipment: Union[str, List[str], None]) -> Optional[List[str]]:
    """Accept a single string or a list; bodyweight is always available."""
    if equipment is None:
        return None
    if isinstance(equipment, str):
        equipment = [equipment]
    normalized = [e.lower().strip() for e in equipment if e and e.strip()]
    if not normalized:
        return None
    if "body only" not in normalized:
        normalized.append("body only")
    return normalized


def classify_injury(text: str) -> List[str]:
    """Classify a free-text injury description onto canonical body areas.

    Exact canonical names win; otherwise aliases are matched longest-first on
    word boundaries, and each matched span is consumed so 'hip labral' does not
    also fire the shoulder 'labral' alias. Returns [] when nothing matches.
    """
    remaining = text.lower().strip()
    # Canonical names count as aliases of themselves; one longest-first pass
    # ensures "hip labral" wins over both "hip" and the shoulder alias "labral".
    terms: Dict[str, List[str]] = {a: [a] for a in INJURY_EXCLUSIONS}
    terms.update(INJURY_ALIASES)
    areas: List[str] = []
    for term in sorted(terms, key=len, reverse=True):
        if re.search(rf"\b{re.escape(term)}\b", remaining):
            areas.extend(terms[term])
            remaining = re.sub(rf"\b{re.escape(term)}\b", " ", remaining)
    return sorted(set(areas))


def resolve_injury_exclusions(injuries: Optional[List[str]]) -> Dict[str, Any]:
    """Map injury descriptions to muscles/categories to exclude.

    Returns {"muscles": [...], "categories": [...], "mapped": {input: [areas]}}.
    Raises on descriptions that cannot be classified.
    """
    muscles: List[str] = []
    categories: List[str] = []
    force_rules: List[dict] = []
    mapped: Dict[str, List[str]] = {}
    for injury in injuries or []:
        areas = classify_injury(injury)
        if not areas:
            raise ValueError(
                f"Cannot classify injury '{injury}'. Supported body areas: "
                f"{', '.join(INJURY_EXCLUSIONS)} (specific terms like 'ACL', "
                "'rotator cuff', or 'herniated disc' are also recognized). "
                "For anything else, recommend the user consult a medical professional."
            )
        mapped[injury] = areas
        for area in areas:
            muscles.extend(INJURY_EXCLUSIONS[area]["muscles"])
            categories.extend(INJURY_EXCLUSIONS[area]["categories"])
            for rule in INJURY_EXCLUSIONS[area]["force_rules"]:
                if rule not in force_rules:
                    force_rules.append(rule)
    return {
        "muscles": sorted(set(muscles)),
        "categories": sorted(set(categories)),
        "force_rules": force_rules,
        "mapped": mapped,
    }


def format_injury_interpretation(mapped: Dict[str, List[str]]) -> Optional[str]:
    """Human-readable note showing how injury inputs were classified."""
    renamed = {
        inp: areas for inp, areas in mapped.items()
        if [inp.lower().strip()] != areas
    }
    if not renamed:
        return None
    parts = [f"'{inp}' -> {', '.join(areas)}" for inp, areas in renamed.items()]
    return "Interpreted injuries: " + "; ".join(parts)


def exercise_search(
    muscle: Optional[str] = None,
    level: Optional[str] = None,
    equipment: Union[str, List[str], None] = None,
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    exclude_muscles: Optional[List[str]] = None,
    exclude_categories: Optional[List[str]] = None,
    exclude_force_rules: Optional[List[dict]] = None,
    exclude_equipment: Union[str, List[str], None] = None,
    max_results: int = 5,
) -> List[dict]:
    """Query the local exercise DB with any combination of filters."""
    sql = """
        SELECT DISTINCT e.id, e.name, e.force, e.level, e.mechanic,
               e.equipment, e.category, e.instructions
        FROM exercises e
        LEFT JOIN exercise_muscles m ON m.exercise_id = e.id
        WHERE 1=1
    """
    params: List[Any] = []
    if muscle:
        sql += " AND m.muscle = ? AND m.is_primary = 1"
        params.append(muscle.lower().strip())
    if level:
        sql += " AND e.level = ?"
        params.append(level.lower().strip())
    equipment_list = _normalize_equipment(equipment)
    if equipment_list:
        placeholders = ", ".join("?" for _ in equipment_list)
        sql += f" AND e.equipment IN ({placeholders})"
        params.extend(equipment_list)
    if exclude_equipment:
        if isinstance(exclude_equipment, str):
            exclude_equipment = [exclude_equipment]
        excluded = [e.lower().strip() for e in exclude_equipment if e and e.strip()]
        if excluded:
            placeholders = ", ".join("?" for _ in excluded)
            # NULL-safe: rows with no equipment (bodyweight-ish) must survive a
            # NOT IN filter, which plain SQL three-valued logic would drop.
            sql += f" AND (e.equipment IS NULL OR e.equipment NOT IN ({placeholders}))"
            params.extend(excluded)
    if category:
        sql += " AND e.category = ?"
        params.append(category.lower().strip())
    if keyword:
        sql += " AND e.name LIKE ?"
        params.append(f"%{keyword.strip()}%")
    if exclude_muscles:
        placeholders = ", ".join("?" for _ in exclude_muscles)
        sql += (
            " AND NOT EXISTS (SELECT 1 FROM exercise_muscles x "
            f"WHERE x.exercise_id = e.id AND x.muscle IN ({placeholders}))"
        )
        params.extend(m.lower().strip() for m in exclude_muscles)
    if exclude_categories:
        placeholders = ", ".join("?" for _ in exclude_categories)
        sql += f" AND e.category NOT IN ({placeholders})"
        params.extend(c.lower().strip() for c in exclude_categories)
    for rule in exclude_force_rules or []:
        force_ph = ", ".join("?" for _ in rule["forces"])
        muscle_ph = ", ".join("?" for _ in rule["muscles"])
        sql += (
            f" AND NOT (e.force IN ({force_ph}) AND EXISTS ("
            "SELECT 1 FROM exercise_muscles x WHERE x.exercise_id = e.id "
            f"AND x.muscle IN ({muscle_ph})))"
        )
        params.extend(rule["forces"])
        params.extend(rule["muscles"])
    sql += " ORDER BY e.level, e.name LIMIT ?"
    params.append(max_results)

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            muscles = conn.execute(
                "SELECT muscle, is_primary FROM exercise_muscles WHERE exercise_id = ?",
                (row["id"],),
            ).fetchall()
            results.append(
                {
                    "name": row["name"],
                    "level": row["level"],
                    "equipment": row["equipment"],
                    "category": row["category"],
                    "mechanic": row["mechanic"],
                    "force": row["force"],
                    "primary_muscles": [m["muscle"] for m in muscles if m["is_primary"]],
                    "secondary_muscles": [m["muscle"] for m in muscles if not m["is_primary"]],
                    "instructions": row["instructions"],
                }
            )
    return results


def _bmi_notes(weight_kg: Optional[float], height_cm: Optional[float]) -> tuple:
    """Return (extra category exclusions, advisory notes) from body metrics."""
    if not weight_kg or not height_cm:
        return [], []
    if weight_kg <= 0 or height_cm <= 0:
        raise ValueError("weight_kg and height_cm must be positive")
    bmi = weight_kg / (height_cm / 100) ** 2
    notes = [f"BMI ~{bmi:.1f} (rough screen only, not a diagnosis)."]
    exclusions: List[str] = []
    if bmi >= 30:
        exclusions.append("plyometrics")
        notes.append(
            "High-impact jumping work excluded; favoring low-impact strength "
            "work to protect joints while building fitness."
        )
    elif bmi < 18.5:
        notes.append(
            "BMI is on the low side — pair training with adequate calories and "
            "protein (the diet tools can help plan this)."
        )
    return exclusions, notes


def _pick_exercises(
    muscle: str,
    level: str,
    equipment: Union[str, List[str], None],
    exclude_muscles: List[str],
    exclude_categories: List[str],
    exclude_force_rules: List[dict],
    count: int,
    used_names: set,
    exclude_equipment: Union[str, List[str], None] = None,
) -> List[dict]:
    """Pick exercises for a muscle, preferring compounds and week-level variety."""
    candidates = exercise_search(
        muscle=muscle,
        level=level,
        equipment=equipment,
        exclude_muscles=exclude_muscles,
        exclude_categories=exclude_categories,
        exclude_force_rules=exclude_force_rules,
        exclude_equipment=exclude_equipment,
        max_results=count * 5,
    )
    if not candidates and equipment:
        candidates = exercise_search(
            muscle=muscle,
            level=level,
            equipment="body only",
            exclude_muscles=exclude_muscles,
            exclude_categories=exclude_categories,
            exclude_force_rules=exclude_force_rules,
            exclude_equipment=exclude_equipment,
            max_results=count * 5,
        )
    compound = [e for e in candidates if e["mechanic"] == "compound"]
    isolation = [e for e in candidates if e["mechanic"] != "compound"]
    ordered = compound + isolation
    fresh = [e for e in ordered if e["name"] not in used_names]
    picks = (fresh + [e for e in ordered if e["name"] in used_names])[:count]
    used_names.update(e["name"] for e in picks)
    return picks


def _pick_cardio_exercises(
    level: str,
    equipment: Union[str, List[str], None],
    exclude_muscles: List[str],
    exclude_categories: List[str],
    exclude_force_rules: List[dict],
    count: int,
    used_names: set,
    exclude_equipment: Union[str, List[str], None] = None,
) -> List[dict]:
    """Pick grounded cardio exercises under the same safety constraints."""
    candidates = exercise_search(
        level=level,
        equipment=equipment,
        category="cardio",
        exclude_muscles=exclude_muscles,
        exclude_categories=exclude_categories,
        exclude_force_rules=exclude_force_rules,
        exclude_equipment=exclude_equipment,
        max_results=count * 5,
    )
    if not candidates and equipment:
        candidates = exercise_search(
            level=level,
            equipment="body only",
            category="cardio",
            exclude_muscles=exclude_muscles,
            exclude_categories=exclude_categories,
            exclude_force_rules=exclude_force_rules,
            exclude_equipment=exclude_equipment,
            max_results=count * 5,
        )
    fresh = [exercise for exercise in candidates if exercise["name"] not in used_names]
    picks = (
        fresh
        + [exercise for exercise in candidates if exercise["name"] in used_names]
    )[:count]
    used_names.update(exercise["name"] for exercise in picks)
    return picks


def build_workout(
    muscles: List[str],
    level: str = "beginner",
    equipment: Union[str, List[str], None] = None,
    injuries: Optional[List[str]] = None,
    exercises_per_muscle: int = 2,
    exclude_equipment: Union[str, List[str], None] = None,
) -> dict:
    """Assemble a single-session workout: compound lifts first, then isolation."""
    exclusions = resolve_injury_exclusions(injuries)
    session: List[dict] = []
    missing: List[str] = []
    skipped_injured = [m for m in muscles if m.lower().strip() in exclusions["muscles"]]
    used: set = set()
    for muscle in muscles:
        muscle_key = muscle.lower().strip()
        if muscle_key in exclusions["muscles"]:
            continue
        picks = _pick_exercises(
            muscle_key, level, equipment,
            exclusions["muscles"], exclusions["categories"],
            exclusions["force_rules"], exercises_per_muscle, used,
            exclude_equipment=exclude_equipment,
        )
        if not picks:
            missing.append(muscle)
            continue
        for ex in picks:
            session.append(
                {
                    "muscle": muscle_key,
                    "name": ex["name"],
                    "mechanic": ex["mechanic"],
                    "equipment": ex["equipment"],
                    "suggested_sets": 3,
                    "suggested_reps": "8-12" if ex["mechanic"] == "compound" else "10-15",
                }
            )
    return {
        "session": session,
        "muscles_without_matches": missing,
        "muscles_skipped_for_injury": skipped_injured,
        "injury_interpretation": format_injury_interpretation(exclusions["mapped"]),
        "injury_disclaimer": INJURY_DISCLAIMER if injuries else None,
    }


def build_weekly_plan(
    days_per_week: int,
    level: str = "beginner",
    equipment: Union[str, List[str], None] = None,
    injuries: Optional[List[str]] = None,
    weight_kg: Optional[float] = None,
    height_cm: Optional[float] = None,
    exercises_per_muscle: int = 2,
    split: Optional[str] = None,
    exclude_equipment: Union[str, List[str], None] = None,
) -> dict:
    """Build a grounded weekly plan from a named or default database split."""
    if days_per_week < 1:
        raise ValueError("days_per_week must be at least 1")
    if days_per_week > 6:
        raise ValueError(
            "Plans are capped at 6 training days: at least one full rest day per "
            "week is required for recovery."
        )
    selected_split = _select_split(days_per_week, split)
    exclusions = resolve_injury_exclusions(injuries)
    bmi_exclusions, notes = _bmi_notes(weight_kg, height_cm)
    exclude_categories = sorted(set(exclusions["categories"] + bmi_exclusions))

    days = []
    used: set = set()
    all_skipped: set = set()
    no_safe_match: set = set()
    for split_day in selected_split["days"]:
        day_name = split_day["day"]
        day_muscles = split_day["muscles"]
        trainable = [m for m in day_muscles if m not in exclusions["muscles"]]
        all_skipped.update(m for m in day_muscles if m in exclusions["muscles"])
        session = []
        if split_day["is_cardio"]:
            cardio_picks = _pick_cardio_exercises(
                level,
                equipment,
                exclusions["muscles"],
                exclude_categories,
                exclusions["force_rules"],
                exercises_per_muscle,
                used,
                exclude_equipment=exclude_equipment,
            )
            if not cardio_picks:
                no_safe_match.add("cardio")
            for ex in cardio_picks:
                session.append(
                    {
                        "muscle": "cardio",
                        "name": ex["name"],
                        "mechanic": ex["mechanic"],
                        "equipment": ex["equipment"],
                        "category": ex["category"],
                        "suggested_sets": 1,
                        "suggested_reps": "20-30 minutes",
                    }
                )
        for muscle in trainable:
            picks = _pick_exercises(
                muscle, level, equipment,
                exclusions["muscles"], exclude_categories,
                exclusions["force_rules"], exercises_per_muscle, used,
                exclude_equipment=exclude_equipment,
            )
            if not picks:
                no_safe_match.add(muscle)
            for ex in picks:
                session.append(
                    {
                        "muscle": muscle,
                        "name": ex["name"],
                        "mechanic": ex["mechanic"],
                        "equipment": ex["equipment"],
                        "category": ex["category"],
                        "suggested_sets": 3,
                        "suggested_reps": "8-12" if ex["mechanic"] == "compound" else "10-15",
                    }
                )
        days.append(
            {
                "day": day_name,
                "is_cardio": split_day["is_cardio"],
                "exercises": session,
            }
        )

    interpretation = format_injury_interpretation(exclusions["mapped"])
    if interpretation:
        notes.append(interpretation)
    if injuries:
        notes.append(INJURY_DISCLAIMER)
    if all_skipped:
        notes.append(
            "Muscle groups skipped due to reported injuries: "
            + ", ".join(sorted(all_skipped))
        )
    if no_safe_match:
        notes.append(
            "No safe exercises found for: "
            + ", ".join(sorted(no_safe_match))
            + (
                " (movements through the injured joint's load path are excluded)"
                if exclusions["force_rules"]
                else " (given the level/equipment constraints)"
            )
        )
    return {
        "split_id": selected_split["id"],
        "split_name": selected_split["name"],
        "days_per_week": days_per_week,
        "days": days,
        "notes": notes,
    }


def one_rep_max(weight: float, reps: int) -> dict:
    """Estimate one-rep max with the Epley and Brzycki formulas."""
    if weight <= 0 or reps <= 0:
        raise ValueError("weight and reps must be positive")
    if reps > 15:
        raise ValueError("1RM estimates are unreliable above ~15 reps")
    epley = weight * (1 + reps / 30)
    brzycki = weight * 36 / (37 - reps)
    return {
        "epley_1rm": round(epley, 1),
        "brzycki_1rm": round(brzycki, 1),
        "average_1rm": round((epley + brzycki) / 2, 1),
    }


# --- LangChain tool wrappers (same pattern as the diet agent) ---

def _format_session(session: List[dict], start: int = 1) -> List[str]:
    lines = []
    for i, ex in enumerate(session, start):
        details = (
            f"{i}. {ex['name']} ({ex['muscle']}, {ex['mechanic'] or 'n/a'}, "
            f"{ex['equipment'] or 'no equipment'}) — "
        )
        if ex.get("category") == "cardio":
            details += ex["suggested_reps"]
        else:
            details += f"{ex['suggested_sets']} sets x {ex['suggested_reps']} reps"
        lines.append(details)
    return lines


class ExerciseSearchArgs(BaseModel):
    muscle: Optional[str] = Field(
        None, description=f"Primary muscle group to target. One of: {', '.join(MUSCLE_GROUPS)}"
    )
    level: Optional[str] = Field(None, description="beginner, intermediate, or expert")
    equipment: Optional[List[str]] = Field(
        None,
        description=(
            "Equipment the user has available (bodyweight exercises are always "
            f"included). Valid values: {', '.join(EQUIPMENT)}"
        ),
    )
    exclude_equipment: Optional[List[str]] = Field(
        None,
        description=(
            "Equipment the user does NOT have or wants to avoid — use this when the "
            "user describes equipment by exclusion, e.g. 'everything except barbell' "
            f"-> exclude_equipment=['barbell']. Valid values: {', '.join(EQUIPMENT)}"
        ),
    )
    category: Optional[str] = Field(
        None, description=f"Exercise category. One of: {', '.join(CATEGORIES)}"
    )
    keyword: Optional[str] = Field(None, description="Keyword to match in the exercise name")
    injuries: Optional[List[str]] = Field(
        None,
        description=(
            "User-reported injuries; exercises touching these areas are excluded. "
            "Pass the user's words (specific terms like 'ACL' or 'rotator cuff' "
            f"are recognized). Body areas: {', '.join(INJURY_EXCLUSIONS)}"
        ),
    )
    max_results: int = Field(5, description="Maximum number of exercises to return")


class ExerciseSearchTool(BaseTool):
    name: ClassVar[str] = "exercise_search"
    description: ClassVar[str] = (
        "Search the local exercise database (873 exercises) by muscle group, "
        "difficulty level, available equipment, category, or name keyword, "
        "optionally excluding injured areas. Returns exercise details including "
        "step-by-step instructions. Use this instead of guessing exercises. "
        "At least one filter is required."
    )
    args_schema: ClassVar[type[ExerciseSearchArgs]] = ExerciseSearchArgs

    def _run(
        self,
        muscle: Optional[str] = None,
        level: Optional[str] = None,
        equipment: Optional[List[str]] = None,
        category: Optional[str] = None,
        keyword: Optional[str] = None,
        injuries: Optional[List[str]] = None,
        exclude_equipment: Optional[List[str]] = None,
        max_results: Any = 5,
    ) -> str:
        try:
            max_results = int(max_results)
        except (ValueError, TypeError):
            max_results = 5
        if not any([muscle, level, equipment, category, keyword, exclude_equipment]):
            return (
                "Provide at least one filter. Valid muscle groups: "
                + ", ".join(MUSCLE_GROUPS)
            )
        if muscle and muscle.lower().strip() not in MUSCLE_GROUPS:
            return (
                f"Unknown muscle group '{muscle}'. Valid options: "
                + ", ".join(MUSCLE_GROUPS)
            )
        try:
            exclusions = resolve_injury_exclusions(injuries)
            results = exercise_search(
                muscle, level, equipment, category, keyword,
                exclusions["muscles"], exclusions["categories"],
                exclusions["force_rules"], exclude_equipment, max_results,
            )
        except Exception as exc:
            return f"Exercise search error: {exc}"
        if not results:
            msg = "No exercises matched those filters."
            if injuries and muscle and muscle.lower().strip() in exclusions["muscles"]:
                msg = (
                    f"No exercises returned: '{muscle}' is excluded by the reported "
                    "injuries."
                )
            else:
                msg += " Try relaxing level or equipment."
            if injuries:
                msg += " " + INJURY_DISCLAIMER
            return msg
        lines = []
        for ex in results:
            lines.append(
                f"Name: {ex['name']}\n"
                f"Level: {ex['level']} | Equipment: {ex['equipment']} | "
                f"Mechanic: {ex['mechanic']} | Category: {ex['category']}\n"
                f"Primary muscles: {', '.join(ex['primary_muscles'])}\n"
                f"Secondary muscles: {', '.join(ex['secondary_muscles']) or 'none'}\n"
                f"Instructions: {ex['instructions']}"
            )
            lines.append("---")
        if injuries:
            interpretation = format_injury_interpretation(exclusions["mapped"])
            if interpretation:
                lines.append(interpretation)
            lines.append(INJURY_DISCLAIMER)
        return "\n".join(lines).strip()


class BuildWorkoutArgs(BaseModel):
    muscles: List[str] = Field(
        ..., description=f"Muscle groups to train this session. Valid: {', '.join(MUSCLE_GROUPS)}"
    )
    level: str = Field("beginner", description="beginner, intermediate, or expert")
    equipment: Optional[List[str]] = Field(
        None,
        description=(
            "Equipment the user has available (bodyweight is always included). "
            f"Valid values: {', '.join(EQUIPMENT)}"
        ),
    )
    exclude_equipment: Optional[List[str]] = Field(
        None,
        description=(
            "Equipment the user does NOT have or wants to avoid — use this when the "
            "user describes equipment by exclusion, e.g. 'everything except barbell' "
            f"-> exclude_equipment=['barbell']. Valid values: {', '.join(EQUIPMENT)}"
        ),
    )
    injuries: Optional[List[str]] = Field(
        None,
        description=(
            "User-reported injuries to work around; pass the user's words "
            "(specific terms like 'ACL', 'rotator cuff', 'herniated disc' are "
            f"recognized). Body areas: {', '.join(INJURY_EXCLUSIONS)}"
        ),
    )
    exercises_per_muscle: int = Field(2, description="Exercises per muscle group (1-4)")


class BuildWorkoutTool(BaseTool):
    name: ClassVar[str] = "build_workout"
    description: ClassVar[str] = (
        "Assemble a single workout session from the local exercise database for the "
        "given muscle groups, difficulty level, available equipment, and any "
        "injuries to work around. Orders compound lifts before isolation work and "
        "suggests sets/reps. Use this to draft one session; use build_weekly_plan "
        "for a full week. This is general fitness information, not medical advice."
    )
    args_schema: ClassVar[type[BuildWorkoutArgs]] = BuildWorkoutArgs

    def _run(
        self,
        muscles: List[str],
        level: str = "beginner",
        equipment: Optional[List[str]] = None,
        injuries: Optional[List[str]] = None,
        exercises_per_muscle: Any = 2,
        exclude_equipment: Optional[List[str]] = None,
    ) -> str:
        try:
            exercises_per_muscle = max(1, min(4, int(exercises_per_muscle)))
        except (ValueError, TypeError):
            exercises_per_muscle = 2
        invalid = [m for m in muscles if m.lower().strip() not in MUSCLE_GROUPS]
        if invalid:
            return (
                f"Unknown muscle groups: {', '.join(invalid)}. Valid options: "
                + ", ".join(MUSCLE_GROUPS)
            )
        try:
            plan = build_workout(
                muscles, level, equipment, injuries, exercises_per_muscle,
                exclude_equipment=exclude_equipment,
            )
        except Exception as exc:
            return f"Workout builder error: {exc}"
        if not plan["session"]:
            return (
                "No exercises matched (all requested muscles may be excluded by the "
                "reported injuries). " + (plan["injury_disclaimer"] or "")
            ).strip()
        lines = _format_session(plan["session"])
        if plan["injury_interpretation"]:
            lines.append(plan["injury_interpretation"])
        if plan["muscles_skipped_for_injury"]:
            lines.append(
                "Skipped due to reported injury: "
                + ", ".join(plan["muscles_skipped_for_injury"])
            )
        if plan["muscles_without_matches"]:
            lines.append(
                "No matches found for: " + ", ".join(plan["muscles_without_matches"])
            )
        if plan["injury_disclaimer"]:
            lines.append(plan["injury_disclaimer"])
        return "\n".join(lines)


class BuildWeeklyPlanArgs(BaseModel):
    days_per_week: int = Field(
        ..., description="Training days per week (1-6; at least one rest day is enforced)"
    )
    split: Optional[str] = Field(
        None,
        description=(
            "Optional named workout split ID. Omit to use the default for the "
            f"requested frequency. Valid IDs: {', '.join(load_splits())}"
        ),
    )
    level: str = Field("beginner", description="beginner, intermediate, or expert")
    equipment: Optional[List[str]] = Field(
        None,
        description=(
            "Equipment/machines the user has available (bodyweight is always "
            f"included). Valid values: {', '.join(EQUIPMENT)}"
        ),
    )
    exclude_equipment: Optional[List[str]] = Field(
        None,
        description=(
            "Equipment the user does NOT have or wants to avoid — use this when the "
            "user describes equipment by exclusion, e.g. 'everything except barbell' "
            f"-> exclude_equipment=['barbell']. Valid values: {', '.join(EQUIPMENT)}"
        ),
    )
    injuries: Optional[List[str]] = Field(
        None,
        description=(
            "User-reported injuries to work around; pass the user's words "
            "(specific terms like 'ACL', 'rotator cuff', 'herniated disc' are "
            f"recognized). Body areas: {', '.join(INJURY_EXCLUSIONS)}"
        ),
    )
    weight_kg: Optional[float] = Field(
        None, description="User's body weight in kg (used with height_cm for a BMI-based screen)"
    )
    height_cm: Optional[float] = Field(
        None, description="User's height in cm (used with weight_kg for a BMI-based screen)"
    )
    exercises_per_muscle: int = Field(2, description="Exercises per muscle group per day (1-3)")


class BuildWeeklyPlanTool(BaseTool):
    name: ClassVar[str] = "build_weekly_plan"
    description: ClassVar[str] = (
        "Build a weekly training plan from the local exercise database, respecting "
        "how many days per week the user can train (1-6), what equipment/machines "
        "they have, reported injuries (affected areas are excluded), and optional "
        "body metrics (weight/height, used as a rough BMI screen to avoid "
        "high-impact work when appropriate). Supports named, database-grounded "
        "strength/cardio splits and uses the frequency default when no split is "
        "requested. This is general fitness information, not medical advice."
    )
    args_schema: ClassVar[type[BuildWeeklyPlanArgs]] = BuildWeeklyPlanArgs

    def _run(
        self,
        days_per_week: Any,
        level: str = "beginner",
        equipment: Optional[List[str]] = None,
        injuries: Optional[List[str]] = None,
        weight_kg: Any = None,
        height_cm: Any = None,
        exercises_per_muscle: Any = 2,
        split: Optional[str] = None,
        exclude_equipment: Optional[List[str]] = None,
    ) -> str:
        try:
            days_per_week = int(days_per_week)
        except (ValueError, TypeError):
            return "days_per_week must be a number between 1 and 6."
        try:
            exercises_per_muscle = max(1, min(3, int(exercises_per_muscle)))
        except (ValueError, TypeError):
            exercises_per_muscle = 2
        try:
            weight_kg = float(weight_kg) if weight_kg is not None else None
            height_cm = float(height_cm) if height_cm is not None else None
        except (ValueError, TypeError):
            return "weight_kg and height_cm must be numbers (or omitted)."
        try:
            plan = build_weekly_plan(
                days_per_week=days_per_week,
                level=level,
                equipment=equipment,
                injuries=injuries,
                weight_kg=weight_kg,
                height_cm=height_cm,
                exercises_per_muscle=exercises_per_muscle,
                split=split,
                exclude_equipment=exclude_equipment,
            )
        except Exception as exc:
            return f"Weekly plan error: {exc}"
        lines = [
            f"Weekly plan: {plan['split_name']} "
            f"({plan['days_per_week']} training day(s))"
        ]
        for day in plan["days"]:
            lines.append(f"\n{day['day']}:")
            if day["exercises"]:
                lines.extend("  " + s for s in _format_session(day["exercises"]))
            else:
                lines.append("  (no exercises available for this day's muscles)")
        if plan["notes"]:
            lines.append("\nNotes:")
            lines.extend("- " + n for n in plan["notes"])
        return "\n".join(lines)


class OneRepMaxArgs(BaseModel):
    weight: float = Field(..., description="Weight lifted (any unit; result is in the same unit)")
    reps: int = Field(..., description="Reps completed at that weight (1-15)")


class OneRepMaxTool(BaseTool):
    name: ClassVar[str] = "one_rep_max_calculator"
    description: ClassVar[str] = (
        "Estimate one-rep max (1RM) from a weight and rep count using the Epley and "
        "Brzycki formulas. Valid for 1-15 reps. This is an estimate for programming "
        "purposes, not medical advice."
    )
    args_schema: ClassVar[type[OneRepMaxArgs]] = OneRepMaxArgs

    def _run(self, weight: Any, reps: Any) -> str:
        try:
            result = one_rep_max(float(weight), int(reps))
        except (ValueError, TypeError) as exc:
            return f"Error: {exc}"
        return (
            f"Estimated 1RM for {weight} x {reps} reps: "
            f"Epley {result['epley_1rm']}, Brzycki {result['brzycki_1rm']}, "
            f"average {result['average_1rm']} (same unit as input)."
        )


def get_workout_tools() -> List[BaseTool]:
    """All workout tools, ready to append to the diet agent's tool list."""
    return [
        ExerciseSearchTool(),
        BuildWorkoutTool(),
        BuildWeeklyPlanTool(),
        OneRepMaxTool(),
    ]
