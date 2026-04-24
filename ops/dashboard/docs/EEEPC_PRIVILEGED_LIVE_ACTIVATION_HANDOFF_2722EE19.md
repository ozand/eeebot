# eeepc Privileged Live Activation Handoff: 2722ee19

Last updated: 2026-04-21 UTC

This is the operator-safe handoff for activating the recorded next-cycle-selection fix on live eeepc.

Exact commit:
- `2722ee19c27025c02b35ca4e8fd425de0fb82d61` — `Use recorded task plans for next cycle selection`

Archive artifact:
- path: `/tmp/nanobot-2722ee19.tar.gz`
- sha256: `4df594cac11bac69fb782881676d05cb1f67449e24c4d1e072083a848e3b76f0`
- build command: `git -C /home/ozand/herkoot/Projects/nanobot archive --format=tar 2722ee19 | gzip -n > /tmp/nanobot-2722ee19.tar.gz`

Target runtime paths under /opt:
- runtime base: `/opt/eeepc-agent/runtimes/self-evolving-agent`
- release dir: `/opt/eeepc-agent/runtimes/self-evolving-agent/releases/2722ee19`
- active symlink: `/opt/eeepc-agent/runtimes/self-evolving-agent/current`

Privileged update commands:
```bash
sudo install -d -m 0755 /opt/eeepc-agent/runtimes/self-evolving-agent/releases/2722ee19
printf '4df594cac11bac69fb782881676d05cb1f67449e24c4d1e072083a848e3b76f0  /tmp/nanobot-2722ee19.tar.gz\n' | sha256sum -c -
sudo tar -xzf /tmp/nanobot-2722ee19.tar.gz -C /opt/eeepc-agent/runtimes/self-evolving-agent/releases/2722ee19
sudo ln -sfn /opt/eeepc-agent/runtimes/self-evolving-agent/releases/2722ee19 /opt/eeepc-agent/runtimes/self-evolving-agent/current
sudo systemctl restart eeepc-self-evolving-agent-health.service
```

Verification commands:
```bash
sudo readlink -f /opt/eeepc-agent/runtimes/self-evolving-agent/current
sudo systemctl is-active eeepc-self-evolving-agent-health.service
sudo journalctl -u eeepc-self-evolving-agent-health.service -n 50 --no-pager
sudo python3 - <<'PY'
from pathlib import Path
import json
root = Path('/opt/eeepc-agent/runtimes/self-evolving-agent/current')
reports = sorted(root.glob('reports/evolution-*.json'), key=lambda p: p.stat().st_mtime)
if not reports:
    raise SystemExit('no report files found')
report = reports[-1]
data = json.loads(report.read_text(encoding='utf-8'))
print(f'report={report}')
print(f"selected_tasks={data.get('selected_tasks')}")
print(f"task_selection_source={data.get('task_selection_source')}")
PY
```

Operator notes:
- Do not edit the live tree in place; unpack side-by-side, then switch `current`.
- If the hash check fails, stop before extract.
- The next report should show `selected_tasks` and `task_selection_source` from the new cycle selection path.
