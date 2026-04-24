# Nanobot Ops Dashboard v1 Summary

Status: complete and manually verified on this host.

What exists:
- separate project repo under `Projects/nanobot-ops-dashboard`
- SQLite-backed history retention
- local CLI commands:
  - `init-db`
  - `collect-once`
  - `poll`
  - `serve`
- live eeepc collection over SSH with optional sudo password env
- local repo-side state collection with graceful fallback when workspace-state is absent
- local web UI pages:
  - overview
  - cycles
  - promotions
  - approvals
  - deployments
  - analytics
  - subagents
- current blocker analysis for eeepc when process-reflection data is present
- streak and trend analytics for cycle status history
- top-goal frequency summary for observed cycle history
- top BLOCK reason summary for observed cycle history
- latest artifact history extracted from stored cycle events
- recent cycle timeline summary for operator-facing quick reading
- recent goal transition summary for operator-facing quick reading
- machine-readable endpoints:
  - `/api/summary`
  - `/api/cycles`
  - `/api/promotions`
  - `/api/approvals`
  - `/api/deployments`
  - `/api/analytics`
- tests for storage, collector, polling, and app rendering

What was manually verified:
- test suite passes
- eeepc live state can be collected into SQLite
- historical snapshots accumulate in `collections`
- local web server starts successfully on `127.0.0.1:8787`
- all pages return HTML and contain expected content
- canonical user services can run the dashboard web UI and collector continuously
- `/cycles` correctly renders approval states like `PASS (fresh)` and `BLOCK (missing)`
- `/system` renders eeepc goal/system-file visibility and local repo docs
- `/experiments` renders reward/failure, credits, used calls, and task linkage
- `/subagents` renders current task / reward / feedback correlation

Canonical runtime mode now exists:
- `scripts/run_web.sh`
- `scripts/run_collector.sh`
- `scripts/install_user_units.sh`
- `systemd/nanobot-ops-dashboard-web.service`
- `systemd/nanobot-ops-dashboard-collector.service`

Current known limitation:
- durable subagent telemetry is not emitted by Nanobot yet, so the dashboard correctly reports that this data source is unavailable instead of inventing it.
