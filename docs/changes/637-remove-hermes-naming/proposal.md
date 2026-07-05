# 637 — Remove private "Hermes" product naming from the public repo

## Why

`eeebot` is a public repo. The subagent-executor provider name
(`hermes_pi_qwen`), the config default `bin_path` (`~/.hermes/node/bin/pi`),
and two standalone docs (`docs/HERMES_AUTONOMY_CHECKLIST.md`,
`docs/HERMES_AUTONOMY_INSTRUCTION_SNIPPET.md`) all named a private product
("Hermes") that should not appear in code/docs/config defaults shipped
publicly. Per the issue #637 claim comment, the host probe found no live
`/etc/eeepc-agent` config pins the old name — this is a default-change plus a
read-side compatibility alias, not a breaking change.

The `pi` binary name itself is **not** a private product name — it is the
functional CLI executable name — and stays as the PATH fallback.

## What renames

- Provider identity: `hermes_pi_qwen` → `local_pi_cli`
  (`nanobot/runtime/subagent_materializer.py`, `nanobot/config/schema.py`
  `SubagentToolConfig.provider` default).
- `bin_path` default: `~/.hermes/node/bin/pi` → `""` (empty = resolve via env
  or PATH, no hardcoded home path). Resolution order: env
  `NANOBOT_SUBAGENT_EXECUTOR_BIN` → configured `bin_path` → bare `"pi"` on
  PATH.
- Docs: `docs/HERMES_AUTONOMY_CHECKLIST.md` and
  `docs/HERMES_AUTONOMY_INSTRUCTION_SNIPPET.md` (the local executor's
  autonomy/completion-discipline instructions) folded, condensed, into
  `docs/specs/subagent-bridge/spec.md` under a new "Executor autonomy
  contract" section, then deleted (recoverable from git history).
- `docs/SYSTEM_OPERATION_REFERENCE.md` (~line 245) and
  `docs/specs/subagent-bridge/spec.md` (~line 42) provider references updated
  to `local_pi_cli`.
- `memory/MEMORY.md` "DO NOT touch" note genericized (no product name; keeps
  the operational warning about the legacy pre-migration agent directory).
- `docs/plans/2026-04-21-eeebot-full-migration-execution-plan.md`: one
  "For Hermes:" instruction line genericized to "Executor note:".
- `nanobot/runtime/lessons.py` docstring: "Hermes project" → "a sibling
  project" (unrelated compatibility note about the lessons schema, not the
  executor).

## Alias strategy

Grepping the codebase, the only readers that compare `config.tools.subagent.
provider` against a literal value are inside
`nanobot/runtime/subagent_materializer.py` itself (`_executor_metadata` and
the `pi_dev` executor-argv builder). Both now call a single
`normalize_provider_alias()` helper (`_LEGACY_PROVIDER_ALIASES = {
"hermes_pi_qwen": "local_pi_cli"}`) before using the value, so a deployed
config that still says `hermes_pi_qwen` keeps working unchanged. No other
in-repo reader compares the provider string, so this one-line normalization on
read is sufficient — no scattered `if` checks elsewhere.

Per migration-spec R7, historical state artifacts on the host (queued
results, learnings, etc.) that already recorded `hermes_pi_qwen` are **not**
rewritten — they remain valid history.

## Rollout

Pure default-value change + doc rename; no host config currently pins the old
provider name or bin path (confirmed by host probe in the issue claim
comment), so this ships as a normal PR — no coordinated host-side change
required. If a host config file explicitly sets `provider: hermes_pi_qwen` or
`bin_path: ~/.hermes/node/bin/pi`, it continues to work via the alias/env
override and can be updated to the new name opportunistically.

## Remaining "hermes" mentions after this change

A few string literals necessarily still say `hermes_pi_qwen` because they
document/implement the compatibility alias itself (not the private product
name as a live default): the `_LEGACY_PROVIDER_ALIASES` mapping key and
surrounding comments in `subagent_materializer.py`, references to the deleted
doc filenames in `docs/specs/subagent-bridge/spec.md`, and the new alias test
in `tests/test_runtime_coordinator.py`. These are the intended, load-bearing
remnants of the alias mechanism, not an incomplete rename.
