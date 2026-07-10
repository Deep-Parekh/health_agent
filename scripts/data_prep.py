"""Build the local workout SQLite database from free-exercise-db.

Downloads the public-domain exercise dataset (https://github.com/yuhonas/free-exercise-db)
and loads it into data/workouts.db with a normalized schema so the agent's tools
can run real SQL queries (filter by muscle group, level, equipment, category).

Usage:
    python data_prep.py
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path

DATASET_URL = (
    "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/dist/exercises.json"
)
IMAGE_BASE_URL = (
    "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/"
)

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "workouts.db"
RAW_JSON_PATH = DATA_DIR / "exercises_raw.json"

# Split definitions use the dataset's canonical muscle names.  Compound-pattern
# coverage is broader than the primary target list (for example, chest pressing
# also trains triceps); the coverage map makes that guarantee explicit and
# testable without turning a one-day full-body plan into 20+ isolation slots.
PUSH = ["chest", "shoulders", "triceps"]
PULL = ["lats", "middle back", "biceps", "forearms"]
LEGS = ["quadriceps", "hamstrings", "glutes", "calves"]
UPPER = ["chest", "lats", "middle back", "shoulders", "biceps", "triceps"]
LOWER = ["quadriceps", "hamstrings", "glutes", "calves"]
FULL = ["chest", "lats", "biceps", "quadriceps", "hamstrings", "shoulders"]
CORE = ["abdominals"]

REQUIRED_WEEKLY_MUSCLES = {
    "chest",
    "shoulders",
    "triceps",
    "biceps",
    "lats",
    "middle back",
    "quadriceps",
    "hamstrings",
    "glutes",
    "calves",
    "abdominals",
}
SECONDARY_COVERAGE = {
    "chest": {"triceps"},
    "lats": {"biceps", "middle back"},
    "quadriceps": {"glutes", "calves"},
}

SPLIT_CATALOG = [
    {
        "id": "full_body",
        "name": "Full Body",
        "days_per_week": 1,
        "is_default": True,
        "description": "One full-body strength session with core.",
        "days": [("Full Body + Core", FULL + CORE, False)],
    },
    {
        "id": "full_body_ab",
        "name": "Full Body A/B",
        "days_per_week": 2,
        "is_default": True,
        "description": "Two full-body sessions with week-level exercise variety.",
        "days": [
            ("Full Body A + Core", FULL + CORE, False),
            ("Full Body B + Core", FULL + CORE, False),
        ],
    },
    {
        "id": "upper_lower_2",
        "name": "Upper / Lower",
        "days_per_week": 2,
        "is_default": False,
        "description": "One upper- and one lower-body session, both with core.",
        "days": [
            ("Upper + Core", UPPER + CORE, False),
            ("Lower + Core", LOWER + CORE, False),
        ],
    },
    {
        "id": "ppl",
        "name": "Push / Pull / Legs",
        "days_per_week": 3,
        "is_default": True,
        "description": "Classic push, pull, and legs split with core mixed in.",
        "days": [
            ("Push + Core", PUSH + CORE, False),
            ("Pull", PULL, False),
            ("Legs + Core", LEGS + CORE, False),
        ],
    },
    {
        "id": "ul_cardio",
        "name": "Upper / Lower / Cardio",
        "days_per_week": 3,
        "is_default": False,
        "description": "Upper and lower strength days plus a cardio-and-core day.",
        "days": [
            ("Upper + Core", UPPER + CORE, False),
            ("Lower", LOWER, False),
            ("Cardio + Core", CORE, True),
        ],
    },
    {
        "id": "full_body_3",
        "name": "Full Body x3",
        "days_per_week": 3,
        "is_default": False,
        "description": "Three full-body sessions with deduplicated exercise picks.",
        "days": [
            ("Full Body A + Core", FULL + CORE, False),
            ("Full Body B + Core", FULL + CORE, False),
            ("Full Body C + Core", FULL + CORE, False),
        ],
    },
    {
        "id": "upper_lower_4",
        "name": "Upper / Lower x2",
        "days_per_week": 4,
        "is_default": True,
        "description": "Two upper- and two lower-body sessions.",
        "days": [
            ("Upper A + Core", UPPER + CORE, False),
            ("Lower A", LOWER, False),
            ("Upper B", UPPER, False),
            ("Lower B + Core", LOWER + CORE, False),
        ],
    },
    {
        "id": "ppl_upper",
        "name": "PPL + Upper",
        "days_per_week": 4,
        "is_default": False,
        "description": "Push, pull, and legs followed by a second upper-body day.",
        "days": [
            ("Push + Core", PUSH + CORE, False),
            ("Pull", PULL, False),
            ("Legs + Core", LEGS + CORE, False),
            ("Upper", UPPER, False),
        ],
    },
    {
        "id": "ulu_cardio",
        "name": "Upper / Lower / Upper / Cardio",
        "days_per_week": 4,
        "is_default": False,
        "description": "Two upper days, one lower day, and one cardio-and-core day.",
        "days": [
            ("Upper A + Core", UPPER + CORE, False),
            ("Lower", LOWER, False),
            ("Upper B", UPPER, False),
            ("Cardio + Core", CORE, True),
        ],
    },
    {
        "id": "ppl_upper_lower",
        "name": "PPL + Upper / Lower",
        "days_per_week": 5,
        "is_default": True,
        "description": "Push, pull, and legs plus a second upper/lower block.",
        "days": [
            ("Push", PUSH, False),
            ("Pull + Core", PULL + CORE, False),
            ("Legs", LEGS, False),
            ("Upper", UPPER, False),
            ("Lower + Core", LOWER + CORE, False),
        ],
    },
    {
        "id": "upper_lower_cardio_5",
        "name": "Upper / Lower x2 + Cardio",
        "days_per_week": 5,
        "is_default": False,
        "description": "Two upper/lower blocks plus a cardio-and-core day.",
        "days": [
            ("Upper A + Core", UPPER + CORE, False),
            ("Lower A", LOWER, False),
            ("Upper B", UPPER, False),
            ("Lower B + Core", LOWER + CORE, False),
            ("Cardio + Core", CORE, True),
        ],
    },
    {
        "id": "ppl_6",
        "name": "PPL x2",
        "days_per_week": 6,
        "is_default": True,
        "description": "Two complete push/pull/legs rotations.",
        "days": [
            ("Push A", PUSH, False),
            ("Pull A + Core", PULL + CORE, False),
            ("Legs A", LEGS, False),
            ("Push B", PUSH, False),
            ("Pull B + Core", PULL + CORE, False),
            ("Legs B", LEGS, False),
        ],
    },
    {
        "id": "ppl_ul_cardio",
        "name": "PPL + UL + Cardio",
        "days_per_week": 6,
        "is_default": False,
        "description": "Push/pull/legs, upper/lower, and a cardio-and-core day.",
        "days": [
            ("Push + Core", PUSH + CORE, False),
            ("Pull", PULL, False),
            ("Legs", LEGS, False),
            ("Upper", UPPER, False),
            ("Lower + Core", LOWER + CORE, False),
            ("Cardio + Core", CORE, True),
        ],
    },
]

SCHEMA = """
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS split_days;
DROP TABLE IF EXISTS splits;
DROP TABLE IF EXISTS exercise_muscles;
DROP TABLE IF EXISTS exercises;

