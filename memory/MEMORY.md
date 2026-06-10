# Project Memory

## Identity
- Host: eeepc — Asus Eee PC, i386, Debian, constrained RAM/CPU
- Runtime state: /var/lib/eeepc-agent/self-evolving-agent/state/
- Live release: /opt/eeepc-agent/runtimes/self-evolving-agent/current/
- This file lives in the live release and persists across subagent sessions.

## Two repositories — know the difference

### ozand/eeebot — the engineering repo (OPERATOR ONLY)
- Path on host: `/home/opencode/servers_team/repo_research/eeebot-canonical`
- Remote: `https://github.com/ozand/eeebot.git`
- Contains: coordinator, bridge, health, systemd configs, this MEMORY.md
- **Subagents do NOT commit here** — that is the operator's domain
- Operator develops on a separate machine and deploys here

### HKUDS/nanobot — legacy dirty checkout (DO NOT USE)
- Path on host: `/home/opencode/servers_team/repo_research/nanobot`
- Has a `DO_NOT_USE_LEGACY_CHECKOUT.md` file explicitly forbidding use
- **Do not write files here, do not commit here**

### playground — local sandbox (no remote)
- Path: `/var/lib/eeepc-agent/self-evolving-agent/playground`
- Has .git but no remote — commits stay local only
- OK for experiments and drafts, but nothing persists beyond the host

## Where subagents SHOULD write
The playground is the correct place for subagent file work right now.
Commits in playground are local but survive reboots and are visible to future subagents.

**Preferred workflow for subagents:**
1. Read the latest artifact from state/improvements/ or state/reports/
2. If metadata-only: write a real improvement to playground/
3. Use tools: write_file, edit_file, exec
4. Commit to playground: `git -c safe.directory=<path> -C <path> add . && git commit -m "..."`
5. Append one line to playground/memory/HISTORY.md
6. Return structured summary with: action_taken, files_changed[], concrete_next_action

## Mission (two vectors)
1. Self-optimisation — improve runtime config/prompts/skills for constrained i386 hardware
2. Owner utility — create tools the operator actually uses: dashboards, capability reports

## How this system works
- Coordinator (app/main.py) runs every 15 min via systemd timer (health.service)
- Bridge spawns a subagent every 15 min to verify/act on the latest artifact
- Subagent has tools: read_file, write_file, edit_file, list_dir, exec, web_search
- TARGET_WORKSPACE env = /home/opencode/servers_team/repo_research/nanobot (legacy, ignore)
- Actual useful workspace for subagents = playground/

## Current problem (as of 2026-06-10)
- All 7700+ coordinator cycles produce metadata-only JSON artifacts (no real file changes)
- Reward stuck at 1.2
- Subagents now run with tools (15 iterations each) but confused about where to write
- FS was read-only for 7 days due to ext4 journal corruption — fixed by reboot + fsck

## Key paths
- state/improvements/ — materialized artifacts (all metadata so far)
- state/reports/ — cycle reports (7700+)
- state/subagents/*.json — subagent results
- live release (read-only): `/opt/eeepc-agent/runtimes/self-evolving-agent/current/`
- playground (writable, local git): `/var/lib/eeepc-agent/self-evolving-agent/playground/`

## Rules
- Do not write to nanobot/ legacy checkout
- Do not commit to eeebot-canonical/ (operator only)
- playground is the correct write target for subagents
- safe.directory required: `git -c safe.directory=/path -C /path <cmd>`
- Never commit secrets or tokens
- Do not rm -rf, do not touch systemd units
