# Userstory: Telegram Audio/File Routing

## User Story

As an operator sending audio or file inputs to the bot,
I want the runtime to route those inputs through the correct bounded processing path,
so that media handling becomes reliable and truthful instead of theoretical.

## Scope

This story covers Telegram media ingress, task-type recognition for audio/files,
and the minimal routing needed to choose an appropriate processing path or explain
why the requested path is unavailable.

## Acceptance Criteria

- The bot can distinguish text, image, audio, and file inputs on the Telegram path.
- Audio/file requests either route to a verified processing path or fail with a specific reason.
- Capability/status surfaces stop implying support when the configured model/path is unavailable.
- The implementation stays bounded and does not require generic unrestricted media execution.

## Current V1 Slice

- Telegram-local simulator now passes `media` payloads into the runtime.
- If audio/file routing is not configured, the bot returns a specific bounded explanation instead of pretending the path is available.

## References

- `docs/MODEL_PROBE_RESULTS_2026-03-30.md`
- `docs/MODEL_ROUTING_FALLBACK_V1.md`
