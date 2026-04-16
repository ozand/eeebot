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
- `scripts/build_status_snapshot.py`

Behavior:
- keep the active-project registry explicit and machine-readable
- summarize active projects alongside the live execution queue
- make the current project stage and ownership posture easy to inspect as part of the autonomy system
- support the control job described above without introducing a new execution model

## Execution queue layer

A diagnosis is not enough. The system must translate actionable incidents into durable bounded tasks, then hand them off through a deterministic executor request layer and a final executor-handoff layer.

The control repo now includes:
- `scripts/analyze_active_remediation.py`
- `scripts/enqueue_active_remediation.py`
- `scripts/consume_execution_queue.py`
- `scripts/consume_execution_requests.py`
- `scripts/consume_executor_handoffs.py`
- `control/execution_queue.json`
- `control/execution_dispatch.json`
- `control/dispatched/<timestamp>-<task-key>.json`
- `control/execution_requests/<timestamp>-<task-key>.json`
- `control/executor_handoffs/<timestamp>-<task-key>.json`

Behavior:
- read the current remediation analysis
- if the diagnosis is actionable, enqueue one bounded remediation task
- deduplicate open tasks against the same goal/report/failure class
- dispatch at most one queued task per consumer run
- mark the first queued task `in_progress` and stamp `dispatched_at`
- write a durable dispatch artifact for auditability
- then, once a dispatched task is eligible, create a durable execution request artifact
- transition that task to `requested_execution` and stamp `execution_requested_at`
- record the requested executor plus source queue/dispatch artifact references
- then, once an execution request is eligible, create a durable executor handoff artifact
- transition that task to `handed_off` and stamp `executor_handoff_at`
- record the requested executor plus source execution request path
- if the first task is already handed off, report that and do not advance later tasks
- avoid leaving corrective action as a purely verbal recommendation
- the live queue is cycle-scoped: if a newer task with the same dedupe key appears, normalize the queue to keep the newest live cycle and preserve the earlier dispatch/request/handoff artifacts in their own artifact directories
- same-cycle status progression is monotonic (`queued` -> `in_progress` -> `requested_execution` -> `handed_off`)
