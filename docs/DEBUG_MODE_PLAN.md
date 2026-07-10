# Debug Mode & Context Management — Future Plan

**Status: parked.** Do NOT start until the four launch phases are done (merge ✅,
HF Space deploy, portfolio case study, eval report page). This document exists so the
idea survives until then.

Goal: a developer-facing debug mode showing, for every prompt, exactly what the agent
did and what it cost — plus active context management (compression + episodic memories).
Portfolio angle: this is the "observability & context engineering" chapter of the story;
recruiters for agentic platforms ask about token budgets and context strategies directly.

## What already exists to build on

- `ToolLoggingHandler` (common.py) captures tool start/end/error + route decisions to
  JSONL and to the UI's "Agent activity" panel — the debug panel is an extension, not new plumbing.
- `HealthAgent.chat()` is the single choke point every turn passes through — all metrics
  can be collected there.
- `UserStore` (memory.py) already handles per-user persistence — episodic memories are a
  second table, not a new system.

## Feature 1 — Per-prompt trace panel

Extend the existing activity panel into a structured trace:
- Route + method (already logged), grounding retries (already logged)
- Each tool call: name, arguments, output size, and **duration** (add `time.monotonic()`
  bookkeeping between `on_tool_start`/`on_tool_end` keyed by `run_id`)
- Number of LLM round-trips in the ReAct loop (count `on_chain_start` at the model node,
  or track via `on_llm_start`)

UI: `gr.Accordion("🔧 Developer debug")` rendered only when `DEBUG_MODE=1` env var is set
(Space stays clean for recruiters; set the var in a duplicated private Space for dev).

## Feature 2 — Token usage per prompt

- LangChain returns `usage_metadata` (`input_tokens`, `output_tokens`, `total_tokens`) on
  each `AIMessage` — collect via `on_llm_end` in the handler or by summing over
  `result["messages"]` in `chat()`.
- Report per turn: prompt tokens, completion tokens, cumulative session total.
- Log to JSONL (`event: "token_usage"`) so the eval/report pages can chart cost per query
  type (diet vs workout vs both — a great portfolio chart: "routing cut average prompt
  tokens by X% vs loading all tools").

## Feature 3 — Context window fill gauge

- Add `context_window` to `HealthAgentConfig` (llama-3.3-70b on Groq: 128k; qwen2.5:3b:
  32k). Compute fill = input_tokens_of_last_call / context_window.
- Before the call, estimate with a cheap heuristic (chars/4) for the live gauge; after
  the call, correct with the true `usage_metadata` number.
- UI: a percentage + progress bar in the debug panel; warn at >60%.

## Feature 4 — Context compression

Trigger: fill > 60% (config), or a "Compress now" debug button.
- Summarize the oldest N conversation turns into one system-context message via a single
  LLM call ("Summarize durable facts and decisions from this conversation segment").
- Replace those turns with the summary message; keep the most recent turns verbatim
  (LangChain `trim_messages` handles the windowing; the summary is ours).
- Log `event: "context_compressed"` with before/after token counts — measurable, chartable.
- Note: today `max_history_turns=6` crudely truncates; compression REPLACES that with
  something lossless-ish. Keep truncation as the fallback ceiling.

## Feature 5 — Episodic memories (distinct from the profile)

The profile stores *structured* facts (whitelisted fields). Memories store *unstructured*
durable notes: "prefers morning workouts", "hated the lentil recipe", "training for a
half-marathon in October".
- New table: `memories(username, memory TEXT, created_at)` in `UserStore` (works in both
  SQLite and Postgres).
- Written two ways: (a) as a side-effect of compression — the summarizer extracts
  memory-worthy facts; (b) a `save_memory` tool the agent can call, description-gated to
  durable preferences/goals only.
- Read path: load the user's most recent K memories into the same context block as the
  profile each turn.
- Later upgrade (separate decision): semantic retrieval with pgvector — Supabase supports
  it on the free tier, and `nomic-embed-text` is already pulled in local Ollama. Only
  worth it once users have >~30 memories; recency works fine before that.

## Suggested build order (one session each)

1. Token usage + context gauge (pure read-path metrics, no behavior change)
2. Trace panel with durations + DEBUG_MODE gating
3. Episodic memories table + save/load (extends tested UserStore patterns)
4. Compression (touches the hot path — do last, behind a config flag, with tests that
   verify no information the profile/memories should hold is lost)

## Acceptance checks

- Debug mode off → UI identical to today; JSONL still gets the new events.
- A 20-turn conversation stays under the fill threshold via compression, and the agent
  still answers a question about turn 1 correctly (via summary or memory).
- Token metrics match `usage_metadata` exactly (no estimate drift in the logged numbers).
- New unit tests: memories CRUD + isolation per user; compression trigger logic;
  token accounting with a fake LLM.
