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
- `state/` directory — not tracked in git, changes there cannot be committed

## Current problem (2026-06-11)
The coordinator produces metadata-only artifacts every cycle. Subagents verify them,
write one line to HISTORY.md, commit, and push. Reward stays at 1.2.
**The loop is stuck.** Real improvements must be written to this repo.

## Concrete backlog — pick one each session

### Priority 1: Enumerate real host capabilities [Done]
All hardware enumerated: CPU=Intel N270 1.60GHz, RAM=2GB, disk=233G SSD, kernel=6.1.0-49-686-pae, wifi, bluetooth, screen=1024x600. Data live in state/host_capabilities.json (populated by scripts/eeebot_dashboard.py --refresh-host-caps).

### Priority 2: Real eeebot dashboard (not placeholder) [Done]
scripts/eeebot_dashboard.py is a 2718-line real dashboard: reads state/current_health.json, last 5 reports, subagent results, reward trend, task queue depth. Runs with `python3 scripts/eeebot_dashboard.py`.

### Priority 3: Archive stale subagent requests on schedule [Done]
File: `scripts/archive_subagent_requests.py` dynamically queries state root using coordinator helpers and runs subagent_materializer.archive_stale_requests() with a 4-hour threshold. Automatically executed within the subagent materializer loop and logs to state/current_health.json.

### Priority 4: Improve reward signal [Done]
File: `nanobot/runtime/coordinator.py` checks git status and commit history using _has_concrete_changes(). If no real source files are modified during a materialized candidate run, it penalizes the reward with 0.8 instead of granting the 1.2 bonus. Tested and validated.

### Priority 5: Add a cycle summary [Done] to HISTORY.md automatically
File: `memory/HISTORY.md` in eeebot-self-evolving.
Currently subagents append one-line entries manually. Write a helper function
`append_cycle_summary(repo_root, cycle_id, action, files_changed)` in
`scripts/cycle_logger.py` (new file). It reads HISTORY.md, avoids duplicate
cycle_id entries, appends one line, and saves. Call it from bridge after commit.
Test: `python3 scripts/cycle_logger.py --test` prints "OK, no duplicate".
Commit: `git add scripts/cycle_logger.py memory/HISTORY.md && git commit -m "feat: add cycle_logger.py"`

### Priority 6: Write a smoke-test script [Done] for the self-evolving loop
File: `scripts/smoke_test_loop.py` (new file).
Purpose: quick sanity check that key runtime files exist and are non-empty.
Checks:
  1. state/current_health.json exists and has result_status
  2. state/host_capabilities.json has at least 5 keys
  3. memory/MEMORY.md has at least 10 lines
  4. at least 1 file in state/goals/history/
Output: "PASS: N/N checks" or list of failures.
Test: `python3 scripts/smoke_test_loop.py`
Commit: `git add scripts/smoke_test_loop.py && git commit -m "feat: add smoke_test_loop.py"`


### Priority 7: Write scripts/report_summary.py — cycle stats in 10 lines
File: `scripts/report_summary.py` (new file in eeebot-self-evolving).
Reads last N JSON files from `state/goals/history/` and prints:
  - total cycles, outcome counts (keep/discard), avg reward, avg tool_calls
  - most recent task_id
Output: plain text, ≤10 lines. Default N=20.
Test: `python3 scripts/report_summary.py --state-root /var/lib/eeepc-agent/self-evolving-agent/state`
Commit: `git add scripts/report_summary.py && git commit -m "feat: add report_summary.py"`

### Priority 8: Add stall-detection check to smoke_test_loop.py
File: `scripts/smoke_test_loop.py` (already exists).
Add check #5: state/goals/history/ has at least 1 cycle in the last 2 hours.
If no cycle in 2h → FAIL with "loop stalled: no cycle in last 2h".
Test: `python3 scripts/smoke_test_loop.py` → PASS: 5/5 checks
Commit: `git add scripts/smoke_test_loop.py && git commit -m "feat: smoke_test_loop stall detection"`

## How each session should go
1. `git -c safe.directory=<path> -C <path> pull`
2. Pick ONE task from the backlog above (lowest-numbered undone priority)
3. Read the relevant file, understand the code
4. Write the improvement using write_file or edit_file (only git-tracked files in eeebot-self-evolving!)
5. Test: `exec("python3 <file>")` or `exec("python3 -c 'import <module>'")`
6. Commit: `git add <file> && git commit -m "fix/feat: <what>"`
7. Push: `git push origin main`
8. Append result to memory/HISTORY.md
9. Return JSON: action_taken, files_changed[], outcome, concrete_next_action

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
- Only commit files inside eeebot-self-evolving/ — state/ is not git-tracked
- If pytest unavailable: test with `python3 -c "import <module>; print('ok')"`
- Do not just write HISTORY.md — that is not an improvement
