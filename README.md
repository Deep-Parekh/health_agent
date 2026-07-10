# HealthVA — Routed Diet + Workout Agent with Per-User Memory

One assistant for nutrition and fitness that demonstrates **context engineering**: each
turn is routed to a domain specialist that loads *only* the tools and prompt it needs,
grounding is enforced by the harness (not the model's goodwill), and user profiles
persist across sessions.

Combines [DietVA](https://huggingface.co/spaces/DeepParekh/diet_app) (diet agent) with the
workout module (873-exercise public-domain DB, injury guardrails with a
[published evaluation](docs/EVALUATION.md)) — both source projects remain
separate, deployed showcases.

## Architecture

```
user turn ──► router (keyword fast path ► LLM for ambiguous) ──► domain agent
                                                                    │
   diet:    5 diet tools    + 2 profile tools + nutrition prompt    │
   workout: 4 workout tools + 2 profile tools + fitness prompt      ├──► reply
   both:    all 11 tools    + combined prompt                       │    + route badge
   general: 2 profile tools + concierge prompt                      │    + JSONL audit log
                                                                    │
              grounding check: plan-shaped reply with no workout ───┘
              tool call ► forced retry ► visible warning
```

| File | Role |
|------|------|
| `healthva/agents.py` | Router (hybrid keyword/LLM), domain prompts, `HealthAgent` orchestration, grounding enforcement |
| `healthva/memory.py` | `UserStore` — Supabase Postgres (`DATABASE_URL`) or local SQLite fallback; whitelisted profile schema; per-session `UserScope` + Postgres row-level security for cross-user isolation; get/update profile tools |
| `healthva/diet_tools.py` | BMR/TDEE, food lookup (FoodData Central subset), recipe search, unit convert, web search |
| `healthva/workout_tools.py` | Exercise search, session & weekly plan builders, 1RM — with injury classification and joint load-path exclusions |
| `healthva/common.py` | Config, LLM backends (OpenAI deployed; Groq/Ollama supported), input guardrails, JSONL tool logging |
| `app.py` | Gradio UI: username-keyed profiles, live route badge, agent activity panel |

## Key design decisions

- **Domain-scoped tool loading**: the model never sees more than 7 tools at once
  (vs 11+ in a naive merge) — smaller context, less tool confusion, per-domain prompts.
- **Hybrid router**: deterministic keyword matching handles clear cases at zero cost;
  an LLM call classifies only ambiguous turns. Every decision is logged.
- **Harness-enforced grounding**: if a workout-route reply looks like a training plan but
  no workout tool ran, the harness rejects it and retries with an enforcement nudge; a
  second failure appends a visible warning. Verified to catch and correct a 3B local model
  that ignored prompt instructions.
- **Memory safety**: profile fields are whitelisted (no names/emails/identifiers), empty
  lists are treated as no-change so a model can't silently wipe stored injuries, and the
  profile is injected as context each turn so the agent doesn't re-ask.
- **Cross-user isolation (implemented & adversarially verified)**: profile access exists
  only through a per-session `UserScope` whose API has no username parameter — cross-user
  access is structurally impossible for both app code and the LLM — and Postgres
  row-level security enforces the same boundary inside the database (scoped transactions
  drop to a non-privileged role with a policy keyed on the session user). Verified by an
  adversarial check in `scripts/verify_deployment.py` that attempts real cross-user reads and
  writes against the live DB: zero rows in every direction.
- **Profile replaces the diet agent's "gatekeeper"**: stable facts are stored once instead
  of re-extracted from conversation every session.

## Run locally

```bash
uv sync                                  # or: pip install -e .
cp .env.example .env                     # add OPENAI_API_KEY (and DATABASE_URL for Postgres)
python app.py                            # http://localhost:7860
```

Backend auto-detects from whichever key is set: `OPENAI_API_KEY` → OpenAI (gpt-4o-mini),
`GROQ_API_KEY` → Groq, neither → Ollama (`uv pip install langchain-ollama`, needs
`ollama serve`). No `DATABASE_URL` → profiles go to local SQLite (`data/users.db`).

## Tests

```bash
python -m unittest discover -s tests -t . -v                    # 22 tests
python scripts/eval_injury_guardrail.py            # regenerates docs/EVALUATION.md
```

## Deploy (HF Spaces)

1. Create a Gradio Space, push this repo (`requirements.txt` is Spaces-ready and slim).
2. Space secrets: `OPENAI_API_KEY`, `DATABASE_URL` (Supabase → Project Settings → Database →
   Connection string URI; use the session pooler URI on IPv4-only hosts like Spaces).
3. The filesystem is ephemeral on free Spaces — that's why profiles live in Postgres.
