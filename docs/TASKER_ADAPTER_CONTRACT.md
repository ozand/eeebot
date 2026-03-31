# Tasker Adapter Contract

Last updated: 2026-03-31 UTC

## Purpose

This contract allows discovery/development/test/release style orchestration without
creating a second task system.

## Canonical Surfaces

- `todo.md` = only active backlog
- `docs/userstory/*` = scope, intent, acceptance
- `done.md` = only completion archive
- `state/...` = runtime evidence and execution artifacts, not product backlog

## Minimal Contract

Tasker logic may orchestrate stages, but it must write into the existing canonical system.

Stage mapping:

- `discovery` -> create/link a user story and update `todo.md`
- `development` -> bounded implementation in allowed runtime/workspace surfaces
- `test` -> attach focused evidence refs and validation results
- `release` -> archive the result in `done.md` and/or promotion/governance records

## Required Rules

1. No `tasks/todo.md` or `tasks/done.md` as a second source of truth.
2. No second markdown log system parallel to `state/...` evidence.
3. One active owner per work item.
4. Shared truth lives in canonical files, not only in chat.
5. If a stage cannot be verified, mark it `blocked`, `degraded`, or `unverified`.

## If Role-Based Subagents Are Used

They may behave like:

- discovery
- development
- test
- release

but they still write into the same canonical surfaces above.
