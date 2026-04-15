# Showing and Running the Nanobot Ops Dashboard

## Project Location

- `/home/ozand/herkoot/Projects/nanobot-ops-dashboard`

## What It Does

The dashboard runs on this host and combines:
- live eeepc self-evolving state over SSH
- local repo-side Nanobot state when available
- historical snapshots stored in SQLite

It is designed so hourly or later state changes can be seen as past snapshots instead of only the latest view.

## Important Current Limitation

Durable subagent telemetry is not currently emitted by Nanobot, so the `Subagents` page is implemented but will clearly show that no durable telemetry exists yet.

## One-Time Setup

From the project directory:

```bash
cd /home/ozand/herkoot/Projects/nanobot-ops-dashboard
PYTHONPATH=src python3 -m nanobot_ops_dashboard init-db
```

Database path:
- `data/dashboard.sqlite3`

## Collect One Snapshot

```bash
cd /home/ozand/herkoot/Projects/nanobot-ops-dashboard
PYTHONPATH=src NANOBOT_EEEPC_SUDO_PASSWORD='<set-in-shell-or-env-file>' python3 -m nanobot_ops_dashboard collect-once
```

This stores one repo snapshot and one eeepc snapshot.

## Keep History Accumulating

Use the poll loop so new hourly changes continue entering SQLite.

Example with shorter interval for demo:

```bash
cd /home/ozand/herkoot/Projects/nanobot-ops-dashboard
PYTHONPATH=src NANOBOT_EEEPC_SUDO_PASSWORD='<set-in-shell-or-env-file>' NANOBOT_DASHBOARD_POLL_INTERVAL=60 python3 -m nanobot_ops_dashboard poll
```

For testing, you can limit iterations:

```bash
cd /home/ozand/herkoot/Projects/nanobot-ops-dashboard
PYTHONPATH=src NANOBOT_EEEPC_SUDO_PASSWORD='<set-in-shell-or-env-file>' NANOBOT_DASHBOARD_POLL_INTERVAL=1 python3 -m nanobot_ops_dashboard poll --iterations 3
```

## Canonical Long-Running Mode

The project now includes:
- `scripts/run_web.sh`
- `scripts/run_collector.sh`
- `scripts/install_user_units.sh`
- `systemd/nanobot-ops-dashboard-web.service`
- `systemd/nanobot-ops-dashboard-collector.service`

Recommended steady-state setup on this host:
1. create `~/.config/nanobot-ops-dashboard.env`
2. put in at minimum:
   - `NANOBOT_EEEPC_SUDO_PASSWORD=...`
   - optional `NANOBOT_DASHBOARD_POLL_INTERVAL=300`
3. run:

```bash
cd /home/ozand/herkoot/Projects/nanobot-ops-dashboard
./scripts/install_user_units.sh
systemctl --user enable --now nanobot-ops-dashboard-web.service
systemctl --user enable --now nanobot-ops-dashboard-collector.service
```

## Run the Web Dashboard

```bash
cd /home/ozand/herkoot/Projects/nanobot-ops-dashboard
PYTHONPATH=src NANOBOT_EEEPC_SUDO_PASSWORD='<set-in-shell-or-env-file>' python3 -m nanobot_ops_dashboard serve --host 127.0.0.1 --port 8787
```

Open:
- `http://127.0.0.1:8787/`

## Pages

- `/` — overview
- `/cycles` — current and historical cycle events
- `/promotions` — repo-side promotion event history
- `/approvals` — latest approval/gate snapshots
- `/deployments` — deployment and verification-related proof fields
- `/subagents` — placeholder page with explicit no-telemetry message
- `/collect` — trigger one immediate collection

## What To Show In A Demo

1. Open `/`
   - show current eeepc status
   - show active goal
   - show repo-side promotion summary if present

2. Open `/cycles`
   - show historical collected cycle events
   - explain that new snapshots accumulate in SQLite over time

3. Open `/promotions`
   - show repo-side promotion lifecycle visibility

4. Open `/subagents`
   - explain that the page is ready, but Nanobot does not yet emit durable subagent telemetry

## Test Status

Run all tests:

```bash
cd /home/ozand/herkoot/Projects/nanobot-ops-dashboard
python3 -m pytest -v
```

## Notes

- eeepc collection currently needs sudo because the live authority root is protected.
- the sudo password is passed via `NANOBOT_EEEPC_SUDO_PASSWORD` for local collection.
- repo-side workspace state may be absent on this host; the dashboard handles that gracefully and still shows repo metadata plus live eeepc truth.
