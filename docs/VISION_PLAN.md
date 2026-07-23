# Image Upload (Vision) — Parked Design Note

**Status: parked, not started.** Deferred deliberately (2026-07-22) so the
balance/time work could ship first. This captures the idea and the honest scoping
so it isn't rediscovered.

## Reframe the use-case first

The original ask was "let users upload images so we understand their schedule."
Parsing a calendar/schedule screenshot into a reliable weekly plan is fuzzy and
hard to ground — it fights the project's "grounded in real data" ethos. A more
concrete, checkable vision task is a better fit:

- **"Photograph your gym / equipment" → detect available equipment → tailor the plan.**
  The output maps onto the existing `equipment` / `exclude_equipment` params, so the
  plan stays grounded in the exercise DB. This is the recommended first vision feature.

(If schedule-from-image is still wanted later, treat it as a separate, lower-confidence
feature with the model only *suggesting* days/times for the user to confirm — never
auto-committing a parsed schedule.)

## Why it's its own phase (cross-stack multimodal)

gpt-4o-mini is vision-capable, but wiring an image through touches every layer:

1. **UI (`port`)** — an upload control in the Composer; read the file, downscale
   client-side, base64-encode; show a thumbnail in the user bubble.
2. **Contract** — `/chat` request gains an optional `image` (base64 data URL or a
   short-lived uploaded URL). Bump `contract_version`. Size cap (e.g. ≤1–2 MB after
   downscale) enforced in the proxy.
3. **Proxy (`app/api/chat`)** — pass `image` through; reject oversized payloads early.
4. **Agent (`api.py` + `HealthAgent.chat`)** — build a multimodal LangChain
   `HumanMessage` with an image content block when an image is present. Only the
   relevant domain (equipment detection → workout) needs it.
5. **Cost/abuse** — images increase token cost; keep the shared-secret gate and
   consider a per-session rate limit before this is public.

## Minimal first slice (when we build it)

- One image per turn, workout domain only, "what equipment is in this photo?"
- A vision step returns a list constrained to the known `EQUIPMENT` vocabulary
  (same self-correcting pattern as muscles/injuries — unknown items dropped).
- Feed the detected list into `build_weekly_plan(equipment=...)`. The activity chip
  shows "detected equipment: …" so it stays transparent/grounded.

## Acceptance (future)

- Non-image turns unchanged; contract stays backward-compatible (image optional).
- Detected equipment is confined to the DB vocabulary; a junk photo degrades to
  "couldn't identify equipment — tell me what you have" (no hallucinated gear).
- Size/cost guarded; secret gate intact.
