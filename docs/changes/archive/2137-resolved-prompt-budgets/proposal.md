# Change: Publish resolved prompt budgets in system-prompt ledger rows

- **change-id:** 2137-resolved-prompt-budgets
- **issue:** https://github.com/ozand/eeebot/issues/2137
- **capability:** `docs/specs/subagent-bridge/spec.md`
- **role / workstream:** runtime / prompt-fit telemetry

## Problem

The loop prompt builder resolves effective prompt and compaction budgets, but
existing `system_prompt` ledger rows do not expose these values. Consumers that
repeat budget constants can drift from the values used for a particular fit.
The existing accepted ADRs describe prompt-fit telemetry and budget units but
not this additive ledger contract.

## Intended change

Append builder-resolved budget metadata to the existing normal-fit and strict-
overflow `system_prompt` ledger events:

- `system_prompt_budget_chars`;
- `release_pool_chars` with `limit` and `used`;
- `operating_reserve_chars`;
- `agents_md_cap_chars`;
- `compaction` with `window_tokens`, `reserve_tokens`, `threshold`, and
  `keep_tokens`.

When builder budget metadata is absent, emit none of these fields rather than
fabricating defaults; preserve the existing fields and behavior on both rows.
The contract rationale is recorded in proposed ADR-046.

## Acceptance

- Normal-fit and strict-overflow events carry the resolved numeric/structured
  fields, including applicable environment overrides.
- Missing builder budget metadata adds no budget fields and leaves each event's
  existing prompt-fit fields intact.
- Focused tests cover both event paths, overrides, and absent metadata.
- No dashboard consumer/presentation, host, deployment, historical-ledger, or
  gate changes are included.

## Out of scope

A current-configuration manifest, exact prompt payload record, consumer or gate
behavior, dashboard-repository work, host/private-history access, and rollout.
