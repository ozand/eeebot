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
- state/improvements/ — materialized artifacts (currently all metadata)
- state/reports/ — cycle reports (7600+)
- state/subagents/*.json — subagent results (concrete_next_action field)
- nanobot/runtime/ — coordinator, health, schemas, state logic
- nanobot/skills/ — skills dir (can be extended by subagent)
- app/main.py — execute_turn, _call_llm (LLM wired since 2026-06-10)

## Rules
- Never commit secrets or tokens
- Keep changes small and reversible
- Run tests before committing: python3 -m pytest tests/ -x -q
- Git workflow: branch from main, commit, push, open PR (or push main for hotfixes)
- Do not rm -rf, do not touch systemd units without operator approval
