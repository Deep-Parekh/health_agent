# Web App Plan — Custom Agent UI on the Portfolio (replacing Gradio + HF Spaces)

**Status: proposed design, awaiting approval. Not started.** Supersedes the old
Phase 2/3 (HF Spaces deploy + static case study). The agent LOGIC is reused verbatim —
this is a UI + hosting change only.

Mockup of the target UI: rendered in the portfolio's own theme tokens (see the artifact
shared in-chat). This doc is the architecture + build plan behind it.

## The core constraint (why this isn't "just move it to Vercel")

The agent brain is Python (LangChain/LangGraph). Vercel can host the **UI** but not the
**agent**: a ReAct turn makes several sequential OpenAI calls + tool runs and routinely
exceeds Vercel Hobby's function timeout, and the 26 MB SQLite DBs + `langchain`/`psycopg`
don't belong in a serverless bundle. So the system splits in two, and **no agent logic is
rewritten** — `agents.py`, `memory.py`, `workout_tools.py`, `diet_tools.py`, `common.py`
stay exactly as they are.

```
Browser  ──►  Next.js on Vercel (portfolio)
                 • /health-agent page  (new React chat console)
                 • /api/chat route handler  (thin server-side proxy)
                        │  (adds shared secret, hides backend URL, same-origin to browser)
                        ▼
              Python API  (FastAPI, ~40 new lines, on a free Python host)
                 • POST /chat → HealthAgent.chat(username, message, history)
                 • returns { reply, route, tool_trace, profile }
                        │                         │
                        ▼                         ▼
                   OpenAI API            Supabase Postgres (existing UserStore, RLS)
```

### Why the Next.js `/api/chat` proxy (not browser → Python directly)
- Keeps the Python URL + shared secret server-side; browser never sees them.
- Same-origin for the browser → no CORS setup.
- Natural spot for rate-limiting later (protects the OpenAI bill from randoms).

### Theming: inherited for free (no iframe)
The console is NATIVE React in the `port` DOM, not an embedded foreign app, so it sits
under the same `<html data-theme>` and reads the same `globals.css` variables — flipping
the site's theme toggle restyles it automatically. Discipline: components style through
tokens (`var(--accent-primary)`), never hardcoded hex. This is the payoff of building a
real UI instead of the old `<iframe src="hf.space">` (cross-origin → parent CSS can't
reach in → theming would need postMessage hacks Gradio barely supports).

## Two-repo management & the UI↔agent contract

Keep `port` (UI, Vercel) and `health_agent` (agent, Modal) as SEPARATE repos — health_agent
is a standalone portfolio artifact, and the two have different runtimes/lifecycles.
Monorepo/submodule rejected (toolchain friction for what is only a small JSON contract).

The entire coupling surface:
- **Contract** — `POST /chat` request `{username, message, history}` → response
  `{reply, route, tool_trace, profile}`. Documented here, mirrored as a TS type in `port`.
  `GET /health` returns `{status, contract_version}` so drift is detectable.
- **Config (no hardcoded URLs)** — `port` (Vercel env): `AGENT_API_URL`, `AGENT_API_SECRET`.
  Modal secrets: `OPENAI_API_KEY`, `DATABASE_URL`, `AGENT_API_SECRET` (must match `port`).
- **Deploys are independent** — agent change → `modal deploy` from health_agent; UI change →
  `git push` → Vercel auto-builds port. On a contract change, deploy the API first
  (additive: add fields, never rename), then the UI.
- **Local dev** — `modal serve` (temp live URL) or uvicorn locally; point port's
  `AGENT_API_URL` at `http://localhost:8000` via `.env.local` to develop UI vs a local agent.

## Decision needed: where the Python API runs

The FastAPI wrapper is identical regardless of host, so this is not a lock-in.

| Option | Free? | Cold start | Notes |
|--------|-------|-----------|-------|
| **Render** (recommend) | Free web service | ~50 s after 15 min idle | Simplest, recruiter-recognizable, Dockerfile or native. Cold start ≈ old HF problem. |
| **Modal** | Generous free credits | ~2–5 s, scale-to-zero | Best demo latency; serverless Python. Slightly more novel setup. |
| **Fly.io** | Free allowance | fast, scale-to-zero | More config. |
| Vercel Python fn | — | — | Rejected: timeout + bundle issues for a multi-step agent. |

**DECIDED (2026-07-10): Modal.** Chosen for scale-to-zero + ~2–5 s cold start — the right
call for a recruiter-clicked demo, and no cost when idle. Phase 1 uses a Modal ASGI stub
wrapping the FastAPI app; `data/*.db` bake into the image, `OPENAI_API_KEY` + `DATABASE_URL`
as Modal secrets. Wrapper code stays host-agnostic so a later move costs nothing.

## What gets built

### Python side (new: `api.py` + deploy config, agent code untouched)
- `POST /chat`: body `{username, message, history}` → calls existing `HealthAgent.chat`
  with a fresh `ToolLoggingHandler`, returns `{reply, route, tool_trace, profile}`.
  `tool_trace` = the handler's structured events (route, tool calls+args, grounding),
  which today only render in Gradio — now returned as JSON.
