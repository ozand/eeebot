# Project Memory

## Identity
- Host: eeepc — Asus Eee PC, i386, Debian, constrained RAM/CPU
- Runtime state: /var/lib/eeepc-agent/self-evolving-agent/state/
- Live release: /opt/eeepc-agent/runtimes/self-evolving-agent/current/
- This file lives in the live release and persists across subagent sessions.

## Two repositories — know the difference

### 1. TARGET_WORKSPACE = agent's working repo
- Path on host: `/home/opencode/servers_team/repo_research/nanobot`
- Remote: `https://github.com/HKUDS/nanobot.git`
- This is what the coordinator/bridge iterates over — config/, prompts/, skills/
- Subagents WRITE HERE and commit to this repo
- safe.directory: `git -c safe.directory=/home/opencode/servers_team/repo_research/nanobot -C /home/.../nanobot ...`

### 2. ozand/eeebot = the runtime engineering repo (developed by operator)
- Path on host: `/home/opencode/servers_team/repo_research/eeebot-canonical`
- Remote: `https://github.com/ozand/eeebot.git`
- Contains: coordinator logic, bridge, health checks, systemd configs
- Operator develops here on a separate dev machine; changes are deployed to eeepc
- Subagents do NOT normally commit here — that is the operator's domain
- Exception: if a subagent identifies a clear bug in coordinator/bridge code, it MAY open a PR

## Mission (two vectors)
1. Self-optimisation — improve the nanobot workspace: better skills, prompts, config, scripts for constrained i386 hardware
2. Owner utility — create tools the operator actually uses: dashboards, generators, capability reports

## How this system works
- Coordinator (app/main.py) runs every 15 min via systemd timer
- It calls execute_turn() → LLM synthesizes/materializes/records a cycle
- Bridge spawns a subagent every 15 min to verify the latest artifact
- Subagent has tools: read_file, write_file, edit_file, list_dir, exec, web_search
- Subagent workspace env var TARGET_WORKSPACE = nanobot repo path above

## Current problem (as of 2026-06-10)
- All 7600+ coordinator cycles produce metadata-only JSON artifacts (no real file changes)
- Subagents correctly identified this 3951+ times but coordinator ignores their output
- Reward stuck at 1.2 — no real progress signal
- Subagents are now running with tools (15 iterations each) but working in playground/ (no remote)

## What a subagent should do
1. Go to TARGET_WORKSPACE = `/home/opencode/servers_team/repo_research/nanobot`
2. `git pull` to get latest state
3. Read the latest artifact from state/improvements/ to understand the proposed change
4. If the artifact is metadata-only: write a real file in nanobot/ instead
   - Good targets: nanobot/skills/, config/, prompts/
5. Test: `python3 -m pytest tests/ -x -q` (skip if tests/ absent)
6. Commit: `git -c safe.directory=... -C ... add <file> && git commit -m "..."`
7. Push: `git -c safe.directory=... -C ... push origin main`
8. Append one line to this file's sibling memory/HISTORY.md

## Key paths
- TARGET_WORKSPACE / agent working repo: `/home/opencode/servers_team/repo_research/nanobot`
- eeebot engineering repo: `/home/opencode/servers_team/repo_research/eeebot-canonical`
- state dir: `/var/lib/eeepc-agent/self-evolving-agent/state/`
  - improvements/ — materialized artifacts
  - reports/ — cycle reports (7685+)
  - subagents/*.json — subagent results
- live release (read-only, no .git): `/opt/eeepc-agent/runtimes/self-evolving-agent/current/`
- playground (local git, NO remote): `/var/lib/eeepc-agent/self-evolving-agent/playground/` — sandbox only

## Rules
- **Write and commit to nanobot repo** (has remote, can push)
- **Do NOT commit eeebot engineering changes** (operator's domain unless explicit bug fix)
- playground has no remote — commits there are lost; do not rely on it
- Never commit secrets or tokens
- Keep changes small and reversible
- safe.directory workaround required: `git -c safe.directory=/path -C /path <cmd>`
- Do not rm -rf, do not touch systemd units without operator approval
