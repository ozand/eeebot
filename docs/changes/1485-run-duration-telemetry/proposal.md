# Run-duration telemetry (#1485)

## Problem

Bridge invocation duration is currently reconstructed from journal timestamps. That
loses the distribution when journal retention or systemd metadata is unavailable,
and it cannot distinguish a normal completion from a loop-breaker abort or a unit
timeout.

## Intended change

Persist one bounded, queryable `run_end` record for each bridge invocation in the
runtime state. The record contains start/end timestamps, duration, a run identity,
optional cycle/request identity, terminal outcome, and a truthful run-end
classification. Records rotate daily and use the existing 90-day retention
horizon. Readers use `state_access` and preserve `partial`, `unavailable`, and
`beyond_retention` status rather than treating missing history as zero.

The artifact is deliberately separate from the cycle ledger: a bridge process can
end before it has selected or started a cycle, so a run must not be falsely mapped
to a cycle outcome.

## Out of scope

- Dashboard presentation or new CLI output.
- Changing loop-breaker thresholds or timeout values.
- Retrospective reconstruction of historical runs that predate this artifact.
- Host mutation, deployment, or service configuration changes.

## Acceptance

- Every armed bridge process creates a run marker and every observable process or
  systemd exit materializes at most one `run_end` row with duration.
- Cycle identity and loop-breaker classification are recorded when available;
  systemd timeout exits are classified as `unit_timeout`.
- Run rows rotate and prune under the existing bounded retention policy.
- `state_access.run_window` reads the active and rotated artifacts and exposes
  unavailable/beyond-retention conditions.
- Focused tests cover completion, loop-breaker metadata, state-access reads, and
  retention boundaries without changing the existing exit-streak contract.
