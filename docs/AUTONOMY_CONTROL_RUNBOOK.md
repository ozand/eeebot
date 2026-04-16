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
- no new PASS for 90 minutes
- last 6 collections all BLOCK
- same report source persists across the last 6 collections
- same goal persists across the last 6 collections
- repeated failure class, especially `stagnating_on_quality_blocker`

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
1. read the registry and current Nanobot stagnation analysis
2. identify any overdue review or ownership gap
3. report the exact next bounded action
4. if Nanobot is stagnating, prioritize the blocker and the smallest safe fix
5. if a project is healthy, still confirm the next review time rather than going silent

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

The system should use both, so Hermes does not merely report that work is stuck; it also keeps projects owned and moving.
