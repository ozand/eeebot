---
title: Record handled-marker disposition without implying interruption cause
status: proposed
date: 2026-10-09
authors: [ozand, pX]
related: ["#2068", "#2050", "ADR-035"]
tags: [runtime, observability, ledger, privacy]
---

# Status

Proposed under #2076 for source/CI implementation of the owner-approved ADR-037 disposition telemetry. Operator approval is recorded in #2076 comment 6086959097; governance recovery and source authorization are recorded in comment 6089977778. The repository ADR acceptance procedure is not yet satisfied on main, so this ADR remains proposed pending the named test contract on main and the operator's acceptance commit. Owner: ozand; source/test implementer: pX. Target: repository source control and CI only. Host rollout is excluded and unauthorized.

# Context

At immutable source `b3031531ff179728700db74f630c0cf6a042ba90`, `nanobot/runtime/bridge.py:8112-8204` defines `_decide_handled_marker`. It returns one of five strings, but its caller at `bridge.py:5648-5653` discarded the result. The terminal outcome was recorded later (near `bridge.py:7374`) and could not include it.

This is disposition observability, not cause attribution. Other interruption handling exists separately (`bridge.py:5664-5694, 5730-5771`); this does not cover every interrupted cycle or explain historical `interrupted_defect` rows in cycle-b56.

The outcome ledger is append-only. At the same immutable source, `nanobot/runtime/cycle_ledger.py:549-566` defines `record_cycle_outcome` and its optional classification arguments; serialization at `cycle_ledger.py:685-699` writes optional telemetry only when supplied. The persisted field is backward-compatible: old rows/callers remain valid, and missing data does not imply a disposition. No public projection is added.

# Decision

Terminal outcome telemetry records the exact `_decide_handled_marker` return as optional additive string field `handled_disposition`, carried from its existing call to the terminal ledger row. The owner-approved field and type are restricted to existing returns:
- `supplier_paused`
- `handled`
- `retired_state_lost`
- `retired_after_retries`
- `retry`

`handled` is broad: ordinary non-LLM and fail-open exception paths both return it. It does not imply completed work, model success, or interruption cause. Do not re-derive the value from error text, cycle outcome, or guessed state.

Missing/unknown values are omitted: no value or a value outside the allowlist is not serialized; old rows remain valid and no backfill occurs. Consumers interpret absence as unknown/not recorded, never as `handled` or another default. This decision is approved by the operator in #2076 comment 6086959097.

## What this IS

An additive internal ledger annotation for one existing helper's returned value, without changing its decisions (operator-approved in #2076 comment 6086959097).

## What this IS NOT

Not interruption-cause classification, executor success attribution, retry-policy change, public/dashboard payload, raw error storage, historical backfill, evidence about cycle-b56 `interrupted_defect`, or instrumentation of separate open-increment paths. No host changes, model calls, deployment, or rollout.

## Success criteria

- Code records only an approved helper return on the same cycle's terminal row.
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

Proposed only; source implementation and tests are being delivered under #2076. Keep this ADR proposed until its named test contract exists on main; only the operator records acceptance after that repository procedure is satisfied. Contract tests cite ADR-037 per `docs/adr/README.md`.

| Decision claim | Test | Currently |
|---|---|---|
| Each helper return is preserved as matching cycle-row enum | `tests/test_bridge_executor_llm_error.py::TestHandledDispositionLedger::test_helper_returns_are_written_on_the_same_cycle_outcome` | passing locally; source/CI pending |
| Missing/unknown is omitted; old rows/callers remain compatible | `tests/test_cycle_ledger.py::TestTypedHelpers::test_record_cycle_outcome_omits_missing_or_unknown_handled_disposition` | passing locally |
| Only enum is added; no raw/free-form field | `tests/test_cycle_ledger.py::TestTypedHelpers::test_record_cycle_outcome_omits_missing_or_unknown_handled_disposition` | passing locally |
| Disposition is not exposed by live recent-outcome projection | `tests/test_cycle_ledger.py::TestTypedHelpers::test_handled_disposition_is_not_projected_to_live_recent_outcomes` | passing locally |
| Existing marker/retry behavior unchanged | `tests/test_bridge_executor_llm_error.py` | 14 passed locally; source/CI pending |

# Verification and staged delivery boundary

This source/CI implementation is in progress under #2076. Local focused tests are recorded above; exact-SHA independent review and CI remain pending. No host validation or rollout is claimed. The target is this repository's source and CI. The staged delivery sequence is:

1. **Design review (completed):** validate the owner-approved field against the
   existing helper and ledger source; the bounded read-only governance recovery
   verified the corrected #2076 scope, approval, and no duplicate/claim conflict.
2. **Proposed source/CI delivery (current stage):** add the optional field and
   deterministic tests under #2076; keep this ADR proposed until its test
   contract exists on main and the operator records acceptance. Obtain fresh
   independent review of the exact PR head and passing CI on that same SHA.
   Evidence is the immutable source comparison, named ADR/document test output,
   independent review of the exact commit, and exact-SHA CI result. If anchors do
   not match, a blocking review finding remains, or CI fails, stop and revise or
   withdraw this Proposed PR; do not merge or deploy.
3. **Merge/source publication:** merge only after exact-head review and CI pass;
   preserve the ADR as proposed until operator acceptance is recorded according
   to `docs/adr/README.md`. Evidence must include tests for all five legal
   returns, missing/unknown compatibility, privacy, exact-base diff review, and
   exact-SHA CI. Stop/revert if mapping, compatibility, privacy, review, or CI
   gates fail; do not deploy.
4. **Host rollout:** excluded and unauthorized. It requires a separate issue,
   named target/environment, rollout evidence, rollback criteria, and explicit
   owner authorization before any host action. No host rollout is part of the
   current design or future source/CI stage.

This sequence is a design plan, not evidence that any later stage has occurred.

# Rollback

For the current source/CI stage, stop and revise or revert the writer if mapping,
compatibility, privacy, independent review, or exact-head CI fails; old rows need
no migration and readers must tolerate absence. Keep this ADR proposed until the
operator records acceptance after the named test contract exists on main. Host
rollout remains excluded; its target and rollback criteria must be decided in a
separate issue before authorization.

# References

- Issue #2068: https://github.com/ozand/eeebot/issues/2068
- Related #2050 is separate interruption work.
- Source SHA `b3031531ff179728700db74f630c0cf6a042ba90`.
- `nanobot/runtime/bridge.py:5648-5653, 8112-8204`; terminal ledger call near 7374.
- `nanobot/runtime/cycle_ledger.py:549-566, 685-699`.