- `GET /health`: readiness probe / keep-warm target.
- Shared-secret header check (value from env, matches the Vercel proxy).
- `Dockerfile` (or Modal stub / `render.yaml`). `data/*.db` ship in the image; Supabase
  via `DATABASE_URL`, OpenAI via `OPENAI_API_KEY` — same env as today.
- **Streaming is Phase 2**: v1 returns the full turn with a loading state + the activity
  rail populating. True token streaming needs LangGraph streaming; not required to ship.

### Portfolio side (new page + components in `~/pers/port`)
- Route: `app/health-agent/page.tsx` (matches the existing `/schedule` route pattern;
  add to `Navbar` navItems with a terminal-style `file` label).
- Server route: `app/api/chat/route.ts` — proxies to the Python API with the secret.
- Components (`components/health-agent/`):
  - `Console` — two-pane shell (conversation + inspector), the mockup's layout.
  - `Message` — user/assistant bubbles; assistant content parses structured plans.
  - `PlanCard` — renders `build_weekly_plan` output as day cards (the mockup's grid)
    instead of a text blob; `MealCard` for diet output.
  - `ActivityChip` — progressive disclosure (Claude Code style), NOT an always-on rail.
    Per assistant turn: a single quiet line `◆ workout · 3 tool calls · grounded ✓` with
    a glyph that fills while the turn runs (working) then settles to a check (done).
    Click expands the full trace INLINE under the message: route reasoning, tool call +
    args, grounding check, memory chips (reads `tool_trace` + `profile`). Collapsed by
    default so casual users/recruiters aren't shown machinery — but the summary line still
    advertises that routing/grounding happened, so the observability (the portfolio hook)
    stays discoverable, one click away. Console is single-column (no right rail) — better
    on mobile, trace stays next to the answer it explains.
  - `Composer` — input + suggested-prompt chips; the instant-echo behavior we already
    fixed in Gradio applies here natively.
- Styling: reuse `globals.css` tokens + `.glass-panel`, `.section-eyebrow`, mono/sans
  pairing. No new design system.
- Session: the `username` field → a "session" chip (localStorage-persisted), keying the
  existing per-user profile + Supabase memory.

## How structured rendering works without changing tools

The tools already return well-structured text (day headers, `name (muscle, mechanic,
equipment) — 3×8–12`). Two clean options, decide at build:
1. **Parse on the client** the existing text into `PlanCard` props (zero backend change), or
2. **Return structured JSON** from `build_weekly_plan` alongside the text (small, additive
   tool change; cleaner). Lean option 2 for the weekly-plan tool only, since the shape is
   stable; everything else renders as markdown.

## Phasing

1. **API wrapper** ✅ (built + locally verified 2026-07-21) — `api.py` (FastAPI:
   `GET /health`, `POST /chat`, `X-API-Key` auth, input guardrails mirrored from app.py),
   `modal_app.py` (Modal ASGI stub, scale-to-zero, `healthva-secrets`). `common.py`
   handler gained an in-memory `events` list so `/chat` returns a structured `tool_trace`.
   Verified via FastAPI TestClient against real OpenAI (SQLite fallback, since Supabase
   was paused): workout turn routed + called build_weekly_plan (no barbell), profile
   saved, medical question blocked, trace returned. **Remaining:** `modal deploy` (needs
   owner: Modal auth + secret), then `verify_deployment.py --remote <url>`.
2. **Portfolio page** — console UI, proxy route, structured plan/meal cards, activity
   inspector; wired to the API.
3. **Polish** — suggested prompts, empty state, mobile (inspector collapses under chat),
   keep-warm ping, loading/skeleton states.
4. **(Later) streaming**, and retire the HF Space + Gradio `app.py` once parity is confirmed.

## What this preserves / retires
- **Preserved, untouched**: all agent logic, tools, memory, RLS isolation, guardrails,
  grounding, the 37 tests, the injury eval.
- **Retired**: `app.py` (Gradio), the HF Space, `scripts/deploy_space.py`. Keep them until
  the web app reaches parity, then remove in one commit.

## Known follow-ups (not blockers)

- **Eager DB connect at startup.** `HealthAgent()` → `UserStore()` opens Postgres at
  import time, so a Modal cold container crashes to boot if Supabase is briefly
  unreachable (free-tier pauses, circuit-breaker). Recommended small hardening: make
  `UserStore` schema-init lazy + tolerant, so the app boots and returns a clean
  per-request error instead of failing readiness. Do before/with the Modal deploy.
- **Supabase free tier pauses after ~7 days idle** (hit on 2026-07-21: `ECIRCUITBREAKER,
  failed to retrieve database credentials`). Resume from the dashboard before deploy; a
  keep-warm ping or the paid tier avoids it for a always-available demo.

## Definition of done (design phase)
- [x] Architecture decided (UI on Vercel, Python agent behind FastAPI on a Python host)
- [x] UI mockup in portfolio theme (artifact)
- [x] Backend host chosen — **Modal** (2026-07-10)
- [ ] Approval to start Phase 1 (API wrapper) — needs Deep
