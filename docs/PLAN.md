# Health Agent — Phase 1 Merge Plan

> **Status (2026-07-09): Steps 1–5 done, Steps 6–7 open.** Scope grew during execution —
> see the progress log at the bottom for what changed, what broke, and how it was fixed.
> Keep that log updated: every agent session that advances this plan should check boxes,
> correct anything stale, and append a dated entry.
>
> **Parked for after all 4 launch phases:** developer debug mode (per-prompt tool traces,
> token usage, context-fill gauge, context compression, episodic memories) — see
> [DEBUG_MODE_PLAN.md](DEBUG_MODE_PLAN.md).

Goal: build **HealthVA**, a single lifestyle agent (diet + workout) in this directory by
porting from two source projects, **without modifying either source**. Both originals
stay deployed and intact as separate showcases.

| Source | Path | What it contributes |
|--------|------|---------------------|
| DietVA (diet agent) | `~/sjsu/cmpe259/langchain_diet_agent` | Agent skeleton, LLM backends, Gradio UI, JSONL tool logging, diet tools + data |
| Workout module | `~/pers/lifestyle_agent` | `workout_tools.py`, `data/workouts.db`, injury guardrail, tests, eval harness |

Deployment target: a **new** Hugging Face Space (e.g. `DeepParekh/health_agent`), leaving
`DeepParekh/diet_app` untouched.

---

## Step 1 — Scaffold this repo ✅

- [x] `git init`, copy `.gitignore`, `.python-version` from the diet agent (`.env.example` written fresh; `data/users.db` added to `.gitignore`)
- [x] `pyproject.toml` written fresh (name `healthva`); slim deps — torch/ollama moved to a `local` optional extra
- [x] `uv sync` → own venv at `.venv` (plus `uv pip install langchain-ollama` for local e2e)

## Step 2 — Port the diet agent core ✅ (restructured, not copied)

- [x] Diet agent's 1,223-line `app.py` split into modules instead of copied wholesale:
  `common.py` (config/backends/guardrails/logging), `diet_tools.py` (5 tools, logic verbatim), `app.py` (Gradio only)
- [x] `data/fdc_subset.json` and `data/recipes.db` copied; food/recipe lookups smoke-tested
- [x] `requirements.txt` written for Spaces (Groq-only, slim)
- [x] **Deliberately dropped**: prompting strategies (chaining/meta/reflection) and the SmolLM Gatekeeper — the persistent profile replaces the Gatekeeper's job. They remain showcased in the diet agent repo.

## Step 3 — Port the workout module ✅

- [x] Copied: `workout_tools.py`, `data_prep.py`, `data/workouts.db`, `test_injury_guardrail.py`, `eval_injury_guardrail.py`
- [x] `workouts.db` added to the `SPACE_ID` copy loop (now in `common.py`)
- [x] `WORKOUTS_DB_PATH` imports from `common.py` (Spaces-aware), with a standalone fallback for tests/eval

## Step 4 — Wire the tools together ✅ (became a routed architecture)

- [x] **Scope change (user request):** instead of one agent with all 11 tools, `agents.py` routes each turn to a domain agent — diet (5+2 tools), workout (4+2), both (11), general (2 profile tools only) — each with its own system prompt
- [x] Hybrid router: keyword fast path (plural-aware), LLM classification for ambiguous turns, every decision logged
- [x] **Added (user request):** per-user memory — `memory.py` `UserStore`, Supabase Postgres via `DATABASE_URL` or SQLite fallback, username-keyed, whitelisted profile fields, profile injected as context each turn
- [x] **Added (found necessary in testing):** harness-enforced grounding — plan-shaped workout replies with no workout tool call trigger a forced retry, then a visible warning (see log entry E3)
- [x] Fitness guardrails in `WORKOUT_PROMPT`; PED keywords added to the input filter; renamed to HealthVA / `health_agent` logger

## Step 5 — Verify locally ✅ (with two caveats for the deploy session)

- [x] `python -m unittest test_router_memory test_injury_guardrail -v` → **20/20 pass**
- [x] Diet flows work (food_lookup, recipe_search smoke-tested against real data)
- [x] E2E with Ollama `qwen2.5:3b`: workout session saves profile (incl. `torn acl`) and produces a DB-grounded weekly plan; a **fresh** diet session recalls the profile — cross-session memory verified
- [x] `logs/tool_calls.jsonl` records both tool families + route + grounding events
- [x] ~~⚠️ Not yet verified on a hosted frontier model~~ → **verified on OpenAI gpt-4o-mini** (2026-07-09, `verify_deployment.py --live`): workout turn called `update_user_profile` + `build_weekly_plan` with **zero grounding retries**
- [x] ~~⚠️ cross-domain showcase not run e2e~~ → **verified**: routes to `both`, chains `build_workout` + `bmr_tdee_calculator` in one turn

