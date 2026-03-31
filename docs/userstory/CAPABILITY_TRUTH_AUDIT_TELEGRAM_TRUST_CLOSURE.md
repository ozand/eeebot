# Userstory: Capability Truth Audit And Telegram Trust Closure

## User Story

As a maintainer of the autonomous runtime,
I want host CLI, `/cap_status`, `/action capability.truth-check`, and Telegram
workspace/capability surfaces to speak from one runtime truth source with
visible provenance,
so that operators can trust what is really available before we widen autonomy or
roll out more aggressive changes.

## Scope

This story covers a bounded truth-audit package.

It should tighten:

- shared capability summary provenance,
- Telegram command/help parity for existing runtime surfaces,
- focused parity tests,
- and the evidence needed to isolate the remaining live Telegram gap.

It should not add a new command transport, a new terminal ingress system, or a
new executor layer.

## Acceptance Criteria

- `/cap_status` and `/action capability.truth-check` share one human-readable
  summary path.
- The capability summary exposes snapshot provenance explicitly.
- Telegram help and command registration reflect the actual `/workspace` runtime
  surface already available in the loop.
- Focused tests prove command/help parity and truth-summary rendering.
- Any remaining live Telegram mismatch is reduced to a real operator probe,
  rather than an unresolved code-path ambiguity.

## References

- `todo.md`
- `docs/LIVE_TELEGRAM_PROBE_PROTOCOL.md`
- `docs/HOST_BOT_COMMUNICATION.md`
- `docs/WORKSPACE_RUNTIME_LANE.md`
