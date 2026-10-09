---
title: Record handled-marker disposition without implying interruption cause
status: proposed
date: 2026-10-09
authors: [ozand, pX]
related: ["#2068", "#2050", "ADR-035"]
tags: [runtime, observability, ledger, privacy]
---

# Status

Proposed for discovery/design review under #2068. Not accepted; no implementation authorized. Owner: ozand; design writer: pX. Design target: repository source control and CI only. Evidence required before any future implementation PR is considered: exact-base/source tests for each disposition and compatibility case, fresh independent review of the exact commit, and passing CI on that exact SHA. Host rollout is excluded and unauthorized here; it requires a separate issue and explicit owner authorization.

# Context

At immutable source `b3031531ff179728700db74f630c0cf6a042ba90`, `nanobot/runtime/bridge.py:8112-8204` defines `_decide_handled_marker`. It returns one of five strings, but its caller at `bridge.py:5648-5653` discards the result. The terminal outcome is recorded later (near `bridge.py:7374`) and cannot include it.

This is disposition observability, not cause attribution. Other interruption handling exists separately (`bridge.py:5664-5694, 5730-5771`); this does not cover every interrupted cycle or explain historical `interrupted_defect` rows in cycle-b56.

The outcome ledger is append-only. At the same immutable source, `nanobot/runtime/cycle_ledger.py:549-566` defines `record_cycle_outcome` and its optional classification argument; serialization at `cycle_ledger.py:685-699` writes that nested telemetry only when supplied. A new persisted field is a format decision: old rows/callers remain valid, and missing data must not imply a disposition. No public projection is proposed.

# Decision

If approved, terminal outcome telemetry may record the exact `_decide_handled_marker` return as optional additive `handled_disposition`, carried from its existing call to the terminal ledger row. Values are restricted to existing returns:
- `supplier_paused`
- `handled`
- `retired_state_lost`
- `retired_after_retries`
- `retry`

`handled` is broad: ordinary non-LLM and fail-open exception paths both return it. It does not imply completed work, model success, or interruption cause. Do not re-derive the value from error text, cycle outcome, or guessed state.

The proposed missing/unknown rule is omission: no value or a value outside the approved allowlist is not serialized; old rows remain valid and no backfill occurs. Consumers interpret absence as unknown/not recorded, never as `handled` or another default. This remains a proposal pending explicit owner approval of field, enum, and compatibility semantics.

## What this IS

An additive internal ledger annotation for one existing helper's returned value, without changing its decisions.

## What this IS NOT

Not interruption-cause classification, executor success attribution, retry-policy change, public/dashboard payload, raw error storage, historical backfill, evidence about cycle-b56 `interrupted_defect`, or instrumentation of separate open-increment paths. No host changes, model calls, deployment, or rollout.

## Success criteria

- Later authorized code records only an approved helper return on the same cycle's terminal row.
- Missing/unknown stays absent, not defaulted.
- Retry, marker, ledger outcome, and interruption behavior stay unchanged.
- No free text or public projection is added.

# Consequences

### What gets easier

Ledger readers can distinguish the existing handled-marker returns without log parsing or retry-state reconstruction.

### What gets harder

The row gains an optional field; writers must allowlist values and readers preserve absent/unknown. `handled` remains broad and cannot answer whether the executor completed.

### What does not change

No decision logic, retry counters, marker writes, supplier handling, open-increment behavior, outcome, raw-error handling, public projection, host, or deployment.

# Alternatives considered

### Keep the return only in logs

This preserves the ledger format, but durable use depends on log retention and parsing. The helper already computes a bounded enum, so structured persistence is simpler if the owner wants durable observability.

### Infer disposition from outcome/error fields

Rejected: those fields are different facts and cannot distinguish all five returns; inference risks conflating disposition with cause.

### Do nothing

No schema risk, but the ledger remains unable to answer which handled-marker result occurred. Acceptable if the owner decides the diagnostic value does not justify a field.

# Test Contract

Proposed/planned only; these tests are not claimed to exist or pass. On acceptance, contract tests must cite ADR-037 per `docs/adr/README.md`.

| Decision claim | Planned test | Currently |
|---|---|---|
| Each helper return is preserved as matching cycle-row enum | `tests/test_bridge_executor_llm_error.py::<new_disposition_ledger_test>` | not yet written |
| Missing/unknown is omitted; old rows/callers remain compatible | `tests/test_cycle_ledger.py::<new_handled_disposition_compatibility_test>` | not yet written |
| Only enum is added; no raw/free-form field | `tests/test_cycle_ledger.py::<new_handled_disposition_privacy_test>` | not yet written |
| Existing marker/retry behavior unchanged | `tests/test_bridge_executor_llm_error.py::<existing_and_new_retry_regressions>` | not yet verified |

# Verification and rollout boundary

This discovery/design proposal is verified only by source review against the
referenced immutable SHA and review of this Proposed ADR. No implementation,
runtime test, host validation, or rollout is claimed. If a separate implementation
is later authorized, its evidence gate is: focused source tests for every legal
return and missing/unknown compatibility, exact-base diff review, fresh
independent review of the final SHA, and CI passing on that same SHA. The target
for that future verification is repository source and CI. Host rollout is
explicitly excluded; it requires a separate issue, target/environment statement,
and explicit owner authorization before any host action.

# Rollback

Before implementation, withdraw/revise this Proposed ADR. If a future authorized
implementation lands, rollback is to stop writing the optional field and revert
the writer; old rows need no migration, and readers must tolerate absence. No
host rollback plan is applicable because host rollout is out of scope and
unauthorized.

# References

- Issue #2068: https://github.com/ozand/eeebot/issues/2068
- Related #2050 is separate interruption work.
- Source SHA `b3031531ff179728700db74f630c0cf6a042ba90`.
- `nanobot/runtime/bridge.py:5648-5653, 8112-8204`; terminal ledger call near 7374.
- `nanobot/runtime/cycle_ledger.py:549-566, 685-699`.
