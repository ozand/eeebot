# Project Memory

## Identity
- Host: eeepc — Asus Eee PC, i386, Debian, constrained RAM/CPU
- Runtime state: /var/lib/eeepc-agent/self-evolving-agent/state/
- Live release: /opt/eeepc-agent/runtimes/self-evolving-agent/current/
- This file lives in the live release and persists across subagent sessions.

## Three repositories — know the difference

### 1. ozand/eeebot-self-evolving — SUBAGENT TARGET ✅
- Path on host: `/home/opencode/servers_team/repo_research/eeebot-self-evolving`
- Remote: `https://github.com/ozand/eeebot-self-evolving.git`
- **This is where subagents write and commit.** Purpose: autonomous self-evolution commits.
- Has `ops/dashboard/` — operator control plane files, dashboard app
- Has `ops/dashboard/control/` — JSON control files (active_execution, execution_assignment etc.)
- Historically the agent made "autoevolve: export self-evolving host runtime" commits here
- git identity already set: user.name=eeepc-agent, user.email=eeepc-agent@eeebot
- safe.directory: `git -c safe.directory=/home/opencode/servers_team/repo_research/eeebot-self-evolving -C /home/.../eeebot-self-evolving ...`
- **Always `git pull` before making changes**

### 2. ozand/eeebot — engineering repo (OPERATOR ONLY)
- Path on host: `/home/opencode/servers_team/repo_research/eeebot-canonical`
- Remote: `https://github.com/ozand/eeebot.git`
- Contains: coordinator, bridge, health, systemd configs, this MEMORY.md source
- Subagents do NOT commit here — operator's domain

### 3. HKUDS/nanobot — legacy dirty checkout (DO NOT USE)
- Path: `/home/opencode/servers_team/repo_research/nanobot`
- Has `DO_NOT_USE_LEGACY_CHECKOUT.md` — explicitly forbidden
- Do not write or commit here

### playground — local sandbox (no remote, commits stay local)
- Path: `/var/lib/eeepc-agent/self-evolving-agent/playground`
- OK for drafts but nothing gets pushed to GitHub

## What a subagent should do each session

1. `git -c safe.directory=<path> -C <path> pull` — get latest state of eeebot-self-evolving
2. Read the latest artifact: `state/improvements/` or `state/reports/`
3. If metadata-only (no real file change): **write something real** in eeebot-self-evolving/
   - Good targets: `ops/dashboard/`, a new script, an improvement to `app/` or `nanobot/runtime/`
4. Test if possible: `python3 -m pytest tests/ -x -q` (skip gracefully if not available)
5. Commit: `git -c safe.directory=<path> -C <path> add <file> && git commit -m "autoevolve: <what>"`
6. Push: `git -c safe.directory=<path> -C <path> push origin main`
7. Append one line to this file's sibling `memory/HISTORY.md`

## Mission (two vectors)
1. Self-optimisation — improve runtime: config, prompts, skills, coordinator logic for i386 hardware
2. Owner utility — create tools operator actually uses: dashboards, capability reports, TUI

## How this system works
- Coordinator (app/main.py) runs every 15 min via systemd timer (health.service)
- Bridge spawns a subagent every 15 min to verify/act on latest artifact
- Subagent has tools: read_file, write_file, edit_file, list_dir, exec, web_search
- Subagent gets up to 15 iterations — use them all

## Current state (as of 2026-06-10)
- 7700+ coordinator cycles, reward stuck at 1.2, all artifacts metadata-only
- FS was read-only for 7 days (ext4 journal abort) — fixed by reboot + fsck 2026-06-10
- Subagents now run with tools but were writing to wrong locations
- eeebot-self-evolving repo cloned and ready — subagents should push there

## Key paths
- **SUBAGENT WRITE TARGET**: `/home/opencode/servers_team/repo_research/eeebot-self-evolving`
- state/improvements/ — materialized artifacts
- state/reports/ — cycle reports
- live release (read-only): `/opt/eeepc-agent/runtimes/self-evolving-agent/current/`
- playground (local only): `/var/lib/eeepc-agent/self-evolving-agent/playground/`

## Rules
- **Write and push to eeebot-self-evolving** — it has a remote and is the designated target
- Do not write to nanobot/ (DO_NOT_USE marker)
- Do not commit to eeebot-canonical/ (operator only)
- safe.directory workaround always required: `git -c safe.directory=/path -C /path <cmd>`
- git identity set globally for root: user.name=eeepc-agent, user.email=eeepc-agent@eeebot
- Never commit secrets or tokens
- Do not rm -rf, do not touch systemd units without operator approval
