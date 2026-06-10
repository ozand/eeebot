# Project Memory

## Identity
- Project: eeebot (canonical repo: ozand/eeebot)
- Host: eeepc — Asus Eee PC, i386, Debian, constrained RAM/CPU
- Runtime state: /var/lib/eeepc-agent/self-evolving-agent/state/
- Live release: /opt/eeepc-agent/runtimes/self-evolving-agent/current/
- This file lives in the workspace and persists across subagent sessions.

## Mission (two vectors)
1. Self-optimisation on constrained hardware — CPU/RAM/disk efficiency, camera/BT/WiFi/mic capability enumeration
2. Owner utility and creative output — TUI dashboards, generators, tools the operator actually uses

## How this system works
- Coordinator (app/main.py) runs every 15 min via systemd timer
- It calls execute_turn() → LLM synthesizes/materializes/records a cycle
- Bridge (eeepc-self-evolving-subagent-bridge.service) spawns a subagent every 15 min to verify the latest artifact
- Subagent has: read_file, write_file, edit_file, list_dir, exec, web_search, web_fetch
- Subagent workspace = /opt/eeepc-agent/runtimes/self-evolving-agent/current/
- Skills available: github, skill-creator, memory, cron (see nanobot/skills/)

## Current problem (as of 2026-06-10)
- All 7600+ coordinator cycles produce metadata-only JSON artifacts (no real file changes)
- Subagents have correctly identified this 3951 times but coordinator ignores their output
- Reward stuck at 1.2 — no real progress signal
- Root cause: coordinator does not read concrete_next_action from subagent results

## What a subagent should do
- Read the latest artifact from state/improvements/ or state/reports/
- If it is metadata-only: DO NOT just say so — write a real file instead
- Prefer: creating a new script, editing nanobot/runtime/*.py, writing a test
- Use exec to run: python3 -m pytest tests/ -x -q (verify before committing)
- Use github skill to commit and push real changes to ozand/eeebot
- Update this MEMORY.md with what was accomplished

## Key paths
- **Canonical git repo on host**: `/home/opencode/servers_team/repo_research/eeebot-canonical`
  - remote: `https://github.com/ozand/eeebot.git`
  - use: `git -c safe.directory=<path> -C <path> ...` (safe.directory required)
  - always `git pull` before making changes
- state dir: `/var/lib/eeepc-agent/self-evolving-agent/state/`
  - improvements/ — materialized artifacts
  - reports/ — cycle reports
  - subagents/*.json — subagent results
- live release: `/opt/eeepc-agent/runtimes/self-evolving-agent/current/` (no .git here — read-only copy)
- playground: `/var/lib/eeepc-agent/self-evolving-agent/playground/` — local sandbox, NO remote push possible
- nanobot/runtime/ — coordinator, health, schemas, state logic
- nanobot/skills/ — skills dir (CAN be extended/improved by subagent)
- app/main.py — execute_turn, _call_llm (LLM wired since 2026-06-10)
- memory/HISTORY.md — THIS file's sibling, append one line per session

## Rules
- **Always commit to eeebot-canonical, never only to playground** (playground has no remote)
- Never commit secrets or tokens
- Keep changes small and reversible
- Run tests before committing: `python3 -m pytest tests/ -x -q` (skip gracefully if pytest absent)
- Git workflow: pull → edit → commit → push to `origin main` (hotfixes direct; features → branch + PR)
- Do not rm -rf, do not touch systemd units without operator approval
- safe.directory workaround: `git -c safe.directory=/path -C /path status`