## Step 6 — Deploy ⬜ (needs Deep for account setup)

**Backend decision (2026-07-09): the deployed LLM backend is OpenAI** — Deep will provide
an `OPENAI_API_KEY` at deploy time. `app.py` auto-detects (openai > groq > ollama), so no
code changes are needed when the key arrives; Groq/Ollama remain supported for local dev.
Hosting is HF Spaces; Supabase provides only the Postgres database.

- [x] Create free Supabase project (done by Deep, 2026-07-09)
- [x] Fix `DATABASE_URL` in local `.env` (Deep swapped in the session pooler URI, 2026-07-09) — `verify_deployment.py` all green: Postgres schema created, profile roundtrip works
- [x] `verify_deployment.py --live` — **9/9 checks passed** on OpenAI + Supabase; profile with injuries persisted to the real `users` table
- [ ] Create HF Space `health_agent` (Gradio SDK), push this repo
- [ ] Space secrets: `OPENAI_API_KEY`, `DATABASE_URL` (Deep pastes both directly in Space settings)
- [ ] Smoke-test on the live Space: the two-session e2e from Step 5 **plus** the cross-domain showcase prompt; confirm profiles land in Supabase (not SQLite — check the startup log line)
- [ ] Watch `logs/tool_calls.jsonl` for `grounding_retry` events — gpt-4o-mini should rarely trigger them; frequent firing means `_looks_like_plan` is too broad

## Step 7 — Hand-off to portfolio phases ⬜

- [ ] GitHub Actions: run the 30 unit tests on push
- [ ] `eval_injury_guardrail.py`: also emit `results.json` for the portfolio eval page
- [ ] Record the 20–30s demo GIF of the cross-domain prompt (after OpenAI verification)

---

## Future phases — full roadmap (context: portfolio on Vercel free tier)

The overall goal is a recruiter-facing showcase. Vercel free can't run the Python agent,
so HF Spaces stays the runtime and the portfolio is the static showcase layer.

**Phase 2 — Deploy** = Step 6 above.

**Phase 3 — Portfolio case study** (in `~/pers/port`, Next.js App Router):
- New static route `app/projects/health-agent/page.tsx`: lead with the engineering story
  (routing, grounding enforcement, memory safety, observability), embed the live Space
  via iframe (note the ~30–60s free-tier cold start), demo GIF fallback, teaser card in
  the existing `ProjectsSection`.

**Phase 4 — Scientific eval report page** (also in `~/pers/port`):
- Static route `app/projects/health-agent/evaluation/page.tsx`, structured as
  Abstract → System → Method → Results → Discussion → Limitations → Reproducibility.
- Renders from `results.json` (Step 7) so re-running the eval updates the page —
  numbers must never be hand-copied into JSX.
- Source material: `~/pers/lifestyle_agent/EVALUATION.md` and the artifact page
  (claude.ai/code/artifact/fbb1ace8-5167-42ff-aea3-1a26d8480631).

**Phase 5 (parked) — Debug mode**: see [DEBUG_MODE_PLAN.md](DEBUG_MODE_PLAN.md). Do not
start until Phases 2–4 ship.

## Compatibility contracts — do NOT break these (later phases depend on them)

1. **JSONL event schema** (`logs/tool_calls.jsonl`): `tool_start/tool_end/tool_error/
   route/grounding_retry/grounding_violation` with their current fields. Debug mode
   (Phase 5) and the portfolio charts build on it — extend with new event types, never
   rename existing ones.
2. **LLM backends go through `ChatOpenAI`-compatible interfaces** (`build_llm` in
   `common.py`). Debug mode's token accounting reads `usage_metadata`, which works for
   both OpenAI and Groq through `langchain-openai` — don't introduce a backend that
   lacks it without noting the gap.
3. **Eval output**: when adding `results.json` (Step 7), keep `EVALUATION.md` generation
   unchanged — the lifestyle_agent repo and the portfolio page consume different formats
   of the same run.
