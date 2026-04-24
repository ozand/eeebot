# Autonomous Execution and Stagnation Control

Last updated: 2026-04-16 UTC

## Objective

Prevent the Nanobot eeepc project from sitting in a stagnant state while the operator assumes progress is happening.

## Failure We Observed

During the monitored window, the system spent hours in repeated `BLOCK` while:
- the active goal stayed the same
- the live report source stayed the same
- approval remained valid
- no new PASS cycles appeared

This means the failure mode was not infrastructure outage.
It was unattended stagnation.

## Process Changes

### 1. Stagnation is a first-class incident
Treat these as incidents, not passive observations:
- same goal + same report source + `BLOCK` over multiple collections
- no new PASS in a defined window
- repeated `failure_class` such as `no_concrete_change`

### 2. Separate roles explicitly
The system needs four ongoing functions:
- observer: collects and surfaces truth
- diagnostician: identifies blocker class and trend
- producer: turns a bounded corrective recommendation into a queued remediation task
- executor: consumes the first queued task and takes the next bounded step toward the project goal

The dashboard already covers observer truth.
A supervisor loop must now cover diagnostician escalation and queue hygiene.

### 3. Escalation thresholds
Escalate when any of the following are true:
- no new PASS in the last 90 minutes
- latest 6 collections are all `BLOCK`
- same `report_source` persists across the last 6 eeepc snapshots
- same `failure_class` repeats across recent eeepc cycle events

### 4. Required response to escalation
When stagnation is detected, the system should automatically produce:
- current blocker summary
- trend summary
- what changed since the last good PASS
- the single best next bounded action

### 5. No silent waiting
The system should not assume the operator will remember to ask again.
A scheduled supervisor must proactively review the dashboard state and report.

## Concrete Control Loop

1. collector keeps snapshots flowing into SQLite
2. stagnation script computes latest trend and incident status
3. scheduled supervisor runs on a cadence
4. supervisor reports status and, if needed, recommends or takes the next bounded slice

## Current Implementation Decision

For now, implement:
- a local stagnation analysis script in the dashboard project
- an active remediation candidate generator that reads the same dashboard SQLite state and emits one bounded corrective action without changing anything
- an autonomous cron supervisor that runs periodically and reports to the current chat
- a project-level autonomy registry/runbook that records ownership, escalation thresholds, and execution roles for every active project
- an additional control job that checks ownership drift and review freshness, not just stagnation

This is the minimum viable process change that removes dependence on a manual nudge.

## Status Heartbeat Transparency Layer

The dashboard repo now also carries a durable status heartbeat layer so the current autonomy state is visible without reading chat transcripts.

Included assets:
- `control/active_projects.json`
- `control/active_execution.json`
- `scripts/build_status_snapshot.py`
- `scripts/stale_execution_watchdog.py`
- `scripts/consume_stale_execution_incidents.py`
- `scripts/consume_stale_execution_next_actions.py`
- `scripts/consume_queued_redispatch_assignments.py`

Behavior:
- keep the active-project registry explicit and machine-readable
- keep the execution-work registry separate and machine-readable
- summarize active projects alongside the current execution-work registry snapshot
- make the current project stage, ownership posture, and execution-work lifecycle easy to inspect as part of the autonomy system
- when the watchdog says the live execution is stale after 30 minutes, let the stale-incident controller write a durable stale incident record, mark the queue item `stale_blocked`, and emit one bounded redispatch candidate
- then let the stale-next-action redispatch controller consume that candidate and turn the queue item back into a fresh queued redispatch line linked to the stale incident
- then let the queued-redispatch assignment controller consume that queued line, write a durable execution-assignment artifact, and restore a fresh live delegated execution line
- support the control job described above without introducing a new execution model

## Execution queue layer

A diagnosis is not enough. The system must translate actionable incidents into durable bounded tasks, then hand them off through a deterministic executor request layer and a final executor-handoff layer.

