---
title: Record handled-marker disposition without implying interruption cause
status: accepted
date: 2026-10-29
authors: [ozand, pX]
related: ["#2068", "#2050", "ADR-035"]
tags: [runtime, observability, ledger, privacy]
---

# Status

Accepted as a necessary technical ADR within the explicitly owner-approved #2076 outcome. The operator approved this decision in #2076 comment 6086959097; source implementation was authorized in comment 6089977778. The published delegated-acceptance authority and staged receipt are recorded in #2076 comment 6508593858, under `AGENT_ROUTING.md` and `docs/adr/README.md` rule 2. This status is delegated technical acceptance, not a claim of personal operator acceptance. The exact local candidate revision remains pending independent review; do not treat it as published acceptance until that reviewer passes this exact revision. Host rollout remains excluded and unauthorized.

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

The source implementation and named tests are merged on main under #2076. All five contract rows map to tests that exist on published main and cite ADR-037; no item is deferred. The named tests and mapping are preserved below.

| Decision claim | Test | Currently |
|---|---|---|
| Each helper return is preserved as matching cycle-row enum | `tests/test_bridge_executor_llm_error.py::TestHandledDispositionLedger::test_helper_returns_are_written_on_the_same_cycle_outcome` | passing on merged PR #2077; exact-head CI passed |
| Missing/unknown is omitted; old rows/callers remain compatible | `tests/test_cycle_ledger.py::TestTypedHelpers::test_record_cycle_outcome_omits_missing_or_unknown_handled_disposition` | passing on merged PR #2077; exact-head CI passed |
| Only enum is added; no raw/free-form field | `tests/test_cycle_ledger.py::TestTypedHelpers::test_record_cycle_outcome_preserves_handled_disposition`; `tests/test_cycle_ledger.py::TestTypedHelpers::test_record_cycle_outcome_omits_missing_or_unknown_handled_disposition` | passing on merged PR #2077; exact-head CI passed |
| Disposition is not exposed by live recent-outcome projection | `tests/test_cycle_ledger.py::TestTypedHelpers::test_handled_disposition_is_not_projected_to_live_recent_outcomes` | passing on merged PR #2077; exact-head CI passed |
| Existing marker/retry behavior unchanged | `tests/test_bridge_executor_llm_error.py::TestHandledDispositionLedger::test_helper_returns_are_written_on_the_same_cycle_outcome` | handled-disposition integration regression passes on merged PR #2077; exact-head CI passed |

# Verification and staged delivery boundary

The source/CI implementation is merged under #2076. Exact-head review and PR CI passed before merge; post-merge CI run `38024849555` passed on main merge commit `33c6d3fab8305b2445c3724141b7e586abc38899`. Docs PR #2080 merged reviewed head `7fbf3bcc4ccdd370541f4b0b159789164a1f586e` as `5c0d4b69029e565de2e061307571690c7c157244`; exact-head and post-merge CI passed (`38026400005`, `38027513678`). No host validation or rollout is claimed.

The delegated technical acceptance authority is the published rule in `docs/adr/README.md` and `AGENT_ROUTING.md`, applied to this explicitly owner-approved technical outcome. Owner approval and source authorization are at #2076 comments 6086959097 and 6089977778; the staged acceptance receipt is #2076 comment 6508593858. This records no personal operator acceptance. The local candidate's exact revision requires independent review PASS before publication or treating acceptance as complete. Host rollout is excluded and unauthorized.

# Rollback

For the source/CI stage, stop and revise or revert the writer if mapping,
compatibility, privacy, independent review, or exact-head CI fails; old rows need
no migration and readers must tolerate absence. Host rollout remains excluded;
its target and rollback criteria require separate authorization.

# References

- Issue #2068: https://github.com/ozand/eeebot/issues/2068
- Related #2050 is separate interruption work.
- Source SHA `b3031531ff179728700db74f630c0cf6a042ba90`.
- `nanobot/runtime/bridge.py:5648-5653, 8112-8204`; terminal ledger call near 7374.
- `nanobot/runtime/cycle_ledger.py:549-566, 685-699`.
