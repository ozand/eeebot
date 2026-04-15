# Nanobot Ops Dashboard

Local observability dashboard for Nanobot.

Purpose:
- run on this host, not on eeepc
- ingest live eeepc self-evolving state over SSH
- ingest local repo-side Nanobot bounded-runtime state
- keep historical snapshots in SQLite
- present a local web UI for current and past cycles, goals, promotions, approvals, deployments, and any available subagent telemetry

Current scope of v1:
- overview page
- cycle history page
- promotions page
- approvals/capability page
- deployments/verification page
- subagents page with graceful "no durable telemetry yet" handling

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
