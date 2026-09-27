# Design: incomplete model-call classification and retry separation

## Classification

- `paused-supplier` is an affirmative infrastructure signal, matched via positive evidence. HTTP status numbers must have error/status context, so numeric payloads such as an output-token ceiling cannot imply provider outage.
- `model_call_incomplete` requires persisted telemetry identifying the model-call stage. Tool execution, response handling, and local code failures do not receive the classification.
- Affirmative client/request rejection evidence (known request-validation error types or invalid-parameter/context/tool-payload markers) retains the ordinary `failed` path even when the model-call stage is known; supplier evidence takes precedence.
- Other errors retain the existing ordinary failure path.
- The bridge returns a distinct non-zero exit status for `model_call_incomplete`; process and systemd exit recorders finalize an explicit incomplete run/exit record without updating the success/failure streak. Deployment activation and post-flip gates treat it as inconclusive and keep the candidate active while awaiting a clean cycle.

## Retry and diagnostics

Supplier pauses leave the request pending without consuming retry state. Incomplete calls use `retry_incomplete_<id>.json`, bounded independently from `retry_<id>.json`; each provider call retains its local transient timeout retries before the bridge-level bounded retry applies. Exhaustion pauses automatic retry and calls for operator request-size/budget inspection. Incomplete-call failures bypass recent-failure suppression while retry budget remains; once exhausted, matching proposals enter the ordinary bounded suppression window. Error-card recording reports the counter selected by the rollback classification, avoiding stale or missing attempt numbers.

## Reader contract

The incomplete-call outcome is excluded from the count-based consecutive-failure stall signal, but does not suppress the independent elapsed-time-without-success alert.

The new outcome remains separate from ordinary execution failures in scorecard fields and daily movement, is treated as infrastructure in lessons/futility readers, is retained in raw loop metrics, and does not become code-defect demand. Success-only and unrelated outcome readers keep their existing narrow predicates.
