# Change: stop the health cycle from prematurely blocking bridge-destined requests

- **change-id:** subagent-request-consumer-race
- **issue:** #570
- **capability:** `docs/specs/subagent-bridge`
- **role / workstream:** role:developer / workstream:runtime

## Problem

`materialize_subagent_requests` (`nanobot/runtime/subagent_materializer.py:266`) is
called unconditionally on **every** self-evolving cycle, immediately after
`_write_subagent_request_artifact` writes a fresh `bounded_execution` request
(`coordinator.py:4811` → `4821`) — in the same cycle, before the independently
scheduled subagent-bridge systemd timer has any chance to claim it.

The health-cycle process has no local executor configured
(`NANOBOT_SUBAGENT_EXECUTOR`/`NANOBOT_SUBAGENT_EXECUTOR_COMMAND` are set for the
bridge's systemd unit, not this process), so `configured_executor` is falsy
(`subagent_materializer.py:301-302`), and the request is immediately
terminalized as `result_status: "blocked"` / `terminal_reason:
"local_executor_unavailable"` (`:344-393`). The `profile` field on the request
(`"bounded_execution"`, set at `coordinator.py:2902`) is read only for cosmetic
echoing into the result/blocker — never used to decide "this belongs to a
different consumer, leave it alone."

The bridge script already anticipates this: `_is_real_result` in both bridge
copies treats a `materialized_from: "queued_request_terminalizer"` /
`local_executor_unavailable` result as **not a real handling** and will still
process the request on its own next tick — but only if the request file
itself is still present and `queued` in `state/subagents/requests/`, which it
is (the materializer never mutates/moves/deletes the request file itself,
confirmed by reading the code — only `archive_stale_requests`, a separate
4-hour-cutoff step, physically moves files). So today's behavior is not fatal
data loss, but it does: (a) produce a confusing/noisy permanent `blocked`
result artifact for a request the bridge later handles fine, and (b) risks a
race where a *slow* bridge tick could still leave a stale-looking "blocked"
result as the only signal an operator sees for a while.

## Intended change

In `materialize_subagent_requests`'s request-processing loop
(`subagent_materializer.py:325-343`), before falling through to the
executor/blocking logic: if a request's `profile` is `"bounded_execution"`
(the bridge's own dispatch profile) AND the request is younger than a short
grace period, skip it (`skipped += 1; continue`) — leave it untouched in
`requests/` for the bridge to claim via its own polling. If the request is
**older** than the grace period (meaning the bridge has had several ticks and
still hasn't claimed it — e.g. the bridge service is down or broken), fall
through to the existing termination logic unchanged, so a genuinely stuck
request still gets a legitimate `blocked` fallback rather than sitting
invisible forever.

Grace period: `BOUNDED_EXECUTION_GRACE_SECONDS = 1800` (30 minutes — roughly 3
bridge timer ticks at the current ~10-minute cadence), a module-level
constant next to the existing `archive_stale_requests` 4-hour cutoff. Age is
computed from the request file's mtime (same source `archive_stale_requests`
already uses), no new field needed on the request schema.

## Acceptance

- [ ] A fresh (< 30 min old) `bounded_execution`-profile request is skipped
      by `materialize_subagent_requests` (not terminalized to `blocked`),
      leaving it queued for the bridge (test).
- [ ] A `bounded_execution`-profile request older than the grace period is
      still terminalized to `blocked`/`local_executor_unavailable` exactly as
      today (regression/fallback guard — test).
- [ ] Non-`bounded_execution` profiles (`research_only`, `review_only`,
      `bounded_review`) are completely unaffected — existing tests in
      `tests/test_runtime_coordinator.py` (`test_subagent_materializer_*`)
      continue to pass unmodified.
- [ ] Full test suite green; deployed to eeepc and verified (dev → test →
      rollout) — a fresh materialize-lane dispatch request is no longer
      immediately blocked by the health cycle.

## Out of scope

- Rewriting the bridge's own `_is_real_result`/pickup logic — it already
  correctly treats `queued_request_terminalizer` blocks as non-terminal; this
  change just avoids producing that noisy artifact in the common case.
- Reconciling the diverged `scripts/eeepc_self_evolving_subagent_bridge.py`
  vs. `host/eeepc/libexec/eeepc-self-evolving-subagent-bridge.py` copies —
  separate pre-existing drift, not this issue's concern.
- Adding a new `consumer`/`owner` field to the request schema — reusing the
  existing `profile` field is sufficient and additive.