CREATE TABLE exercises (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    force        TEXT,              -- push / pull / static
    level        TEXT NOT NULL,     -- beginner / intermediate / expert
    mechanic     TEXT,              -- compound / isolation
    equipment    TEXT,              -- barbell, dumbbell, body only, ...
    category     TEXT NOT NULL,     -- strength, cardio, stretching, ...
    instructions TEXT NOT NULL,     -- newline-joined steps
    images       TEXT               -- newline-joined hosted image URLs
);

CREATE TABLE exercise_muscles (
    exercise_id TEXT NOT NULL REFERENCES exercises(id),
    muscle      TEXT NOT NULL,      -- abdominals, biceps, chest, ...
    is_primary  INTEGER NOT NULL    -- 1 = primary target, 0 = secondary
);

CREATE INDEX idx_muscle ON exercise_muscles(muscle, is_primary);
CREATE INDEX idx_level ON exercises(level);
CREATE INDEX idx_equipment ON exercises(equipment);
CREATE INDEX idx_category ON exercises(category);

CREATE TABLE splits (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    days_per_week INTEGER NOT NULL CHECK(days_per_week BETWEEN 1 AND 6),
    is_default    INTEGER NOT NULL CHECK(is_default IN (0, 1)),
    description   TEXT NOT NULL
);

CREATE TABLE split_days (
    split_id  TEXT NOT NULL REFERENCES splits(id) ON DELETE CASCADE,
    day_order INTEGER NOT NULL,
    day_name  TEXT NOT NULL,
    muscles   TEXT NOT NULL,
    is_cardio INTEGER NOT NULL DEFAULT 0 CHECK(is_cardio IN (0, 1)),
    PRIMARY KEY (split_id, day_order)
);

CREATE INDEX idx_splits_frequency ON splits(days_per_week);
CREATE UNIQUE INDEX idx_default_split_per_frequency
    ON splits(days_per_week) WHERE is_default = 1;