The control repo now includes:
- `scripts/analyze_active_remediation.py`
- `scripts/enqueue_active_remediation.py`
- `scripts/consume_execution_queue.py`
- `scripts/consume_stale_execution_incidents.py`
- `scripts/consume_stale_execution_next_actions.py`
- `scripts/consume_queued_redispatch_assignments.py`
- `scripts/consume_execution_requests.py`
- `scripts/consume_executor_handoffs.py`
- `scripts/consume_pi_dev_requests.py`
- `scripts/consume_pi_dev_bundles.py`
- `scripts/consume_pi_dev_dispatches.py`
- `scripts/consume_delegated_executor_requests.py`
- `control/execution_queue.json`
- `control/execution_dispatch.json`
- `control/dispatched/<timestamp>-<task-key>.json`
- `control/execution_requests/<timestamp>-<task-key>.json`
- `control/executor_handoffs/<timestamp>-<task-key>.json`
- `control/pi_dev_requests/<timestamp>-<task-key>.json`
- `control/pi_dev_bundles/<timestamp>-<task-key>.json`
- `control/pi_dev_dispatches/<timestamp>-<task-key>.json`
- `control/pi_dev_dispatches/<timestamp>-<task-key>.prompt.txt`
- `control/pi_dev_dispatches/<timestamp>-<task-key>.sh`
- `control/pi_dev_dispatch.json`
- `control/delegated_executor_requests/<timestamp>-<task-key>.json`
- `control/delegated_executor_request.json`
- `control/execution_assignments/<timestamp>-<task-key>.json`
- `control/execution_assignment.json`
- `control/execution_completions/<timestamp>-<task-key>.json`
- `control/execution_completion.json`
- `control/stale_execution_incidents/<timestamp>-<task-key>.json`
- `control/stale_execution_next_actions/<timestamp>-<task-key>.json`
- `control/stale_execution_redispatches/<timestamp>-<task-key>.json`
- `control/stale_execution_incident.json`
- `control/stale_execution_next_action.json`
- `control/stale_execution_redispatch.json`

Behavior:
- read the current remediation analysis
- if the diagnosis is actionable, enqueue one bounded remediation task
- deduplicate open tasks against the same goal/report/failure class
- dispatch at most one queued task per consumer run
- mark the first queued task `in_progress` and stamp `dispatched_at`
- record the execution-work registry separately so status reporting can tell whether a delegated task is truly live or only project-owned
- write a durable dispatch artifact for auditability
- then, once a dispatched task is eligible, create a durable execution request artifact
- transition that task to `requested_execution` and stamp `execution_requested_at`
- record the requested executor plus source queue/dispatch artifact references
- then, once an execution request is eligible, create a durable executor handoff artifact
- transition that task to `handed_off` and stamp `executor_handoff_at`
- record the requested executor plus source execution request path
- if the first task is already handed off, report that and do not advance later tasks
- then, once a Pi Dev bundle is eligible, create a durable dispatch bridge artifact and prompt/command bundle
- transition that request/task to `pi_dev_dispatch_ready` and stamp `pi_dev_dispatch_created_at`
- record the source Pi Dev request path, bundle path, prompt path, script path, and runnable command
- the bridge layer is intentionally truthful: it may prepare an explicit invocation command without claiming Pi Dev execution succeeded
- avoid leaving corrective action as a purely verbal recommendation
- the live queue is cycle-scoped: if a newer task with the same dedupe key appears, normalize the queue to keep the newest live cycle and preserve the earlier dispatch/request/handoff artifacts in their own artifact directories
- same-cycle status progression is monotonic (`queued` -> `in_progress` -> `requested_execution` -> `handed_off` -> `completed` when the bounded implementation and verification both finish)
- project-level `in_progress` is not the same as a live delegated execution task
- when a bounded delegated execution is verified complete, record the durable completion artifact and clear the pseudo-live executor line from the current snapshot
- the delegated executor request pointer stays historical, but the current request/assignment/completion pointers must show `completed` once the completion proof exists
