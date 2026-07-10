# HealthVA — Work Completed

A concise record of what was built and verified, in order. Detailed state and
per-session notes live in `PLAN.md`; design rationale in `README.md`.

## Origin

- HealthVA merges two independent projects into one agent, without modifying either:
  **DietVA** (LangChain/LangGraph diet planner, deployed on HF Spaces) and a **workout
  module** built on the public-domain free-exercise-db dataset (873 exercises,
  17 muscle groups, loaded into SQLite).

## Workout domain & safety (built first, in the workout module)

- Grounded tools over the local exercise DB: `exercise_search`, `build_workout`
  (single session), `build_weekly_plan` (1–6 days/week; 7 days refused to enforce a
  rest day), `one_rep_max_calculator` (Epley + Brzycki).
- Database-grounded workout split catalog: 13 named options across 1–6 days/week,
  including PPL, upper/lower, full-body, and mixed cardio plans. Split structure,
  core placement, and cardio days are loaded from `workouts.db`; bad names and
  frequency mismatches return the valid catalog for model self-correction. Build-time
  validation and tests enforce one default per frequency, weekly major-muscle
  coverage, and core frequency.
- User constraints respected end to end: training frequency, equipment owned
  (bodyweight always implicitly available), body metrics (BMI screen — ≥30 excludes
  high-impact plyometrics), and injuries.
- Injury guardrail with three exclusion rules: affected muscles (primary **or**
  secondary), risky categories (e.g. plyometrics for knee/ankle), and **joint
  load-path rules** (shoulder/elbow/wrist injuries exclude all upper-body push/pull —
  a bench press stresses the elbow even though the dataset lists only "chest").
- Deterministic injury classification in code, not the LLM: "torn ACL" → knee,
  "spinal" → lower back + neck, "hip labral tear" → hip. Unknown injuries are refused
  with an escalation to a medical professional. Disclaimers are embedded in tool
  output so the model cannot omit them.
- **Evaluated, not just tested**: 34/34 injury phrases classified correctly, 5/5
  out-of-taxonomy refusals, 0 exclusion violations across 6,641 prescribed exercises
  in a 288-configuration sweep, 100% disclaimer coverage
  (`scripts/eval_injury_guardrail.py` → `docs/EVALUATION.md`).

## The merged agent (this repo)

- **Domain-routed architecture**: each turn is classified diet / workout / both /
  general; only that domain's tools and system prompt enter the model's context
  (max 7 tools instead of 11). Hybrid router: deterministic keyword pass first,
  one LLM call only for ambiguous turns; every decision is logged.
- **Per-user memory**: `UserStore` on Supabase Postgres (`DATABASE_URL`) with a local
  SQLite fallback, keyed by username. Profile fields are whitelisted (no names,
  emails, or identifiers), list fields accept model-friendly string input, and empty
  lists are no-ops so stored injuries can't be silently wiped. The profile is injected
  as context each turn, replacing DietVA's per-session info-gathering "gatekeeper".
- **Cross-user isolation, enforced twice**: application code can only touch profiles
  through a per-session `UserScope` whose API has no username parameter (cross-user
  access is structurally impossible, not just forbidden), and Postgres row-level
  security fences the same boundary in the database — every scoped transaction drops
  to a non-privileged role with a policy keyed on the session user, so even a SQL
  bug could not read or write another user's row. Verified adversarially against the
  live database: cross-user reads, cross-user updates, and identity-less scans all
  return zero rows.
- **Harness-enforced grounding**: a plan-shaped workout reply that used no workout tool
  triggers a forced retry with an enforcement nudge; a second failure appends a visible
  warning. Added after observing a 3B local model invent a leg day for a torn-ACL user
  while ignoring prompt instructions — verified to catch and correct exactly that case.
- **Input guardrails** before any LLM call: medical/PED topics, privacy patterns
  (SSN/email/phone), and prompt-leak requests are blocked with explanations.
- **Observability**: every route decision, tool call, grounding event, and error goes
  to `logs/tool_calls.jsonl`; the UI shows the live route badge and tool trace.
- **Gradio 6 chat compatibility**: the UI uses role/content message dictionaries and
  converts them at the boundary to the agent's internal turn-pair history, preventing
  first-prompt rendering failures while keeping orchestration unchanged.
- Diet tools ported logic-unchanged from DietVA: BMR/TDEE (Mifflin-St Jeor), food
  lookup (FoodData Central subset), recipe search with dietary-restriction filters,
  kitchen unit conversion, web-search fallback.

## UI & constraint fixes (2026-07-10)

- Split catalog shipped: named, database-grounded splits (PPL, Upper/Lower, cardio
  variants) with test-enforced weekly muscle coverage and core mixed in.
- Chat UX fixed: submitted messages clear from the textbox and echo into the chat
  instantly (two-stage Gradio flow) before the agent starts working.
- Equipment by exclusion: "everything except a barbell" now maps to an
  `exclude_equipment` tool parameter (NULL-safe SQL filter) — verified live: the model
  passes it and plans contain no excluded equipment.

## Verification status

- **30/30 unit tests** (router keyword paths, memory isolation/validation, UI history,
  the full
  injury guardrail suite, and split catalog guarantees), with test isolation guaranteed
  not to touch production data.
- **`scripts/verify_deployment.py` all green, including `--live`** on the production stack
  (OpenAI `gpt-4o-mini` + Supabase Postgres): workout turn saved the profile and called
  the planner with zero grounding retries; diet turn grounded in recipe/web tools; the
  cross-domain prompt routed to `both` and chained tools across domains; the profile
  (injuries included) persisted to the real `users` table.
- LLM backends: OpenAI (deployed), Groq, Ollama (offline dev) — auto-detected from
  available keys.

## Remaining before launch

- Create the HF Space, add `OPENAI_API_KEY` + `DATABASE_URL` as Space secrets, push,
  and smoke-test live (`scripts/verify_deployment.py` prompts).
- Then: portfolio case study page, scientific eval report page, CI (see `PLAN.md`
  roadmap; a developer debug mode is planned and deliberately parked).
