# 641 — Remove the pi_dev subagent executor dependency

## Context

The `pi_dev` subagent executor profile (`NANOBOT_SUBAGENT_EXECUTOR=pi_dev`)
shelled out to the external `pi` binary (`/usr/local/bin/pi`) with `--mode
json -p --no-session --no-tools --provider local_pi_cli --model
un/qwen3.6-27b-mtp`. With `--no-tools`, pi's entire agentic harness is
disabled, so the call was functionally equivalent to one direct LiteLLM
request through the same proxy nanobot already uses — we paid the dependency
cost without the harness benefit:

- an external binary from a separate private product with its own release
  cycle;
- a Node.js runtime officially unsupported on i386, self-compiled for the
  eeepc host (#637 already documented this as the most fragile link — any
  `pi` update risks forcing a node rebuild);
- extra config surface (`provider`, `bin_path`, `normalize_provider_alias()`
  legacy-name map) surviving past the naming cleanup in #637.

## Decision (variant 1, operator-approved 2026-07-05)

Remove the `pi_dev` executor path entirely. The verify step now relies solely
on the built-in executor path — the same one used when
`NANOBOT_SUBAGENT_EXECUTOR` is unset (`queued_request_terminalizer` in the
coordinator, and the bridge's own direct LiteLLM call in
`nanobot/runtime/bridge.py`). If the host still sets
`NANOBOT_SUBAGENT_EXECUTOR=pi_dev` (e.g. an uncleaned systemd drop-in), the
runtime logs one line and falls back to the built-in path instead of
constructing pi argv or crashing.

## Scope

- `nanobot/runtime/subagent_materializer.py`: removed `PI_DEV_PROVIDER`,
  `normalize_provider_alias()`, `_resolve_executor_bin()`, `PI_DEV_BIN`,
  `PI_DEV_COMMAND(_ARGV)`, and the pi argv-construction branch.
  `NANOBOT_SUBAGENT_EXECUTOR_COMMAND` (the generic custom-command mechanism)
  is unaffected — it is a separate, still-supported override.
- `nanobot/config/schema.py`: removed the now-dead `SubagentToolConfig.provider`
  and `.bin_path` fields; `max_running`, `model`, `api_base` are unchanged.
- Tests updated in `tests/test_runtime_coordinator.py` to cover the
  graceful-degrade path instead of pi argv construction.
- Docs: `docs/specs/subagent-bridge/spec.md` and
  `docs/SYSTEM_OPERATION_REFERENCE.md` updated to describe a single built-in
  executor, with a short history note pointing here and at #637.

## Non-goals

- Enabling pi *with* tools as a code-execution harness for subagents (variant
  2) — that would be a deliberate autonomy-surface expansion requiring
  promotion gating, out of scope here.
- Changing `/usr/local/bin/pi` on the host — it remains installed for other
  products; eeebot simply stops depending on it.
- Rewriting historical state artifacts that still carry old provider names
  (migration spec R7: never rewritten in place).
