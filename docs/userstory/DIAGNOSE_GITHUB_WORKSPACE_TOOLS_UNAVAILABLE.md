# Userstory: Diagnose GitHub/Workspace Tools Unavailable

## User Story

As an operator reading the bot's replies,
I want the bot to explain precisely why GitHub or workspace status tools are unavailable,
so that I can distinguish real capability gaps from routing, boundary, or truth-surface drift.

## Scope

This story covers diagnosis and truth parity for claims like "I cannot access GitHub" or
"I cannot use workspace status", especially when bounded runtime paths such as
`workspace.repo_status` or curated `exec` are already present.

## Acceptance Criteria

- The bot no longer gives a generic "no access" answer when a bounded path exists.
- Failure responses identify a concrete reason such as config, boundary, disabled action, or auth gap.
- GitHub access and local repo/workspace access are treated as separate capabilities.
- The explanation matches the live capability snapshot and runtime config.

## Current V1 Slice

- natural-language intents such as `git status`, `repo status`, and `workspace status`
  are routed to the bounded `/workspace repo-status` path instead of falling back to a generic refusal.

## References

- `docs/BOT_TERMINAL_COMMAND_CATALOG.md`
- `docs/REPO_STATUS_BOUNDARY_NOTE.md`
- `todo.md`