4. **`UserStore` schema**: `users(username, profile, updated_at)` with JSON profile.
   Phase 5 adds a `memories` table alongside it — additive only, no migration of the
   users table (there will be real user rows in Supabase by then).
5. **`HealthAgentConfig`**: Phase 5 adds `context_window`; keep the dataclass the single
   place model/limit knobs live so the gauge doesn't need scattered constants.

---

## Decisions already made (don't relitigate)

- Both source projects stay untouched and separately deployed; this repo is a copy-and-combine
- New Space rather than overwriting `diet_app`; Supabase Postgres for memory (user chose over Turso / HF-dataset hack / Vercel rebuild); simple username field for identity (user chose over HF OAuth)
- Routed architecture + per-user memory + grounding enforcement are in scope for Phase 1 (user requested the first two; testing proved the third necessary)
- Injury safety rules live in `workout_tools.py`, ported verbatim from `lifestyle_agent` and covered by its 13 tests — extend only with new tests

## Progress log & notes for agents

Append dated entries. Include errors verbatim enough to be searchable.

### 2026-07-09 — Phase 1 built and locally verified (Claude session)

**Environment gotchas:**
- **E1:** Use `.venv/bin/python` in THIS repo — do not borrow the diet agent's venv anymore. `langchain-ollama` is installed here but is not in `requirements.txt` (Spaces doesn't need it).
- **E2:** The diet agent's `.env` `GROQ_API_KEY` is a 7-char placeholder (`gsk_...`), NOT a real key — don't burn time on 401 debugging like this session did. The real key exists only in the `diet_app` Space secrets. Local e2e uses Ollama (`ollama serve`, `qwen2.5:3b` and `mistral:7b` are pulled).
- `duckduckgo_search` prints a rename warning (`pip install ddgs`) — cosmetic, same as in the diet agent; migrate when convenient.

