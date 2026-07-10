# Workout Split Catalog — Implementation Plan

**Status (2026-07-10): complete.** Implemented independently of deployment with zero
changes to `agents.py`, `app.py`, or `memory.py`, no new tools, and no JSONL schema
changes.

## Goal

Replace the single hardcoded split per frequency (the `SPLITS` dict in
`healthva/workout_tools.py`) with a **split catalog stored in `data/workouts.db`**, so:

- users can ask for a *named* split ("give me Push/Pull/Legs", "Upper/Lower with cardio"),
- every split is **guaranteed to cover all major muscle groups weekly** (test-enforced),
- **core is mixed in** on defined days rather than appearing only on leg days,
- **cardio days** become a first-class day type,
- the grounding story extends: split structure itself comes from the DB, not the model.

## Non-goals (keep it small)

- No new agent tools — `build_weekly_plan` gains one optional parameter.
- No periodization/progression logic, no per-user split memory (profile already stores
  `days_per_week`; a `preferred_split` profile field is a later 2-line addition).
- No changes to injury/equipment logic — named splits flow through the same exclusions.

## The catalog (the actual content)

Day templates (dataset muscle names):

| Template | Muscles |
|----------|---------|
| PUSH | chest, shoulders, triceps |
| PULL | lats, middle back, biceps, forearms |
| LEGS | quadriceps, hamstrings, glutes, calves |
| UPPER | chest, lats, middle back, shoulders, biceps, triceps |
| LOWER | quadriceps, hamstrings, glutes, calves |
| FULL | chest, lats, biceps, quadriceps, hamstrings, shoulders |
| CARDIO_CORE | (category = cardio picks) + abdominals |

Core rule: `+ core` on a day appends `abdominals`. Every split must include core ≥ 2×/week
(1×/week splits: on the full-body day).

| Days | Split (default first) | Day sequence |
|------|----------------------|--------------|
| 1 | Full Body | FULL + core |
| 2 | Full Body A/B | FULL + core, FULL + core |
| 2 | Upper / Lower | UPPER + core, LOWER + core |
| 3 | **Push / Pull / Legs** | PUSH + core, PULL, LEGS + core |
| 3 | Upper / Lower / Cardio | UPPER + core, LOWER, CARDIO_CORE |
| 3 | Full Body ×3 | FULL + core ×3 (variety via week-level dedup, already built) |
| 4 | **Upper / Lower ×2** | UPPER + core, LOWER, UPPER, LOWER + core |
| 4 | PPL + Upper | PUSH + core, PULL, LEGS + core, UPPER |
| 4 | Upper / Lower / Upper / Cardio | UPPER + core, LOWER, UPPER, CARDIO_CORE |
| 5 | **PPL + Upper / Lower** | PUSH, PULL + core, LEGS, UPPER, LOWER + core |
| 5 | Upper / Lower ×2 + Cardio | UPPER + core, LOWER, UPPER, LOWER + core, CARDIO_CORE |
| 6 | **PPL ×2** | PUSH, PULL + core, LEGS, PUSH, PULL + core, LEGS |
| 6 | PPL + UL + Cardio | PUSH + core, PULL, LEGS, UPPER, LOWER + core, CARDIO_CORE |

Weekly coverage guarantee (enforced by a new test, see Step 4): for every split with
≥ 3 days, the union of the week's muscles must include
`{chest, shoulders, triceps, biceps, lats, middle back, quadriceps, hamstrings, glutes,
calves, abdominals}`. For 1–2-day splits the same set minus nothing (FULL + core and
UPPER+LOWER + core both satisfy it via secondary coverage — assert it, don't assume it).

## Steps

### 1. Schema + data (`scripts/data_prep.py`) ✅

Two new tables built idempotently alongside the exercises tables:

```sql
CREATE TABLE splits (
    id            TEXT PRIMARY KEY,   -- 'ppl', 'upper_lower', 'ul_cardio', ...
    name          TEXT NOT NULL,      -- 'Push / Pull / Legs'
    days_per_week INTEGER NOT NULL,
    is_default    INTEGER NOT NULL,   -- 1 = the default for that frequency
    description   TEXT NOT NULL
);
CREATE TABLE split_days (
    split_id  TEXT REFERENCES splits(id),
    day_order INTEGER NOT NULL,
    day_name  TEXT NOT NULL,          -- 'Push', 'Cardio + Core'
    muscles   TEXT NOT NULL,          -- JSON list; [] for pure cardio
    is_cardio INTEGER NOT NULL DEFAULT 0
);
```

The catalog above lives as a Python literal in `data_prep.py` (single source of truth),
gets inserted, and `data/workouts.db` is re-committed. Print a coverage summary at build
time like the muscle-count summary the script already prints.

### 2. Loader with fallback (`healthva/workout_tools.py`) ✅

`load_splits()` reads the tables into the same shape the code uses today
(`{days: [(day_name, [muscles]), ...]}` plus name/id metadata), cached at module level.
If the tables don't exist (old DB), fall back to the current hardcoded `SPLITS` — no
breakage for anyone who hasn't re-run `data_prep.py`.

### 3. `build_weekly_plan` accepts a split ✅

- New optional arg `split: Optional[str]` on the pure function and `BuildWeeklyPlanArgs`
  (description lists valid ids, mirroring how muscle vocabulary is exposed).
- No `split` → the `is_default` split for `days_per_week` (today's behavior, unchanged
  outputs for PPL/UL defaults).
- Unknown split name → refuse and echo the catalog (`id — name (Nd/week)` lines), the
  same self-correction pattern as unknown muscles/injuries.
- Split whose `days_per_week` ≠ requested days → refuse with the catalog filtered to the
  requested frequency.
- Cardio days: `is_cardio` day → `exercise_search(category="cardio", ...)` picks
  (respecting injuries — ankle/knee exclusions already drop cardio where needed) plus the
  core muscles on the day.
- The tool's day header shows the split name (`Weekly plan: Push / Pull / Legs`).

### 4. Tests (`tests/test_split_catalog.py`) ✅

- **Coverage guarantee**: for every split in the DB, union of week muscles ⊇ the
  required set above; core appears ≥ 2×/week (≥ 1× for 1-day splits).
- Default split per frequency exists and is unique.
- Unknown split refused with catalog echo; frequency-mismatched split refused.
- Cardio day returns only `category='cardio'` exercises (plus core work).
- Injury interaction: PPL with `torn acl` still excludes leg work and says why
  (reuses `violates_exclusions`).

### 5. Docs ✅

- README tool table row for `build_weekly_plan` (+ named splits), PROGRESS.md bullet,
  architecture.html file map note, PLAN.md dated log entry, and — since tool behavior
  changes — re-run `scripts/eval_injury_guardrail.py` to confirm 0 violations still
  (the sweep calls `build_weekly_plan` with defaults, so it must stay green untouched).

## Definition of done

- [x] `data_prep.py` builds `splits`/`split_days`; `data/workouts.db` rebuilt
- [x] `build_weekly_plan(split="ul_cardio", days_per_week=3)` returns Upper/Lower/Cardio
      with core mixed in; bad names echo the catalog
- [x] All existing 22 tests untouched and green; 7 new split tests green (29 total)
- [x] Injury eval still 0 violations (6,641 prescribed exercises checked)
- [x] Docs updated per plan discipline
