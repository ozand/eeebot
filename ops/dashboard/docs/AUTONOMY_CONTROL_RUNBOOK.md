# Autonomy Control Runbook

Last updated: 2026-04-16 UTC

This runbook defines the host-level control loop that moves Hermes from passive reporting toward explicit ownership.

## Purpose

The control loop has three goals:
- keep active projects clearly owned
- escalate when progress stalls or ownership becomes vague
- preserve bounded execution so Hermes does not widen scope unsafely

## Canonical control artifact

Machine-readable registry:
- `docs/autonomy_control_registry.json`

Status heartbeat registries:
- `control/active_projects.json`
- `control/active_execution.json`
- `control/execution_completion.json`
- `control/eeepc_reachability.json`
- `control/status_feed.jsonl`

Status heartbeat snapshot/feed generators:
- `scripts/build_status_snapshot.py`
- `scripts/build_status_feed.py`
- `scripts/stale_execution_watchdog.py`
- `scripts/eeepc_reachability_watchdog.py`

Human-readable policy summary:
- this runbook

## Roles

- observer: records facts and snapshots
- diagnostician: classifies blockers and detects drift
- executor: takes the next bounded slice of work
- owner: accountable for the project’s forward motion
- approver: authorizes releases, infra changes, and risky scope changes
- operator: final human fallback on this host

## Escalation thresholds

Global ownership thresholds:
- ownership review every 24 hours for active projects
- escalate if owner or executor is missing
- escalate if review is overdue
- escalate if a release gate blocks progress

Nanobot-specific stagnation thresholds:
|- any live delegated `in_progress` task older than 30 minutes must be investigated and, if needed, escalated
|- no new PASS for 90 minutes
- last 6 collections all BLOCK
- same report source persists across the last 6 collections
- same goal persists across the last 6 collections
- repeated failure class, especially `stagnating_on_quality_blocker`

## eeepc reachability incidents

Host access loss is now a first-class control-plane incident.

The watchdog `scripts/eeepc_reachability_watchdog.py` must be used to distinguish:
- eeepc reachable, but collection may still fail for state reasons
- eeepc unreachable, which is a control-plane incident and not a content/collector symptom

When the reachability probe reports `reachable: false`:
- treat the incident as explicit control-plane loss of host access
- do not retry with broad network repair or manual probing loops
- record the watchdog output in `control/eeepc_reachability.json`
- expect the dashboard to surface the incident in the live eeepc status card and blocker view
- follow the `recommended_next_action` exactly: verify power/network access, then retry collection after reachability returns

This distinction matters because cadence analysis and blocker diagnosis cannot be trusted when the host is unreachable.

## Active project ownership

The registry explicitly covers these active projects:
- Nanobot eeepc control loop
- OpenSpace
- aparser-cli
- private-skills-repo

Each project entry must include:
- owner
- executor_role
- approver_role
- review interval
- next bounded action
- escalation thresholds

## How Hermes should behave

When the control job runs:
1. refresh the status heartbeat snapshot from `control/active_projects.json`, `control/active_execution.json`, `scripts/build_status_snapshot.py`, and append one local line to `control/status_feed.jsonl` via `scripts/build_status_feed.py`
2. read the registry and current Nanobot stagnation analysis from `scripts/analyze_stagnation.py`
3. run the active remediation candidate generator in `scripts/analyze_active_remediation.py` to turn a stagnant state into one bounded corrective action
4. enqueue that action in `control/execution_queue.json` when appropriate
5. run `scripts/consume_stale_execution_incidents.py` so any truly stale live execution is truthfully marked `stale_blocked` and a bounded redispatch candidate is emitted
6. run `scripts/consume_stale_execution_next_actions.py` so one eligible stale next-action artifact becomes a fresh queued redispatch task linked back to the stale incident
7. run `scripts/consume_queued_redispatch_assignments.py` so one eligible queued redispatch task becomes a fresh live delegated execution assignment
8. run the execution consumer in `scripts/consume_execution_queue.py` to dispatch at most one queued remediation task and persist a dispatch artifact
9. identify any overdue review or ownership gap
10. report the exact next bounded action
11. if Nanobot is stagnating, prioritize the blocker and the smallest safe fix
12. if a project is healthy, still confirm the next review time rather than going silent

