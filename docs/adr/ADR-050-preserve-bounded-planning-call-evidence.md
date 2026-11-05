---
title: Preserve bounded planning-call evidence without inventing cause
status: proposed
date: 2026-11-05
authors: [delegated delivery]
related: ["#2160", "#1765", "#1919", "ADR-035"]
tags: [runtime, planning, ledger, recovery]
---

# Status

Proposed; not binding or implemented by this record. The local #1765 implementation draft preceded identification of the ADR gate; later review cannot retroactively make that edit compliant. Technical acceptance is delegated within the approved scope, not personal operator acceptance. Runtime implementation and its acceptance remain in #1765.

# Context

At main `b7c041950b9a00bed7dcffa81d5f7092d074c490`, planning can report `supplier_error` and `no_plan_recovery` classifies it as supply, but `cycle_ledger.record_planning_session` coerces it to `malformed`. These observations do not prove a supplier caused any particular incomplete call.

Unmerged #1919 candidate `848827b4` proposes a structured `model_call_failure` in the cycle outcome. Its response error producer emits `stage=response_handling` and `call_stage=model_call`; its ledger projection omits `call_stage` and message. Its classifier needs that provenance for the response-handling path, in addition to other inputs. This candidate is not published main. A separate existing `llm_error_classification.raw_error` contract is not changed here.

# Decision

Add `supplier_error` and `model_call_incomplete` to the planning-session outcome allowlist. Preserve affirmative supplier errors as `supplier_error`, with the existing supply recovery family. Use `model_call_incomplete` only when structured evidence establishes an incomplete model call but not a definitive cause; its recovery family is neither supply nor planner, leaving their existing counters unchanged. Do not infer supplier fault from incompleteness. Preserve the existing definitive request/auth rejection path as failed or malformed as appropriate; do not turn it into supplier or unknown-cause recovery.

For the proposed cycle-outcome `model_call_failure` projection, add optional `call_stage` only when its value is exactly `model_call`. Missing, invalid or other values are omitted and remain unknown, not inferred. The separate `stage=response_handling` is not itself proof that a model call failed. Historical rows are not rewritten. Other unrecognized planning outcomes retain the existing `malformed` fallback. Existing readers must tolerate both added outcomes and missing optional fields; unknown external consumers have not been verified.

The new projection adds no raw message. Existing separate `llm_error_classification.raw_error` behavior is outside this decision, not silently removed or extended. Preserving provenance alone cannot recreate classification without the same other inputs; no complete replay or telemetry-trust guarantee is claimed. No new framework, host rollout, or change to the existing request/authorization policy is authorized.

# Consequences

### Easier

The planning ledger can distinguish supplier evidence from an incomplete call of unknown cause, and the structured cycle evidence can retain a bounded provenance token.

### Harder

Consumers must handle additive values and absence. Historical rows cannot be repaired from the new field, and external readers may need separate compatibility work. Fail-open ledger writes do not guarantee evidence was recorded.

### Unchanged

Existing raw-error and recovery contracts otherwise remain; neither a recorded stage nor this ADR proves external causation or a successful host deployment.

# Alternatives considered

Keep coercing both outcomes to `malformed`: rejected because it attributes supplier or unknown-call failures to planner format. Treat all incomplete calls as supply: rejected because cause is not established. Persist arbitrary stage strings or the new raw message: rejected because provenance needs only an allowlisted token and extra text expands exposure. Backfill old rows: rejected because absent evidence cannot be reconstructed reliably.

# Test Contract

All rows are deferred to the separate implementation issue #1765; the names below describe required tests, not tests asserted to exist on main or already citing this ADR. A genuine implementation and independent review remain necessary before those deferrals can be resolved.

| Claim | Required test | Status |
|---|---|---|
| Affirmative supplier outcome survives producer-to-ledger and increments supply, not planner recovery | `tests/test_agent_chooses.py::test_planner_supplier_error_classified_as_supply` expanded to assert ledger and recovery | deferred (#1765) |
| Structured unknown incomplete stays distinct and increments neither family; definitive request/auth rejection stays failed/malformed | `tests/test_agent_chooses.py::test_planner_incomplete_and_request_rejection_recovery` | deferred (#1765) |
| Only `model_call` persists as optional call_stage; absent/invalid omitted, no message added; legacy rows remain readable | `tests/test_cycle_ledger.py::test_model_call_stage_allowlist_and_legacy_rows` | deferred (#1765) |
| Unknown planning outcome still maps to malformed; proposed values survive | `tests/test_cycle_ledger.py::test_planning_outcome_allowlist_and_invalid_fallback` | deferred (#1765) |
| Response-handling classifier round trip uses preserved stage with the same other decision inputs; absence alone cannot establish provenance | `tests/test_paused_supplier_1765.py::test_response_handling_call_stage_roundtrip` | deferred (#1765) |

# References

- Governing issue and pre-edit research: https://github.com/ozand/eeebot/issues/2160#issuecomment-6651817991
- Implementation and remaining criteria: https://github.com/ozand/eeebot/issues/1765 ; candidate https://github.com/ozand/eeebot/pull/1919
- Main producer and consumers: `nanobot/runtime/bridge.py`, `nanobot/runtime/cycle_ledger.py`, `nanobot/runtime/no_plan_recovery.py`, `nanobot/runtime/planner_rest.py`; `scripts/measure_package_1903.py` reports the last planning outcome. Health reads planning phase; scorecard terminal outcomes are separate. This bounded inventory does not establish compatibility of external consumers.
- Candidate structured producer/classifier: `nanobot/agent/subagent.py`, `nanobot/runtime/llm_error_classification.py`; decision remains proposed until the repository acceptance gates complete.
