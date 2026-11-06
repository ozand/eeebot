# Change: distinguish incomplete model calls from supplier outages and request defects

- **change-id:** 1765-incomplete-model-call
- **issue:** #1765 — https://github.com/ozand/eeebot/issues/1765
- **capability:** `docs/specs/subagent-bridge`

## Problem

Provider errors were collapsed into ordinary executor failures or supplier pauses. Incomplete provider calls need separate evidence and bounded retry handling: they indicate uncertainty about request size/budget, not proof that proposed code is defective. Supplier unavailability must not consume a request's defect retry budget.

## Intended change

Classify only positively identified supplier transport/status signals as `paused-supplier`; status numbers require HTTP/error context. Model-call-stage failures with persisted call evidence become `model_call_incomplete`; tool/code failures remain ordinary failures. Keep independent bounded retry counters for incomplete calls and ordinary executor failures, pause incomplete retries with an operator check, and include the appropriate attempt count in error-card recording. Exclude infrastructure-only outcomes from code-failure lessons and futility while preserving dedicated scorecard and daily-movement visibility.

## Acceptance

- [x] Supplier outage, incomplete model call, and request/code failure remain distinct.
- [x] Incomplete calls persist model-call evidence and use an independent bounded retry counter.
- [x] Error-card diagnostics identify the relevant retry attempt.
- [x] Outcome readers retain explicit semantics for the new terminal outcome.