"""


def download_dataset() -> list[dict]:
    if RAW_JSON_PATH.exists():
        print(f"Using cached {RAW_JSON_PATH}")
        return json.loads(RAW_JSON_PATH.read_text())
    print(f"Downloading {DATASET_URL} ...")
    with urllib.request.urlopen(DATASET_URL) as resp:
        raw = resp.read()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_JSON_PATH.write_bytes(raw)
    return json.loads(raw)


def split_weekly_coverage(split: dict) -> set[str]:
    """Return direct plus explicit compound-pattern muscle coverage."""
    direct = {
        muscle
        for _, muscles, _ in split["days"]
        for muscle in muscles
    }
    covered = set(direct)
    for muscle in direct:
        covered.update(SECONDARY_COVERAGE.get(muscle, set()))
    return covered


def validate_split_catalog() -> None:
    """Fail the database build if the catalog loses a structural guarantee."""
    ids = [split["id"] for split in SPLIT_CATALOG]
    if len(ids) != len(set(ids)):
        raise ValueError("Split IDs must be unique")

    for frequency in range(1, 7):
        options = [s for s in SPLIT_CATALOG if s["days_per_week"] == frequency]
        defaults = [s for s in options if s["is_default"]]
        if len(defaults) != 1:
            raise ValueError(
                f"Expected one default split for {frequency} day(s), found {len(defaults)}"
            )

    for split in SPLIT_CATALOG:
        if len(split["days"]) != split["days_per_week"]:
            raise ValueError(f"{split['id']} day count does not match its frequency")
        coverage = split_weekly_coverage(split)
        missing = REQUIRED_WEEKLY_MUSCLES - coverage
        if missing:
            raise ValueError(f"{split['id']} lacks weekly coverage for {sorted(missing)}")
        core_days = sum("abdominals" in muscles for _, muscles, _ in split["days"])
        minimum_core_days = 1 if split["days_per_week"] == 1 else 2
        if core_days < minimum_core_days:
            raise ValueError(
                f"{split['id']} has core on {core_days} day(s); "
                f"requires {minimum_core_days}"
            )


def build_db(exercises: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    for ex in exercises:
        conn.execute(
            "INSERT INTO exercises VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ex["id"],
                ex["name"],
                ex.get("force"),
                ex["level"],
                ex.get("mechanic"),
                ex.get("equipment"),
                ex["category"],
                "\n".join(ex.get("instructions", [])),
                "\n".join(IMAGE_BASE_URL + img for img in ex.get("images", [])),
            ),
        )
        for muscle in ex.get("primaryMuscles", []):
            conn.execute(
                "INSERT INTO exercise_muscles VALUES (?, ?, 1)", (ex["id"], muscle)
            )
        for muscle in ex.get("secondaryMuscles", []):
            conn.execute(
                "INSERT INTO exercise_muscles VALUES (?, ?, 0)", (ex["id"], muscle)
            )

    validate_split_catalog()
    for split in SPLIT_CATALOG:
        conn.execute(
            "INSERT INTO splits VALUES (?, ?, ?, ?, ?)",
            (
                split["id"],
                split["name"],
                split["days_per_week"],
                int(split["is_default"]),
                split["description"],
            ),
        )
        conn.executemany(
            "INSERT INTO split_days VALUES (?, ?, ?, ?, ?)",
            [
                (
                    split["id"],
                    order,
                    day_name,
                    json.dumps(muscles),
                    int(is_cardio),
                )
                for order, (day_name, muscles, is_cardio) in enumerate(
                    split["days"], start=1
                )
            ],
        )

    conn.commit()

    n_ex = conn.execute("SELECT COUNT(*) FROM exercises").fetchone()[0]
    n_muscles = conn.execute(
        "SELECT COUNT(DISTINCT muscle) FROM exercise_muscles"
    ).fetchone()[0]
    print(f"Built {DB_PATH}: {n_ex} exercises, {n_muscles} distinct muscles")

    print("\nExercises per primary muscle group:")
    for muscle, count in conn.execute(
        "SELECT muscle, COUNT(*) FROM exercise_muscles WHERE is_primary = 1 "
        "GROUP BY muscle ORDER BY COUNT(*) DESC"
    ):
        print(f"  {muscle:<15} {count}")
    print("\nWorkout split catalog:")
    for days, count in conn.execute(
        "SELECT days_per_week, COUNT(*) FROM splits "
        "GROUP BY days_per_week ORDER BY days_per_week"
    ):
        default_name = conn.execute(
            "SELECT name FROM splits WHERE days_per_week = ? AND is_default = 1",
            (days,),
        ).fetchone()[0]
        print(f"  {days} day(s): {count} split(s), default = {default_name}")
    conn.close()


if __name__ == "__main__":
    build_db(download_dataset())
