# Repo Status Boundary Note

Last updated: 2026-03-30 UTC

## Problem

The running bot workspace on the host is:

- `/home/opencode/.nanobot-eeepc/workspace`

The actual source repository lives elsewhere:

- `/home/opencode/servers_team/repo_research/nanobot`

This means a truthful `git status` request cannot be solved by a simple read-only
`exec` call in the current workspace, because:

- `exec` runs in the bot workspace by default,
- `restrictToWorkspace=true` is enabled,
- the source repo is outside the bot workspace boundary.

## Why This Matters

- `pwd` and similar safe diagnostics now work through curated `exec`.
- `git status` is still not reliably meaningful, not because `exec` is absent, but
  because the current working boundary is not the source repo.

## Smallest Safe Next Step

Prefer a bounded, read-only repo-status path rather than widening generic shell.

Chosen direction:

1. `workspace.repo_status`
2. `/workspace repo-status`

Verification status:

- host simulator confirms `/workspace repo-status` returns truthful git status for
  `/home/opencode/servers_team/repo_research/nanobot` while `restrictToWorkspace`
  remains enabled for the main bot workspace.

Do not disable `restrictToWorkspace` globally just to make `git status` work.

## References

- `docs/BOT_TERMINAL_COMMAND_CATALOG.md`
- `docs/WORKSPACE_RUNTIME_LANE.md`
- `.env/eeepc_host_access.md`
