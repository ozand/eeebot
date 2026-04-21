# eeepc Privileged Subagent Telemetry Handoff: f14d33c

Last updated: 2026-04-21 UTC

This is the operator-safe, minimal handoff bundle for the remaining eeepc blocker: the live subagent bridge/runtime path under /home/opencode still needs the canonical state-root fix that makes durable telemetry appear under the host control-plane tree.

Do not treat this as the switch itself. It is the exact deploy/trigger/verify bundle for a privileged operator.

## Exact active bridge/runtime path(s) under /home/opencode

- active Nanobot checkout: `/home/opencode/servers_team/repo_research/nanobot`
- active bridge telemetry sink before the fix: `/home/opencode/servers_team/repo_research/nanobot/.nanobot/subagents`
- canonical host telemetry root expected after the fix: `/var/lib/eeepc-agent/self-evolving-agent/state/subagents`

## Exact commit to deliver

- `f14d33ccdd7610877af9f5e3a43f6c7934f06e4b` — `Fix eeepc subagent state-root resolution`

This is the latest commit containing the fix.

## Exact archive or files to overlay/copy

Preferred overlay artifact:
- archive: `/tmp/nanobot-f14d33c.tar.gz`
- sha256: `64872f68073dd11077d501f06942f6aee3f44dafd0acf9e3195910c2ce6c3ce1`
- build command:
  `git -C /home/ozand/herkoot/Projects/nanobot archive --format=tar f14d33c | gzip -n > /tmp/nanobot-f14d33c.tar.gz`

Minimal live-critical file if a surgical copy is required instead of the archive:
- `nanobot/runtime/state.py`

Verification-only companion file from the same commit:
- `tests/test_runtime_coordinator.py`

## Exact privileged trigger

Restart the active eeepc subagent bridge units:

```bash
sudo systemctl restart eeepc-self-evolving-subagent-bridge.service
sudo systemctl restart eeepc-self-evolving-subagent-bridge.timer
```

If the host uses a one-shot timer trigger only, restart the timer first and let it invoke the service once. If the service is already active, restart it after the overlay so it reloads the updated checkout.

## Exact verification commands

Run these after the overlay and trigger:

```bash
sudo git -C /home/opencode/servers_team/repo_research/nanobot rev-parse --short HEAD
sudo systemctl is-active eeepc-self-evolving-subagent-bridge.service
sudo systemctl is-active eeepc-self-evolving-subagent-bridge.timer
sudo find /var/lib/eeepc-agent/self-evolving-agent/state/subagents -maxdepth 1 -type f -name '*.json' -print | sort
sudo python3 - <<'PY'
from pathlib import Path
root = Path('/var/lib/eeepc-agent/self-evolving-agent/state/subagents')
files = sorted(root.glob('*.json'), key=lambda p: p.stat().st_mtime if p.exists() else 0)
if not files:
    raise SystemExit(f'no telemetry files found in {root}')
print(files[-1])
PY
```

## What success looks like

- the active checkout reports `f14d33c` or later containing it
- the bridge/timer unit is active after reload
- at least one file exists at `/var/lib/eeepc-agent/self-evolving-agent/state/subagents/*.json`
- the latest file is readable as durable subagent telemetry from the canonical host state root

## Operator rule

Do not widen scope. If the overlay, restart, or verification fails, stop and record the exact failure instead of improvising another repair.