## Execution queue and dispatch

Project ownership and delegated execution are separate facts:
- `control/active_projects.json` records project-level ownership and stage
- `control/active_execution.json` records whether a bounded delegated task is queued, in progress, waiting for dispatch, blocked, stale_blocked, needs_redispatch, or completed
- a project can remain `in_progress` even when there is no live delegated execution task
- status reporting must not collapse those two layers into one
- `scripts/stale_execution_watchdog.py` treats a live `in_progress` task as stale once it exceeds the 30-minute investigation threshold and emits a bounded JSON incident record
- `scripts/consume_stale_execution_incidents.py` consumes the watchdog/control state, writes a durable stale incident plus next-action artifact, and truthfully converts the live queue item to `stale_blocked`
- `scripts/consume_stale_execution_next_actions.py` consumes at most one eligible stale next-action artifact, writes a durable redispatch artifact, and turns the queue item back into a fresh queued redispatch line linked to the stale incident
- `scripts/consume_queued_redispatch_assignments.py` consumes at most one eligible queued redispatch task, writes a durable execution-assignment artifact, and turns the queue item into a fresh live delegated execution line linked to the stale incident and redispatch artifacts
- when a delegated execution finishes and the bounded implementation plus eeepc side-by-side verification both pass, write an execution completion artifact, mark the queue/assignment/request pointers `completed`, and let the live snapshot collapse to terminal evidence instead of a pseudo-live executor line

The autonomy control loop now has a clear handoff:
- producer: `scripts/enqueue_active_remediation.py`
- dispatch consumer: `scripts/consume_execution_queue.py`
- stale incident controller: `scripts/consume_stale_execution_incidents.py`
- stale next-action controller: `scripts/consume_stale_execution_next_actions.py`
- queued redispatch execution-assignment controller: `scripts/consume_queued_redispatch_assignments.py`
- execution request consumer: `scripts/consume_execution_requests.py`
- executor handoff consumer: `scripts/consume_executor_handoffs.py`
- Pi Dev request consumer: `scripts/consume_pi_dev_requests.py`
- Pi Dev bundle consumer: `scripts/consume_pi_dev_bundles.py`
- Pi Dev dispatch bridge: `scripts/consume_pi_dev_dispatches.py`
- queue: `control/execution_queue.json`
- dispatch artifact: `control/execution_dispatch.json` or `control/dispatched/<timestamp>-<task-key>.json`
- stale incident artifact: `control/stale_execution_incidents/<timestamp>-<task-key>.json`
- stale next-action artifact: `control/stale_execution_next_actions/<timestamp>-<task-key>.json`
- stale redispatch artifact: `control/stale_execution_redispatches/<timestamp>-<task-key>.json`
- execution assignment artifact: `control/execution_assignments/<timestamp>-<task-key>.json`
- stale incident pointer: `control/stale_execution_incident.json`
- stale next-action pointer: `control/stale_execution_next_action.json`
- stale redispatch pointer: `control/stale_execution_redispatch.json`
- execution assignment pointer: `control/execution_assignment.json`
- execution request artifact: `control/execution_requests/<timestamp>-<task-key>.json`
- executor handoff artifact: `control/executor_handoffs/<timestamp>-<task-key>.json`
- Pi Dev request artifact: `control/pi_dev_requests/<timestamp>-<task-key>.json`
- Pi Dev execution bundle artifact: `control/pi_dev_bundles/<timestamp>-<task-key>.json`
- Pi Dev dispatch bridge artifact: `control/pi_dev_dispatch.json` or `control/pi_dev_dispatches/<timestamp>-<task-key>.json`

