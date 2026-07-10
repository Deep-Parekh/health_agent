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

SCHEMA = """
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
    conn.close()


if __name__ == "__main__":
    build_db(download_dataset())
