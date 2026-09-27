# Design: incomplete model-call classification and retry separation

## Classification

- `paused-supplier` is an affirmative infrastructure signal, matched via positive evidence. HTTP status numbers must have error/status context, so numeric payloads such as an output-token ceiling cannot imply provider outage.
- `model_call_incomplete` requires persisted telemetry identifying the model-call stage. Tool execution, response handling, and local code failures do not receive the classification.
- Affirmative client/request rejection evidence (known request-validation error types or invalid-parameter/context/tool-payload markers) retains the ordinary `failed` path even when the model-call stage is known; supplier evidence takes precedence.
- Other errors retain the existing ordinary failure path.

## Retry and diagnostics

Supplier pauses leave the request pending without consuming retry state. Incomplete calls use `retry_incomplete_<id>.json`, bounded independently from `retry_<id>.json`; exhaustion pauses automatic retry and calls for operator request-size/budget inspection. Incomplete-call failures bypass recent-failure suppression while retry budget remains; once exhausted, matching proposals enter the ordinary bounded suppression window. Error-card recording reports the counter selected by the rollback classification, avoiding stale or missing attempt numbers.

## Reader contract

The new outcome remains separate from ordinary execution failures in scorecard fields and daily movement, is treated as infrastructure in lessons/futility readers, and is retained in raw loop metrics. Success-only and unrelated outcome readers keep their existing narrow predicates.
