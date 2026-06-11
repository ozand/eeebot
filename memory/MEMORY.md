# Project Memory

## Identity
- Host: eeepc — Asus Eee PC 1015PEM, i386, Debian, 1GB RAM, constrained CPU
- Runtime state: /var/lib/eeepc-agent/self-evolving-agent/state/
- Live release: /opt/eeepc-agent/runtimes/self-evolving-agent/current/
- This file lives in eeebot-self-evolving/memory/ and in the live release.

## Write target: ozand/eeebot-self-evolving
- Path: `/home/opencode/servers_team/repo_research/eeebot-self-evolving`
- Remote: `https://github.com/ozand/eeebot-self-evolving.git`
- git identity: user.name=eeepc-agent, user.email=eeepc-agent@eeebot
- Always `git pull` before working, always `git push origin main` after commit
- safe.directory: `git -c safe.directory=/home/opencode/.../eeebot-self-evolving -C /home/.../eeebot-self-evolving ...`

## DO NOT touch
- `/home/opencode/servers_team/repo_research/nanobot` — legacy, has DO_NOT_USE_LEGACY_CHECKOUT.md
- `/home/opencode/servers_team/repo_research/eeebot-canonical` — operator only

## Current problem (2026-06-11)
The coordinator produces metadata-only artifacts every cycle. Subagents verify them,
write one line to HISTORY.md, commit, and push. Reward stays at 1.2.
**The loop is stuck.** Real improvements must be written to this repo.

## Concrete backlog — pick one each session

### Priority 1: Fix the artifact cycle (highest value)
The coordinator's materialized artifacts have no `concrete_change` field and no
`recommended_next_action`. Subagents receive empty `task` fields.
File: `nanobot/runtime/coordinator.py` function `_write_subagent_request_artifact()`
Fix: add a `"task"` field built from `concrete_improvement_statement` + hadi_cycle action.
This is the root cause of the stagnation loop.

### Priority 2: Enumerate real host capabilities
File: `state/host_capabilities.json` — already has camera/bt/wifi/mic.
Next: add CPU info, RAM, disk, uptime, kernel version.
Command: `cat /proc/cpuinfo | grep 'model name' | head -1`
         `free -m`, `df -h /`, `uname -r`, `uptime`
Write results back to state/host_capabilities.json.

### Priority 3: Real eeebot dashboard (not placeholder)
File: `scripts/eeebot_dashboard.py`
Currently is a placeholder. Build a real CLI dashboard that reads:
- `/var/lib/eeepc-agent/self-evolving-agent/state/current_health.json`
- last 5 reports from `state/reports/`
- last 3 subagent results from `state/subagents/`
- reward trend, current task, queue depth
Output: plain text, 20 lines, no TUI needed. Run with `python3 scripts/eeebot_dashboard.py`.

### Priority 4: Archive stale subagent requests on schedule
File: `scripts/archive_subagent_requests.py` exists but is never called.
Add a cron/systemd script or improve the existing one to:
- Move requests older than 4h from state/subagents/requests/ to state/subagents/archive/
- Log count to state/current_health.json

### Priority 5: Improve reward signal
The coordinator always returns reward=1.2 for metadata-only artifacts.
File: `nanobot/runtime/coordinator.py` — find reward calculation.
If `concrete_change` is absent/empty → reward should be 0.8 (below baseline).
This creates pressure to produce real changes.

## How each session should go
1. `git -c safe.directory=<path> -C <path> pull`
2. Pick ONE task from the backlog above (start with Priority 1 or 3)
3. Read the relevant file, understand the code
4. Write the improvement using write_file or edit_file
5. Test: `exec("python3 <file>")` or `exec("python3 -c 'import <module>'")`
6. Commit: `git add <file> && git commit -m "fix/feat: <what>"`
7. Push: `git push origin main`
8. Append result to memory/HISTORY.md
9. Return: action_taken, files_changed[], concrete_next_action

## Key paths on host
- State: `/var/lib/eeepc-agent/self-evolving-agent/state/`
- Improvements: `state/improvements/materialized-*.json`
- Reports: `state/reports/`
- Subagent results: `state/subagents/*.json`
- Live coordinator: `/opt/eeepc-agent/runtimes/self-evolving-agent/current/nanobot/runtime/coordinator.py`
- This repo: `/home/opencode/servers_team/repo_research/eeebot-self-evolving/`

## Rules
- safe.directory workaround always required for git
- Never commit secrets or tokens
- Do not rm -rf, do not touch systemd units
- If pytest unavailable: test with `python3 -c "import <module>; print('ok')"`
- Do not just write HISTORY.md — that is not an improvement
