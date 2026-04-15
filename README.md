# Nanobot Ops Dashboard

Local observability dashboard for Nanobot.

Purpose:
- run on this host, not on eeepc
- ingest live eeepc self-evolving state over SSH
- ingest local repo-side Nanobot bounded-runtime state
- keep historical snapshots in SQLite
- present a local web UI for current and past cycles, goals, promotions, approvals, deployments, and any available subagent telemetry

Current scope of v1:
- overview page with latest-source summaries, blocker analysis, and quick links
- cycle history page with visible filter form
- promotions page with visible filter form
- approvals/capability page
- deployments/verification page
- analytics page with recent snapshots, recent cycles, failure-class breakdown, streaks, top-goal frequency, top BLOCK reasons, and artifact history
- subagents page with graceful "no durable telemetry yet" handling
- `/api/summary` machine-readable endpoint
- `/api/cycles` machine-readable history endpoint
- `/api/promotions` machine-readable history endpoint
- `/api/approvals` machine-readable operational endpoint
- `/api/deployments` machine-readable deployment/proof endpoint
- `/api/analytics` machine-readable analytics endpoint

The dashboard is intentionally dependency-light:
- Python stdlib
- SQLite
- Jinja2
- system ssh/scp

Quick start:

```bash
cd /home/ozand/herkoot/Projects/nanobot-ops-dashboard
PYTHONPATH=src python3 -m nanobot_ops_dashboard init-db
PYTHONPATH=src NANOBOT_EEEPC_SUDO_PASSWORD='<set-in-shell-or-env-file>' python3 -m nanobot_ops_dashboard collect-once
PYTHONPATH=src NANOBOT_EEEPC_SUDO_PASSWORD='<set-in-shell-or-env-file>' python3 -m nanobot_ops_dashboard serve --host 127.0.0.1 --port 8787
```

Then open:
- `http://127.0.0.1:8787/`

More details:
- `docs/SHOWING_THE_DASHBOARD.md`

Canonical runtime assets included:
- `scripts/run_web.sh`
- `scripts/run_collector.sh`
- `scripts/install_user_units.sh`
- `systemd/nanobot-ops-dashboard-web.service`
- `systemd/nanobot-ops-dashboard-collector.service`
