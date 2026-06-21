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

## How each session should go
1. `git -c safe.directory=<path> -C <path> pull`
2. Pick ONE task from **Active backlog** below (lowest-numbered undone priority)
3. Read the relevant file, understand the code
4. Write the improvement using write_file or edit_file (only git-tracked files in eeebot-self-evolving!)
5. Test: `exec("python3 <file>")` or `exec("python3 -c 'import <module>'")`
6. Commit: `git add <file> && git commit -m "fix/feat: <what>"`
7. Push: `git push origin main`
8. Append result to memory/HISTORY.md (newest line first)
9. Return JSON: action_taken, files_changed[], outcome, concrete_next_action

## Rules
- safe.directory workaround always required for git
- Never commit secrets or tokens
- Do not rm -rf, do not touch systemd units
- Only commit files inside eeebot-self-evolving/ — state/ is not git-tracked
- If pytest unavailable: test with `python3 -c "import <module>; print('ok')"`
- Do not just write HISTORY.md — that is not an improvement

## Key paths on host
- State: `/var/lib/eeepc-agent/self-evolving-agent/state/`
- Improvements: `state/improvements/materialized-*.json`
- Reports: `state/reports/`
- Subagent results: `state/subagents/*.json`
- Live coordinator: `/opt/eeepc-agent/runtimes/self-evolving-agent/current/nanobot/runtime/coordinator.py`
- This repo: `/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving/`

---

## Active backlog — pick one each session

<!-- BACKLOG_START -->
<!-- When all priorities here are [Done], new ones will be auto-seeded from research/feed.json -->

### Priority 9: Restructure MEMORY.md — active backlog first, Completed section at bottom
File: `memory/MEMORY.md` (this file, in eeebot-self-evolving).
Move all [Done] priority blocks from `## Active backlog` into `## Completed` section at bottom.
Update `scripts/eeepc_self_evolving_subagent_bridge.py` `_try_mark_backlog_done()` to move
completed blocks to `## Completed` instead of marking inline.
Test: `python3 -c "import re; text=open('memory/MEMORY.md').read(); assert '## Completed' in text; print('ok')"`
Commit: `git add memory/MEMORY.md scripts/eeepc_self_evolving_subagent_bridge.py && git commit -m "feat: restructure MEMORY.md active/completed sections"`

### Priority 10: HISTORY.md newest-first — update cycle_logger.py to prepend
File: `scripts/cycle_logger.py` (already exists).
Change `append_cycle_summary()` to **prepend** new entries (newest first) instead of appending.
This ensures subagent `read_file` without offset sees recent context.
Test: `python3 scripts/cycle_logger.py --test` → "OK, no duplicate"
Also verify first line of HISTORY.md is more recent than last line after prepend.
Commit: `git add scripts/cycle_logger.py && git commit -m "feat: cycle_logger prepend newest-first"`

### Priority 11: Auto-seed backlog from research/feed.json when all priorities Done
File: `scripts/eeepc_self_evolving_subagent_bridge.py` — `_try_mark_backlog_done()`.
After marking last priority Done, if `## Active backlog` has no undone Priority:
  1. Read `state/research/feed.json` from STATE_DIR
  2. Pick top 2 candidate titles not already in MEMORY.md
  3. Add as `### Priority N:` blocks with concrete instructions
  4. Commit: `chore: auto-seed Priority N/M from research feed (backlog empty)`
Test: run bridge with all-Done MEMORY.md → new Priority blocks appear.
Commit: `git add scripts/eeepc_self_evolving_subagent_bridge.py && git commit -m "feat: auto-seed backlog from research feed"`

### Priority 12: Structured lesson recording after subagent commit
File: `scripts/eeepc_self_evolving_subagent_bridge.py`.
After `commits_pushed > 0`, write structured lesson entry to `lessons/lessons.yaml`:
  - id: LESS-YYYYMMDD-<short_cycle>
  - task_id, hypothesis (from artifact), result (files + commit hash), generalized_insight
  - Use `_derive_insight(files_changed, tool_calls, elapsed)` helper (rules-based, no LLM)
Test: `python3 -c "import yaml; d=yaml.safe_load(open('lessons/lessons.yaml')); print(len(d['lessons']), 'entries')"`
Commit: `git add lessons/lessons.yaml scripts/eeepc_self_evolving_subagent_bridge.py && git commit -m "feat: structured lesson recording after subagent commit"`

### Priority 13: L0/L1 memory — memory_archiver.py with Gemini summarization
File: `scripts/memory_archiver.py` (new file).
Triggered when MEMORY.md > 50 lines OR last archive > 6 days ago.
  1. Read HISTORY.md entries from last 7 days
  2. Call `cl/gemini-3.5-flash` via LiteLLM (`http://100.82.9.44:4001/v1`) for 3-sentence weekly summary
  3. Fallback (LLM unavailable): deterministic summary (count lines, list task_ids)
  4. Append to `memory/MEMORY_ARCHIVE.md` under `## Week YYYY-WNN`
  5. Truncate HISTORY.md: keep last 14 days, move older to archive
Test: `python3 scripts/memory_archiver.py --repo-root . --dry-run`
Commit: `git add scripts/memory_archiver.py memory/MEMORY_ARCHIVE.md && git commit -m "feat: add memory_archiver.py with L0/L1 split"`

<!-- BACKLOG_END -->

---

## Completed
<!-- Completed priorities are moved here by bridge _try_mark_backlog_done() -->
<!-- Format: ### Priority N: <title> [Done]\n<brief outcome> -->

### Priority 1: Enumerate real host capabilities [Done]
CPU=Intel N270 1.60GHz, RAM=2GB, disk=233G SSD, kernel=6.1.0-49-686-pae. Data in state/host_capabilities.json.

### Priority 2: Real eeebot dashboard (not placeholder) [Done]
scripts/eeebot_dashboard.py — 2718-line dashboard: health, reports, subagent results, reward trend.

### Priority 3: Archive stale subagent requests on schedule [Done]
scripts/archive_subagent_requests.py — 4-hour threshold, integrated into materializer loop.

### Priority 4: Improve reward signal [Done]
_has_concrete_changes() in coordinator.py — penalizes 0.8 when no source files modified.

### Priority 5: Add cycle summary to HISTORY.md automatically [Done]
scripts/cycle_logger.py — append_cycle_summary() with duplicate protection.

### Priority 6: Write smoke-test script for the self-evolving loop [Done]
scripts/smoke_test_loop.py — 5-check runtime sanity (now with stall detection).

### Priority 7: Write scripts/report_summary.py [Done]
scripts/report_summary.py — cycle stats: total, outcomes, avg reward, avg tool_calls.

### Priority 8: Add stall-detection check to smoke_test_loop.py [Done]
check #5: history has ≥1 cycle in last 2h; FAIL = "loop stalled".