The consumers must be deterministic and bounded:
- inspect the first queued task only when dispatching
- transition at most one task to `in_progress` per dispatch run
- stamp `dispatched_at`
- if the first task is already `in_progress`, `completed`, or `cancelled`, report that and do not consume a later task
- when a task is already `in_progress` or `dispatched`, transition at most one task to `requested_execution` per executor-request run
- stamp `execution_requested_at`
- record the requested executor and source queue/dispatch artifact references
- if the first task is already handed off, report that and do not advance later tasks
- when a requested execution is eligible, transition at most one task to `handed_off` per executor-handoff run
- stamp `executor_handoff_at`
- write a durable executor handoff artifact that records the diagnosis, active goal, failure class, remediation class, requested executor, and source execution request path
- if the first request is already handed off, skip it and do not consume a later request unless it is the first eligible one
- when a Pi Dev request is eligible, transition at most one request to `bundled` per bundle run and write a concise executor-oriented execution bundle artifact
- stamp `bundled_at`
- record the source Pi Dev request path, queue task key, and bounded instruction text in the bundle artifact
- if the first Pi Dev request is already bundled, skip it and do not consume a later request unless it is the first eligible one
- when a Pi Dev bundle is eligible, transition at most one request to `pi_dev_dispatch_ready` per dispatch-bridge run and write a durable bridge artifact plus runnable command and prompt bundle
- stamp `pi_dev_dispatch_created_at`
- record the source Pi Dev request path, bundle path, queue task key, prompt path, script path, and the explicit runnable Pi Dev command in the dispatch bridge artifact
- if the first Pi Dev bundle is already dispatch-ready, skip it and do not consume a later request unless it is the first eligible one
- the bridge layer must not claim Pi Dev execution success unless the command is actually run and its result is captured truthfully
- when the Pi Dev command is blocked by a local provider/model mismatch, create a durable `delegated_executor_requests/<timestamp>-<task-key>.json` fallback artifact, point the live queue at that fallback request, and mark the task `in_progress` so the execution registry reflects the active delegated fallback path rather than silent waiting
- the fallback artifact must record the blocked Pi Dev command, the failure reason, the source dispatch bridge, and the requested Hermes/subagent executor path
- when the bounded delegated executor later completes and verification passes, add `control/execution_completions/<timestamp>-<task-key>.json` and transition the current pointer files to `completed` so the dashboard no longer shows a live executor unless one really exists
- when `scripts/consume_stale_execution_incidents.py` sees a stale live execution older than 30 minutes, it must write one durable incident artifact, mark exactly one queue item `stale_blocked`/`needs_redispatch`, preserve the prior evidence, and emit exactly one bounded redispatch candidate artifact
- when `scripts/consume_stale_execution_next_actions.py` sees that redispatch candidate, it must consume at most one eligible stale next-action artifact, write one durable redispatch artifact, and convert the queue item into a fresh queued redispatch line linked to the stale incident
- the stale incident and stale next-action controllers must both be idempotent; once the queue item already carries stale incident or redispatch markers, a rerun should not fabricate a completion or create a second trail
- treat queued/requested_execution/handed_off as one monotonic lifecycle for a single task record; the live queue should only retain the newest cycle for a dedupe key, while older dispatch/request/handoff artifacts remain in their artifact directories
- treat requested/bundled as the Pi Dev handoff-preparation lifecycle for a single request record; the live queue/request artifacts should point at the latest bundle path for that request
- treat bundled/pi_dev_dispatch_ready as the Pi Dev dispatch-preparation lifecycle for a single request record; the live queue/request/bundle artifacts should point at the latest dispatch bridge path for that request
- use `scripts/normalize_execution_queue.py` when the live queue drifts and contains multiple records for the same dedupe key

## Safe operating rules

- Do not broaden scope in a cron job.
- Do not create nested cron jobs from cron output.
- Do not change release or infrastructure state unless the runbook or registry explicitly allows it.
- Prefer one bounded file-level change, one verification step, or one explicit escalation.

## Operational interpretation

Healthy state means:
- every active project has an owner and executor role recorded
- the next review time is visible
- there is a concrete next bounded action
- the gateway is still running and cron remains scheduled

Action required means any of the following:
- ownership missing
- review overdue
- stagnation threshold breached
- the smallest safe next step is not obvious

## Where this fits

This control loop complements the hourly stagnation reporter:
- stagnation reporter = incident detection
- autonomy control job = ownership and execution hygiene
- status heartbeat transparency layer = durable active-project visibility, live queue context, and append-only local status feed evidence

The system should use both, so Hermes does not merely report that work is stuck; it also keeps projects owned and moving.