**Bugs found & fixed (all have regression tests in `test_router_memory.py` / covered paths):**
- Keyword router missed plurals ("workouts", "meals" → no route). Fix: `\b{kw}s?\b` in `agents.py::keyword_route`.
- `update_user_profile` rejected `injuries: "torn acl"` (string) with a pydantic `list_type` error — qwen passes strings for list fields. Fix: list fields in `UpdateProfileArgs` accept `Union[str, List[str]]`; `validate_profile_updates` splits on commas.
- Model passed `injuries: []` for a user who HAS an injury, silently wiping the field. Fix: empty lists are treated as no-change in `validate_profile_updates` (deliberate trade-off: users can't clear a list via the agent; acceptable for now).

**E3 — the big one:** small local models (qwen2.5:3b) hallucinate workout plans instead of calling `build_weekly_plan` — including a leg day for a torn-ACL user; prompt instructions alone did NOT fix it (mistral:7b was worse: writes tool calls as literal text). Fix: harness-level grounding check in `HealthAgent.chat()` — plan-shaped reply on workout/both routes with no `GROUNDING_TOOLS` call → one forced retry with a nudge message → if still ungrounded, append `UNGROUNDED_PLAN_WARNING`. Verified: retry fired and produced a correctly injury-filtered, DB-grounded plan. JSONL events: `grounding_retry`, `grounding_violation`.

**For the next session (deploy):** everything in Step 6; both ⚠️ items in Step 5; expect a frontier hosted model to route/ground far better than the 3B local model, but confirm the grounding check stays silent (it should never fire on a compliant model — if it fires often, the `_looks_like_plan` heuristic may be too broad; check `logs/tool_calls.jsonl`).

### 2026-07-09 (later) — Deployment backend switched to OpenAI (Claude session)

- Deep will provide an `OPENAI_API_KEY` at deploy; hosting stays HF Spaces + Supabase DB.
- Zero-rework change made now: `app.py` backend auto-detection is `OPENAI_API_KEY` →
  `GROQ_API_KEY` → Ollama, with per-backend default models (`gpt-4o-mini` for OpenAI).
  `build_llm` already had the openai branch from day one, so nothing else moved.
- `.env.example` and README updated to lead with OpenAI. Verified: 20/20 tests still
  pass; backend resolution unit-checked by hand (no key envs → ollama).
- Added "Future phases" roadmap and "Compatibility contracts" sections to this plan so
  later phases don't cause rework of Phase 1 code.

### 2026-07-09 (later still) — Supabase wiring, verify script, secret-handling rules (Claude session)

- **Secret discipline (Deep's explicit instruction):** never read `.env` directly (no
  `cat`/Read); access secrets only through code via `dotenv`, and print only
  presence/shape booleans — never values. `verify_deployment.py` sanitizes exceptions so
  psycopg errors can't leak the connection string. Follow this in all future sessions.
- Added `verify_deployment.py` — reusable checks: secret presence (names only), DB
  connect + schema init, profile roundtrip, backend construction; `--live` adds three
  real agent turns (workout+memory, diet+recall, cross-domain). This same script is the
  smoke test on the live Space later. Also fixed `.gitignore`: `.env.example` was
  accidentally ignored (inherited from diet agent); now committable via `!.env.example`.
- **E4 (open):** first run failed at UserStore init (`OperationalError`, DNS). URL-shape
  diagnosis (parsed in code, nothing printed): `DATABASE_URL` is the **direct-connection**
  host (`db.<ref>.supabase.co` — IPv6-only, dead on HF Spaces) AND contains literal
  square brackets (the `[YOUR-PASSWORD]` placeholder brackets were kept around the real
  password). Fix is on Deep: swap in the **session pooler** URI, no brackets. Gotcha for
  future agents: `urlparse` raises `ValueError: ... does not appear to be an IPv4 or
  IPv6 address` on bracketed hosts — that error means brackets in the URL, not a DNS issue.
- OpenAI backend confirmed working shape-wise (`ChatOpenAI` constructs with the real key
  present); live turns deferred until E4 is fixed.
- Added `architecture.html` — self-contained contributor guide (system diagram, request
  flow, file map, setup/deploy/test). Keep it updated when the architecture changes;
  it's part of the repo, not generated.

### 2026-07-09 (evening) — E4 resolved; full OpenAI + Supabase verification green (Claude session)

- Deep fixed `DATABASE_URL` (session pooler URI, brackets removed). `verify_deployment.py
  --live`: **9/9 passed**. Postgres schema auto-created; profile roundtrip + live persistence
  confirmed in the real `users` table (probe rows are cleaned up by the script).
- Live turns on gpt-4o-mini: workout → `update_user_profile` + `build_weekly_plan`
  (**no grounding retry** — the enforcement stayed silent on a compliant model, as designed);
  diet → `recipe_search` + `web_search`; cross-domain → route `both`, chained
  `build_workout` + `bmr_tdee_calculator`. Steps 5 fully closed.
- **Maintenance notes for a future session (not urgent):**
  - `create_react_agent` is deprecated in LangGraph v1.0 (moved to
    `langchain.agents.create_agent`; removal in v2.0) — migrate `agents.py:250` before any
    langgraph major-version bump.
  - `duckduckgo_search` package renamed to `ddgs` — migrate `diet_tools.py` import +
    `requirements.txt`/`pyproject.toml` together.
- **Remaining for launch (needs Deep in browser):** create the HF Space, paste both
  secrets in Space settings, push the repo, then run the `--live` prompts in the UI and
  check the startup log says "UserStore: using Postgres".

### 2026-07-09 (night) — Dead-code cleanup, PROGRESS.md, test-isolation fix (Claude session)

- Removed diet-VA leftovers per Deep: the `hf_local` backend (torch/transformers branch
  in `common.py`, config literal, `top_p`, pyproject `local` extra trimmed to
  langchain-ollama). Backends are now openai/groq/ollama; config default is
  openai/gpt-4o-mini. Ruff cleaned 2 unused test imports; `uv lock` refreshed.
- `architecture.html` is now git-ignored (Deep wants it local-only); added `PROGRESS.md`
  (committable, concise record of all work) — keep BOTH updated as work lands.
- **E5, important:** adding `DATABASE_URL` to `.env` broke 2 memory tests — `UserStore()`
  in setUp silently connected to REAL Postgres (env fallback), then the test's
  `store.backend = "sqlite"` override pointed queries at a schemaless temp file. Fixed
  with `mock.patch.dict(os.environ)` + popping `DATABASE_URL` in setUp, and setUp now
  asserts `backend == "sqlite"`. Rule for future tests: strip `DATABASE_URL` from the
  env; never construct a bare `UserStore()` in tests.
- Verified after cleanup: 20/20 tests, `verify_deployment.py` all green (non-live).

### 2026-07-09 (late night) — Cross-user isolation: scope API + Postgres RLS (Claude session)

- **Feature (Deep's request): one user can no longer touch another user's rows.** Two layers:
  1. **Structural (all backends):** `UserStore.get_profile/update_profile` are GONE.
     The only data path is `store.scope(username)` → a `UserScope` whose methods take
     no username at all; profile tools bind to a scope, so neither app code nor the
     LLM can name another user. Enforced by tests (`test_scope_api_cannot_address_other_users`,
     `test_store_has_no_direct_profile_methods`) — 22 tests now.
  2. **Database (Postgres):** row-level security policy `user_isolation` on `users`
     (`username = current_setting('app.current_user')` for USING and WITH CHECK).
     Every scoped transaction runs `SET LOCAL ROLE <app_role>` + transaction-local
     `set_config('app.current_user', …)` — pooler-safe, evaporates at commit.
- **Two Supabase platform gotchas discovered (cost ~3 debug rounds):**
  - **E6:** Supabase's `postgres` login role has `BYPASSRLS = true` — policies never
    apply to it, even with `FORCE ROW LEVEL SECURITY`. RLS must run under another role.
  - **E7:** `GRANT health_agent_app TO CURRENT_USER` is blocked on Supabase — the
    server ABORTS THE CONNECTION mid-command (OperationalError mentioning SSL; state
    not applied). You cannot give `postgres` SET membership in a custom role.
    Solution: fall back to Supabase's built-in `authenticated` role (no BYPASSRLS,
    `postgres` already has SET on it). `UserStore` detects the assumable role at
    startup: `health_agent_app` on vanilla Postgres, `authenticated` on Supabase,
    warning + structural-only isolation if neither.
- `verify_deployment.py` gained an **adversarial isolation check**: under user B's
  transaction identity it attempts to read and UPDATE user A's row directly by name,
  and counts visible rows with no identity set — all three must be 0. Result on real
  Supabase: `role: authenticated, cross-read 0, cross-update 0, no-identity visible 0`.
  All checks green; 22/22 tests.
- Contract addition: the RLS policy name (`user_isolation`), the `app.current_user`
  GUC, and startup role detection are now part of the memory-layer contract — schema
  changes must keep them intact.

### 2026-07-09 — First push to GitHub (Claude session)

- Repo live at https://github.com/Deep-Parekh/health_agent (branch `main`, commit
  `3f91cd8`). Deep's push had failed simply because the repo had no commits yet —
  remote was configured, staging was clean.
