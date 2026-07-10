# HealthVA (health_agent)

Routed diet + workout agent (LangGraph) with per-user memory. Portfolio project for
agentic-platform roles — grounding, guardrails, and observability are the point, not
just features. Architecture and design decisions: `README.md`.

## Plan discipline (important)

`PLAN.md` is the single source of truth for project state, and it must stay current:

- **Before starting work**: read `PLAN.md`, especially the "Progress log & notes for
  agents" section — known gotchas (placeholder Groq key, Ollama for local e2e) and
  prior bugs are recorded there so you don't rediscover them.
- **After completing work**: check the boxes you completed, fix anything the plan gets
  wrong, and append a dated log entry covering progress, difficulties, and errors
  (verbatim enough to be searchable).
- `DEBUG_MODE_PLAN.md` is **parked** until the 4 launch phases finish — don't start it,
  don't delete it.
- `PROGRESS.md` is the committable, user-facing record of completed work — keep it
  current alongside PLAN.md. `architecture.html` is deliberately **git-ignored**
  (local contributor doc) — update it on architecture changes, never commit it.
- Tests must never construct a bare `UserStore()` — strip `DATABASE_URL` from the env
  first (see E5 in PLAN.md), or they'll silently hit production Postgres.
- Cross-user isolation is load-bearing: ALL profile access goes through
  `store.scope(username)`; never add store/tool methods that accept a username, and
  never weaken the `user_isolation` RLS policy or the startup app-role detection
  (Supabase gotchas E6/E7 in PLAN.md explain why it works the way it does).

## Hard rules

- Never modify the two source projects: `~/sjsu/cmpe259/langchain_diet_agent` and
  `~/pers/lifestyle_agent`. They stay deployed as separate showcases.
- Injury-safety logic in `workout_tools.py` is test-covered (`test_injury_guardrail.py`);
  change it only with accompanying tests, and keep tool-layer disclaimers intact.
- Never commit `.env` or `data/users.db`. Secrets live in HF Space settings.

## Working in this repo

- Python: `.venv/bin/python` (created via `uv sync`; `langchain-ollama` installed extra
  for local e2e — deliberately absent from `requirements.txt`).
- Tests: `.venv/bin/python -m unittest test_router_memory test_injury_guardrail -v`
  (20 tests, all must pass).
- Secrets: a real `OPENAI_API_KEY` and `DATABASE_URL` live in local `.env`. **Never read
  `.env` directly** (no cat/Read) — Deep's explicit rule. Access secrets only through
  code via `dotenv`; print presence/shape booleans, never values; sanitize exceptions
  that might embed the connection string. `verify_deployment.py` is the template.
- Local e2e: `python verify_deployment.py [--live]` with the OpenAI backend; Ollama
  (`qwen2.5:3b`) works offline. The diet agent's Groq key is a placeholder — ignore it.
- Data files in `data/` (workouts.db, recipes.db, fdc_subset.json) are committed on
  purpose — HF Spaces needs them; `users.db` is local-only fallback state.
