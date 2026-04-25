# eeebot Ops Dashboard

Local observability dashboard for eeebot.

Canonical source:
- durable product source now lives in `ozand/eeebot` under `ops/dashboard/`
- local canonical path: `/home/ozand/herkoot/Projects/nanobot/ops/dashboard`
- the sibling repository `ozand/eeebot-ops-dashboard` is a staging/mirror/legacy reference and is not the durable source of truth for future product work

Compatibility note:
- the imported dashboard still exposes the `eeebot-ops-dashboard` project identity where that is user-facing
- many local paths, services, package names, and collected control artifacts still carry `nanobot` names for compatibility with the existing runtime and deployed host
- those internal names should be migrated only in a separate controlled compatibility tranche

Purpose:
- run on this host, not on eeepc
- ingest live eeepc self-evolving state over SSH
- ingest local repo-side eeebot bounded-runtime state
- keep historical snapshots in SQLite
- present a local web UI for current and past cycles, goals, promotions, approvals, deployments, and any available subagent telemetry

Current scope of v1:
- overview page with latest-source summaries, blocker analysis, quick links, and compact status-emphasized timelines
- hypotheses/backlog page with HADI + explicit WSJF and execution spec visibility
- cycle history page with visible filter form and PASS/BLOCK/unknown badges
- promotions page with visible filter form and promotion-status badges
- approvals/capability page
- deployments/verification page with repo-vs-eeepc divergence visibility
- experiments page with reward, budget, credits summary, used-call visibility, and task linkage
- credits ledger page
- system files page for eeepc goal/system files and local repo docs
- analytics page with status-emphasized counters, recent snapshots, recent cycles, failure-class breakdown, streaks, top-goal frequency, top BLOCK reasons, artifact history, and recent goal transitions
- subagents page with durable task/goal/cycle correlation visibility
- compatibility service units for both names:
  - `nanobot-ops-dashboard-*.service`
  - `eeebot-ops-dashboard-*.service`
- `/api/summary` machine-readable endpoint
- `/api/cycles` machine-readable history endpoint
- `/api/promotions` machine-readable history endpoint
- `/api/approvals` machine-readable operational endpoint
- `/api/deployments` machine-readable deployment/proof endpoint
- `/api/system` machine-readable system/current-proof endpoint
- `/api/analytics` machine-readable analytics endpoint
- `/api/hypotheses` machine-readable HADI/WSJF backlog endpoint
- `/api/plan` machine-readable task-plan/reward endpoint
- `/api/experiments` machine-readable experiments/budget/credits endpoint
- `/api/credits` machine-readable credits ledger endpoint
- autonomy control artifacts for project ownership, status-heartbeat transparency, escalation thresholds, and execution roles

The dashboard is intentionally dependency-light:
- Python stdlib
- SQLite
- Jinja2
- system ssh/scp

Quick start:

```bash
cd /home/ozand/herkoot/Projects/nanobot/ops/dashboard
PYTHONPATH=src python3 -m nanobot_ops_dashboard init-db
PYTHONPATH=src NANOBOT_EEEPC_SUDO_PASSWORD='<set-in-env-file>' python3 -m nanobot_ops_dashboard collect-once
PYTHONPATH=src NANOBOT_EEEPC_SUDO_PASSWORD='<set-in-env-file>' python3 -m nanobot_ops_dashboard serve --host 127.0.0.1 --port 8787
```

Then open:
- `http://127.0.0.1:8787/`

More details:
- `docs/SHOWING_THE_DASHBOARD.md`
- `docs/operations/2026-04-24-eeebot-ops-dashboard-baseline.md`

Canonical runtime assets included:
- `scripts/run_web.sh`
- `scripts/run_collector.sh`
- `scripts/install_user_units.sh`
- `scripts/eeepc_reachability_watchdog.py`
- `systemd/nanobot-ops-dashboard-web.service`
- `systemd/nanobot-ops-dashboard-collector.service`

Project links:
- Main repo: `https://github.com/ozand/eeebot`
- Dashboard repo: `https://github.com/ozand/eeebot-ops-dashboard`