- Pre-push audit confirmed excluded from the commit: `.env`, `data/users.db`,
  `logs/`, `architecture.html` (local-only by choice). Committed on purpose:
  `data/recipes.db` (25 MB), `data/workouts.db`, `data/fdc_subset.json` — HF Spaces
  installs from the repo and needs them. GitHub's hard limit is 100 MB/file, so fine.
- This same repo can now be added as the HF Space remote for Step 6
  (`git remote add space https://huggingface.co/spaces/<name>` + push).

### 2026-07-09 — Repo reorganized into logical folders (Claude session)

- New layout: `healthva/` (the package: agents, common, memory, diet_tools,
  workout_tools), `scripts/` (data_prep, verify_deployment, eval_injury_guardrail),
  `tests/`, `docs/` (PLAN, PROGRESS, DEBUG_MODE_PLAN, EVALUATION, architecture.html
  [still git-ignored]). `app.py` + `requirements.txt` MUST stay at repo root — HF
  Spaces' Gradio SDK looks for them there.
- Mechanics future agents should know: imports are now `from healthva.x import …`;
  scripts carry a 3-line `sys.path` shim so `python scripts/foo.py` works from anywhere;
  `common.BASE_DIR`/`data_prep.BASE_DIR` use `parents[1]` (repo root) since the files
  moved one level down; the eval imports its labeled cases from `tests.test_injury_guardrail`
  and writes to `docs/EVALUATION.md`.
- Test command changed: `python -m unittest discover -s tests -t .` (22 tests).
- Ran the injury-guardrail eval in THIS repo for the first time → committed
  `docs/EVALUATION.md` (same results as lifestyle_agent: 34/34 classification,
  0 violations across 5,757 prescribed exercises, 100% disclaimer coverage).
- Full re-verification after the move: 22/22 tests, deployment checks all green
  (isolation still zero-rows), `app.py` imports clean.

### 2026-07-10 — Database-grounded workout split catalog (GPT session)

