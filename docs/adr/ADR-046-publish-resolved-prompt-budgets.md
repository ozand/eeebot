---
title: Publish resolved prompt budgets on system_prompt ledger rows
status: accepted
date: 2026-11-01
authors: [eeebot maintainers]
related: ["#2137", "#2048", "ADR-003", "ADR-022"]
tags: [runtime, prompt, telemetry, ledger]
---

# Status

**Accepted — delegated technical acceptance candidate.** Prepared 2026-11-01
by the delegated delivery executor under the explicitly owner-approved
technical outcome in issue #2137. This is not personal operator acceptance.
The candidate is not published and must not be treated as formally accepted
until an independent reviewer passes the exact final revision and the
applicable checks pass, under `docs/adr/README.md` and `AGENT_ROUTING.md`.
The candidate accepting change is this draft status/index update in PR
[#2138](https://github.com/ozand/eeebot/pull/2138). Its head at preparation
is `e44c687b9e6fd5b81c2bc91c177a4e82bc85ab26`; independent review and
applicable checks of the final PR revision remain pending.

The source implementation merged in PR #2048 at
`c7b533abe2d788c90374615364ae6004f38a208c`. All six jobs in CI run
[41274493829](https://github.com/ozand/eeebot/actions/runs/41274493829)
passed on that exact main SHA. The five contract rows below map to existing
`main` tests; each named function is defined in
`tests/test_system_prompt_budget_telemetry.py`, which cites ADR-046. The
reviewed acceptance-candidate revision and independent review result must be
recorded in the delivery PR/review evidence before publication; this candidate
does not claim that review has already occurred.

# Context

Issue #2137 scopes PR #2048 to adding the prompt builder's effective budget
metadata to existing `phase: "system_prompt"` ledger events. The exact PR head
reviewed for this proposal was `0d2d2e32dbfd49f644bf6b8e8e6b40a2cee1f57a`;
its implementation has normal-fit and strict-overflow write sites and reads
values resolved by the builder/configuration. PR #2048's research reports that
no in-repository gate or selection reader consumes the proposed fields; that
is PR research, not a guarantee about future consumers.

ADR-022 decision 7 establishes the prompt budget's character enforcement and
token derivation; ADR-003 documents existing prompt-fit ledger data. Neither
explicitly decides this additive set of fields or shapes. A new persisted
ledger contract is warranted by the reversal test: removing it later requires
knowing which values were emitted and why.

# Decision

Append these five top-level fields to existing
`system_prompt` rows, using the builder's effective values for that fit:

- `system_prompt_budget_chars`: numeric character cap.
- `release_pool_chars`: object with numeric `limit` and `used` values.
- `operating_reserve_chars`: numeric character reserve.
- `agents_md_cap_chars`: numeric character cap.
- `compaction`: object with numeric `window_tokens`, `reserve_tokens`,
  `threshold`, and `keep_tokens` values resolved from the compaction settings.

Do not invent defaults: unavailable component values may be null; if builder
fit metadata itself is absent, emit no budget fields (empty added metadata).
Keep existing event fields, phase, writers, ledger behavior, and consumers
unchanged. This is additive telemetry, not a current-configuration manifest,
a record of exact prompt contents, or evidence that any gate passed.

Out of scope: dashboard consumer/presentation changes, host/runtime access,
deployment, historical ledger reads, new infrastructure, or changes to gates.
This ADR records the technical contract implemented in PR #2048; it does not
claim any dashboard rendering, host deployment, or production availability.

# Consequences

## What gets easier

Ledger consumers can use values resolved by the runtime builder rather than
independently restating prompt and compaction settings. The existing event
shape remains extensible without creating a second configuration authority.

## What gets harder

The ledger now carries nested numeric metadata whose semantics and units must
remain stable. Future changes to these values need compatible tests and should
not silently repurpose field meanings.

## What does not change

Existing prompt-fit fields and readers, gate/selection decisions, prompt
contents, compaction behavior, the ledger append model, and the dashboard or
host remain outside this proposal.

# Alternatives considered

- **Keep only existing prompt-fit measurements.** Rejected because they do not
  expose the effective component budget settings that motivated #2137.
- **Create a separate current-configuration/provenance record.** Rejected for
  this issue's bounded scope: it needs only values associated with the fit row,
  not a general manifest or historical configuration authority.
- **Publish exact prompt contents or use the new fields in a gate.** Rejected:
  neither is required to address this telemetry gap, and both expand the data
  exposure or authority surface beyond the issue.

# Test Contract

| Claim in Decision | Test | Currently |
|---|---|---|
| Normal-fit row includes the five resolved budget fields and numeric/structured shapes | `tests/test_system_prompt_budget_telemetry.py::test_healthy_system_prompt_row_carries_resolved_budgets` | Present on main at c7b533a; test file cites ADR-046 |
| Strict-overflow row includes the same resolved fields and shapes | `tests/test_system_prompt_budget_telemetry.py::test_overflow_system_prompt_row_carries_resolved_budgets` | Present on main at c7b533a; test file cites ADR-046 |
| Environment-resolved prompt and compaction values, rather than defaults, are published | `tests/test_system_prompt_budget_telemetry.py::test_healthy_system_prompt_row_carries_resolved_budgets`, `tests/test_system_prompt_budget_telemetry.py::test_overflow_system_prompt_row_carries_resolved_budgets`, `tests/test_system_prompt_budget_telemetry.py::test_builder_budget_reads_compaction_module_at_fit_time` | All three tests present on main at c7b533a; test file cites ADR-046 |
| Missing builder budget metadata adds no fabricated fields while existing event behavior remains intact on the normal-fit row | `tests/test_system_prompt_budget_telemetry.py::test_healthy_system_prompt_row_without_builder_budget_keeps_existing_fields` | Present on main at c7b533a; test file cites ADR-046 |
| Missing builder budget metadata adds no fabricated fields while existing event behavior remains intact on the strict-overflow row | `tests/test_system_prompt_budget_telemetry.py::test_overflow_system_prompt_row_without_builder_budget_keeps_existing_fields` | Present on main at c7b533a; test file cites ADR-046 |

# References

- Issue #2137 — `https://github.com/ozand/eeebot/issues/2137`.
- PR #2048 — `https://github.com/ozand/eeebot/pull/2048`; merged source
  revision `c7b533abe2d788c90374615364ae6004f38a208c`.
- ADR-003 — existing prompt-fit ledger fields and ownership.
- ADR-022 — context ontology and budget units.
- `nanobot/agent/context.py`, `nanobot/runtime/bridge.py`,
  `tests/test_system_prompt_budget_telemetry.py` on main at `c7b533a`.
