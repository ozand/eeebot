# Userstory: Live Telegram Model Selection Proof

## User Story

As an operator using the real Telegram bot,
I want to see that model routing/fallback works on the live Telegram path,
so that I can trust that task-type model selection is not only correct in simulator runs.

## Scope

This story covers real Telegram verification for the already implemented task-type routing
for `general`, `code`, and `vision` turns. It is about proof and observability, not about
adding more routing complexity.

## Acceptance Criteria

- A live Telegram probe demonstrates at least one `general` and one `code` turn.
- The probe records which model was selected and whether fallback was used.
- The proof is consistent with the live capability/runtime truth surface.
- Failures are classified as routing, model access, or Telegram-path issues.

## References

- `docs/MODEL_PROBE_RESULTS_2026-03-30.md`
- `docs/MODEL_ROUTING_FALLBACK_V1.md`
- `docs/LIVE_TELEGRAM_PROBE_PROTOCOL.md`
