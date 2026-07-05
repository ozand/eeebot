# Change: fix dashboard false-stagnant and cross-cycle subagent_consumption gaps from the #571 grace period

- **change-id:** dashboard-and-consumption-grace-period-regressions
- **issue:** #572
- **capability:** `docs/specs/subagent-bridge`
- **role / workstream:** role:developer / workstream:runtime

## Problem

#571 added `BOUNDED_EXECUTION_GRACE_SECONDS = 1800` so a fresh `bounded_execution`
request is left queued (no result) for up to 30 minutes instead of being
terminalized immediately. Two consumers weren't updated for this and are
confirmed broken by code review + reading the logic:

1. `ops/dashboard/src/nanobot_ops_dashboard/app.py`'s `_discover_subagent_requests`
   already computes per-request `age_seconds` (mtime-based, line ~1426), but
   `queued_subagent_request_count` (line ~1528) counts every queued request with
   no age filter, and `unresolved_subagent_request` (lines ~2122-2127) — which
   forces `autonomy_verdict.status = "stagnant"` — trips whenever
   `queued_count > 0` regardless of age. Every normal `bounded_execution` cycle
   now falsely reports "stagnant" for up to 30 minutes.
2. `nanobot/runtime/coordinator.py`'s `_subagent_consumption_snapshot`
   (~line 2104) only accepts a materializer result as a match when
   `"cycle_id" in match_reasons or "report_path" in match_reasons`. The
   materializer stamps the request's *original* `cycle_id` into the result,
   never a `report_path`. When a request is terminalized in a later cycle than
   the one that created it (now routine), the terminalizing cycle's own
   `cycle_id` won't equal the request's original one, so the match fails and
   `subagent_consumption` silently never populates for that request.

## Intended change

### 1. Dashboard: age-aware unresolved check

Import `BOUNDED_EXECUTION_GRACE_SECONDS` from `nanobot.runtime.subagent_materializer`
into `app.py`. In `_discover_subagent_requests`'s result (or the call site that
derives `unresolved_subagent_request`), compute a grace-aware count — e.g.
`queued_beyond_grace_count`: the number of queued requests that are either not
`profile: bounded_execution`, or are `bounded_execution` AND
`age_seconds >= BOUNDED_EXECUTION_GRACE_SECONDS`. Use this new count (not the
raw `queued_subagent_request_count`) to gate `unresolved_subagent_request`.
`queued_subagent_request_count` itself (the raw display figure) is unchanged —
this only narrows the "stagnant" trigger, so a genuinely stuck request (past
the grace period, still no result) still correctly trips stagnant.

### 2. Coordinator: cross-cycle `subagent_consumption` matching

`_write_subagent_request_artifact`'s path is only recorded onto `current_plan`
in the cycle that creates it (`coordinator.py:4818-4819`). Carry it forward
across cycles when no new request is written this cycle:
```python
current_plan["subagent_request_path"] = subagent_request_path or (
    recorded_task_plan.get("subagent_request_path")
    if isinstance(recorded_task_plan, dict) else None
)
```
Pass this persisted path into `_subagent_consumption_snapshot` as a new
parameter (e.g. `tracked_request_path`). Inside the function, add a new
`match_reasons` entry when `payload.get("request_path") == tracked_request_path`
(the materializer result's `request_path` field is the original request
file's path — stable and always populated), and include this reason in the
acceptance gate alongside the existing `cycle_id`/`report_path` checks. This
lets a result terminalized in a later cycle still be attributed correctly, as
long as the request path is still being tracked (i.e. hasn't been superseded
by a newer request).

## Acceptance

- [ ] A queued `bounded_execution` request younger than the grace period does
      not cause `unresolved_subagent_request`/`autonomy_verdict.status` to be
      `"stagnant"` (test).
- [ ] Regression guard: a request queued well past the grace period with still
      no result correctly still trips `stagnant` (test — the existing test
      `test_autonomy_verdict_blocks_healthy_progress_when_subagent_request_is_queued_without_result`
      must keep passing unmodified since it constructs the "genuinely stuck"
      case directly).
- [ ] A subagent result terminalized in a cycle later than the one that
      created its request still populates `current["subagent_consumption"]`
      (test).
- [ ] Regression guard: the existing same-cycle grace-period test
      (`test_cycle_executes_configured_subagent_executor_and_consumes_completed_result`)
      keeps asserting no consumption happens in the SAME cycle a fresh request
      is written (grace period still applies) — this change only fixes
      *later*-cycle attribution, it does not shrink the grace period.
- [ ] Full test suite green; deployed to eeepc and verified (dev → test →
      rollout).

## Out of scope

- Re-litigating the grace-period mechanism itself (#571).
- The broader altitude concerns from #571's review (hardcoded profile
  special-case instead of a general consumer/ownership field; grace period
  constant not linked to the bridge's timer cadence) — a future design pass,
  not this bugfix.
- Any change to `_subagent_rollup_snapshot`/`state.py`'s separate disk-rescan
  path unless it turns out to feed the same `unresolved_subagent_request`
  computation (verify during implementation; only touch if actually in the
  path).
