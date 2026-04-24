# eeebot ops dashboard operational baseline — 2026-04-24

Canonical repository note:
- durable source: `ozand/eeebot` under `ops/dashboard/`
- sibling repo `ozand/eeebot-ops-dashboard` is staging/mirror/legacy and is not the durable source of truth
Created at: `2026-04-24T00:45:45Z` UTC / `2026-04-24 03:45 MSK`.

## Scope

This baseline records the current verified state of the local eeebot ops dashboard after the dashboard became the primary observability surface for eeebot/nanobot on eeepc.

Local project:
- `/home/ozand/herkoot/Projects/nanobot/ops/dashboard`

Canonical GitHub repo:
- `ozand/eeebot` (`ops/dashboard/`)

Staging/mirror GitHub repo:
- `ozand/eeebot-ops-dashboard`

Observed eeebot repo:
- `/home/ozand/herkoot/Projects/nanobot`
- GitHub repo: `ozand/eeebot`

## Services

Canonical local user services:

```bash
systemctl --user status nanobot-ops-dashboard-web.service
systemctl --user status nanobot-ops-dashboard-collector.service
```

Expected steady state:
- web service: active
- collector service: active
- dashboard HTTP endpoint responds on port `8787`

The collector service is intentionally bounded with:
- `MemoryMax=512M`
- `RuntimeMaxSec=12h`

This protects the long-running poll loop from unbounded memory growth while preserving automatic restart behavior.

## Dashboard database state

Runtime SQLite database:
- `/home/ozand/herkoot/Projects/nanobot/ops/dashboard/data/dashboard.sqlite3`

Counts observed during this baseline:
- collections: `5808`
- events: `1869`
- cycle PASS events: `802`
- cycle BLOCK events: `38`

## Latest runtime truth

Latest eeepc snapshot:
- collected_at: `2026-04-24T00:43:06.231059Z`
- status: `PASS`
- active_goal: `goal-bootstrap`
- report: `/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260424T003516Z-cycle-b18c021eccba.json`

Latest repo-side snapshot:
- collected_at: `2026-04-24T00:43:06.220266Z`
- status: `PASS`
- active_goal: `goal-bootstrap`
- gate_state: `fresh`
- report: `/home/ozand/herkoot/Projects/nanobot/workspace/state/reports/evolution-20260424T003920Z-cycle-daf36d664619.json`

Interpretation:
- current eeepc runtime is healthy
- repo-side runtime is also healthy
- the previous `no_concrete_change` blocker is not the current live state

## Control artifact policy

`control/` is mixed by design:
- append-only timestamped evidence artifacts can be durable product/evidence state
- scripts that produce or reconcile control artifacts are product code
- mutable latest pointers and live probes are runtime state and are ignored

Ignored runtime pointers:
- `control/status_feed.jsonl`
- `control/eeepc_reachability.json`
- `control/no_live_executor_incident.json`

Tracked control baselines:
- `control/active_projects.json`
- `control/active_execution.json`
- `control/execution_queue.json`

Tracked durable evidence examples:
- `control/no_live_executor_incidents/*.json`
- `control/eeepc_preactivation_verification.json`

## Verification commands

```bash
cd /home/ozand/herkoot/Projects/nanobot/ops/dashboard
python3 -m pytest -v
systemctl --user restart nanobot-ops-dashboard-collector.service nanobot-ops-dashboard-web.service
systemctl --user is-active nanobot-ops-dashboard-web.service nanobot-ops-dashboard-collector.service
python3 - <<'PY'
import urllib.request
for path in ['/', '/analytics', '/system', '/cycles?source=eeepc']:
    r = urllib.request.urlopen('http://127.0.0.1:8787' + path, timeout=10)
    print(path, r.status)
PY
```

## Known limitations / follow-up

1. Durable subagent telemetry depends on eeebot/nanobot emitting durable subagent records.
2. Runtime SQLite files remain local and intentionally ignored.
3. eeepc live authority reads depend on SSH plus local env secret for sudo where required.
4. Public identity is eeebot, while some internal compatibility paths still use nanobot names.
