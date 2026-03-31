# Task System Alignment Note

Last updated: 2026-03-31 UTC

## Problem

A host-side proposal introduced a separate `tasks/todo.md -> tasks/done.md -> tasks/logs/`
tasker workflow for discovery, development, test, and release.

## Why That Is Risky

The project already has canonical task and evidence surfaces:

- product backlog: `todo.md`
- completed archive: `done.md`
- scope/acceptance: `docs/userstory/*`
- self-evolving runtime state: `state/inbox`, `state/reports`, `state/outbox`, `state/goals`
- governance/promotion evidence: validation/reconciliation/promotion records

Adding a second markdown backlog under `tasks/` would create a duplicate source of truth.

## Recommended Single-System Approach

- Keep `todo.md` as the only canonical active backlog.
- Keep `done.md` as the only canonical completion archive.
- Keep `docs/userstory/*` as the only scope/acceptance extension layer.
- Keep host `state/...` as runtime evidence, not as a second product backlog.

## If Tasker Logic Is Still Useful

Treat it as an adapter, not a second storage layer:

- Discovery -> update `todo.md`
- Development -> bounded runtime/workspace actions
- Test -> attach evidence/log references
- Release -> archive in `done.md` or promotion/governance records

It may orchestrate roles, but it must write into the existing canonical system.

## Current Host Observation

The host workspace already contains tasker-style artifacts such as reports, scripts,
and optimization notes, but no separate canonical `tasks/` governance layer should be adopted.
