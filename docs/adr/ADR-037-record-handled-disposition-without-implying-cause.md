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

Proposed pending the operator's acceptance commit. The source implementation and named tests are merged on main under #2076. Keep this ADR proposed until the operator records acceptance under the procedure in `docs/adr/README.md`. Contract tests cite ADR-037.

| Decision claim | Test | Currently |
|---|---|---|
| Each helper return is preserved as matching cycle-row enum | `tests/test_bridge_executor_llm_error.py::TestHandledDispositionLedger::test_helper_returns_are_written_on_the_same_cycle_outcome` | passing on merged PR #2077; exact-head CI passed |
| Missing/unknown is omitted; old rows/callers remain compatible | `tests/test_cycle_ledger.py::TestTypedHelpers::test_record_cycle_outcome_omits_missing_or_unknown_handled_disposition` | passing on merged PR #2077; exact-head CI passed |
| Only enum is added; no raw/free-form field | `tests/test_cycle_ledger.py::TestTypedHelpers::test_record_cycle_outcome_preserves_handled_disposition`; `tests/test_cycle_ledger.py::TestTypedHelpers::test_record_cycle_outcome_omits_missing_or_unknown_handled_disposition` | passing on merged PR #2077; exact-head CI passed |
| Disposition is not exposed by live recent-outcome projection | `tests/test_cycle_ledger.py::TestTypedHelpers::test_handled_disposition_is_not_projected_to_live_recent_outcomes` | passing on merged PR #2077; exact-head CI passed |
| Existing marker/retry behavior unchanged | `tests/test_bridge_executor_llm_error.py::TestHandledDispositionLedger::test_helper_returns_are_written_on_the_same_cycle_outcome` | handled-disposition integration regression passes on merged PR #2077; exact-head CI passed |

# Verification and staged delivery boundary

The source/CI implementation is merged under #2076. Exact-head review and PR CI passed before merge; post-merge CI run `38024849555` passed on main merge commit `33c6d3fab8305b2445c3724141b7e586abc38899`. No host validation or rollout is claimed. The staged delivery sequence is:

1. **Design review (completed):** validate the owner-approved field against the
   existing helper and ledger source; the bounded read-only governance recovery
   verified the corrected #2076 scope, approval, and no duplicate/claim conflict.
2. **Source/CI delivery (merged):** PR #2077 added the optional field and
   deterministic tests under #2076. Exact-head review was clean and PR CI passed
   on `2ef727435aa6bb696fc8a1ab487d392b06474d42`; post-merge CI passed on
   `33c6d3fab8305b2445c3724141b7e586abc38899`. The ADR remains proposed until
   the operator records acceptance according to `docs/adr/README.md`.
3. **Operator acceptance (pending):** after verifying every named contract test
   exists on main and cites ADR-037, the operator may record acceptance by a
   commit that changes frontmatter and index status to `accepted` and records the
   acceptance date and commit/PR under `# Status`, as required by
   `docs/adr/README.md`. This document update does not itself accept the ADR.
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