- Implemented `docs/SPLITS_PLAN.md`: `data/workouts.db` now has `splits` and
  `split_days` tables with 13 named options across 1–6 training days. The schema
  enforces valid frequencies/booleans, ordered days, referential integrity, and one
  default per frequency; `scripts/data_prep.py` validates weekly muscle/core coverage
  before inserting anything and prints a catalog summary.
- `build_weekly_plan` accepts an optional split ID or normalized display name, defaults
  by frequency, and returns the valid catalog for unknown or frequency-mismatched
  requests. Split/day structure is loaded from SQLite with a cached compatibility
  fallback for old databases. Cardio is now a first-class day type and uses
  `exercise_search(category="cardio")` under the existing injury/equipment exclusions;
  core work is mixed into catalog-defined days.
- Two contradictions in the draft catalog were corrected to satisfy its own guarantees:
  the 2-day Upper/Lower option now includes core on both days (the stated minimum is
  twice weekly), and FULL explicitly includes biceps so the generated 1-day plan's
  primary+secondary exercise coverage contains every required major muscle group.
- Added `tests/test_split_catalog.py`: catalog/default integrity, generated weekly
  coverage and core frequency for all 13 splits, self-correcting refusals, cardio-only
  category picks plus core, natural display-name matching, torn-ACL safety interaction,
  and old-DB fallback. Full suite: **29/29 passed**.
- Re-ran `scripts/eval_injury_guardrail.py`: **34/34** classifications, **5/5**
  refusals, **0 violations across 6,641 prescribed exercises**, and **0 missing
  disclaimers**. `docs/EVALUATION.md`, README, PROGRESS, SPLITS_PLAN, and the local
  architecture guide were updated.

### 2026-07-10 — Gradio 6 first-prompt rendering fix (GPT session)

- Local testing exposed `gradio.exceptions.Error: "Data incompatible with messages
  format. Each message should be a dictionary with 'role' and 'content' keys or a
  ChatMessage object."` after the agent had successfully completed its first turn.
- Root cause: Gradio 6 requires messages format, while `app.py` still returned the
  legacy `[[user, assistant]]` tuple format. Small boundary helpers now append
  role/content dictionaries and convert prior messages back to the pair format expected
  by `HealthAgent.chat`; guardrail-blocked responses use the same path. Gradio 6 also
  removed the old `Chatbot(type=...)` selector, so no compatibility flag is passed.
- Added `tests/test_app_ui.py` to lock the two-way history conversion. Full suite:
  **30/30 passed**.

### 2026-07-10 — Two bug fixes: Gradio textbox + equipment exclusion (Claude session)

- Context: Deep implemented the split catalog (docs/SPLITS_PLAN.md, all boxes checked,
  29→35 tests) and migrated app.py to Gradio 6 messages format, then reported two bugs
  from local runs.
- **E8 (UI):** submitted messages stayed in the textbox. Cause: two independent
  `msg.submit()` listeners (respond + a clearing lambda) — unreliable in Gradio 6.
  Fix: canonical chained flow — `queue_message` (echoes the user message into the chat
  and returns "" for the textbox, instantly) `.then()` `generate_reply` (guardrails +
  agent). Bonus UX: the user's message now appears immediately instead of after the
  LLM finishes. Guardrail replies now append to an already-echoed user message.
  Note: `agent.chat` receives `_history_pairs(history[:-1])` because it appends the
  current message itself — don't "fix" that slice.
- **E9 (equipment):** "I have everything except a barbell" still produced barbell
  exercises. Cause: tools only accepted an *inclusion* list; the model had no way to
  express exclusion and passed nothing → all equipment allowed. Fix: `exclude_equipment`
  threaded through exercise_search (NULL-safe SQL: `equipment IS NULL OR NOT IN`),
  both pickers, build_workout, build_weekly_plan, all three tool schemas, plus a
  WORKOUT_PROMPT rule to use it for exclusion-phrased equipment. Verified live via
  gradio_client: the model passed exclude_equipment=['barbell'] and the plan contained
  zero barbell exercises.
- Tests: 37/37 (5 new equipment tests in tests/test_equipment.py; tests/test_app_ui.py
  updated to the two-stage helpers). Injury eval re-run: still 0 violations.
- **Gotcha (cost 1 debug round):** a stale `python app.py` from an earlier session held
  port 7860, so the new boot died ("Cannot find empty port") and gradio_client silently
  tested the OLD process. Before UI testing: `pkill -f app.py` and check
  `lsof -iTCP:7860`.
