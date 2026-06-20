# Project Memory

## Identity
- Host: eeepc — Asus Eee PC 1015PEM, i386, Debian, 1GB RAM, constrained CPU
- Runtime state: /var/lib/eeepc-agent/self-evolving-agent/state/
- Live release: /opt/eeepc-agent/runtimes/self-evolving-agent/current/
- This file lives in eeebot-self-evolving/memory/ and in the live release.

## Write target: ozand/eeebot-self-evolving
- Path: `/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving`
- Remote: `https://github.com/ozand/eeebot-self-evolving.git`
- git identity: user.name=eeepc-agent, user.email=eeepc-agent@eeebot
- Always `git pull` before working, always `git push origin main` after commit
- safe.directory: `git -c safe.directory=/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving -C /var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving ...`

## DO NOT touch
- `/opt/eeepc-agent/hermes-agent` — legacy, has DO_NOT_USE_LEGACY_CHECKOUT.md
- `/opt/eeepc-agent/runtimes/self-evolving-agent/current` — live release (canonical eeebot code)

## Current problem (2026-06-11)
The coordinator produces metadata-only artifacts every cycle. Subagents verify them,
write one line to HISTORY.md, commit, and push. Reward stays at 1.2.
**The loop is stuck.** Real improvements must be written to this repo.

## Durable artifact improvement (2026-06-21)
Materialized improvement artifacts now include an explicit `recommended_next_action` field in `nanobot/runtime/coordinator.py`, giving follow-up verification lanes a concrete next step instead of only metadata.

## Concrete backlog — pick one each session

### Priority 1: Enumerate real host capabilities
File: `state/host_capabilities.json` — already has camera/bt/wifi/mic.
Next: add CPU info, RAM, disk, uptime, kernel version.
Command: `cat /proc/cpuinfo | grep 'model name' | head -1`
         `free -m`, `df -h /`, `uname -r`, `uptime`
Write results back to state/host_capabilities.json.

### Priority 2: Real eeebot dashboard (not placeholder)
File: `scripts/eeebot_dashboard.py`
Currently is a placeholder. Build a real CLI dashboard that reads:
- `/var/lib/eeepc-agent/self-evolving-agent/state/current_health.json`
- last 5 reports from `state/reports/`
- last 3 subagent results from `state/subagents/`
- reward trend, current task, queue depth
Output: plain text, 20 lines, no TUI needed. Run with `python3 scripts/eeebot_dashboard.py`.

### Priority 3: Archive stale subagent requests on schedule [Done]
File: `scripts/archive_subagent_requests.py` dynamically queries state root using coordinator helpers and runs subagent_materializer.archive_stale_requests() with a 4-hour threshold. Automatically executed within the subagent materializer loop and logs to state/current_health.json.

### Priority 4: Improve reward signal [Done]
File: `nanobot/runtime/coordinator.py` checks git status and commit history using _has_concrete_changes(). If no real source files are modified during a materialized candidate run, it penalizes the reward with 0.8 instead of granting the 1.2 bonus. Tested and validated.

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
- This repo: `/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving/`

## Rules
- safe.directory workaround always required for git
- Never commit secrets or tokens
- Do not rm -rf, do not touch systemd units
- If pytest unavailable: test with `python3 -c "import <module>; print('ok')"`
- Do not just write HISTORY.md — that is not an improvement
