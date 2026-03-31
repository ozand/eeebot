# Heartbeat Canonical Reporting Note

Last updated: 2026-03-31 UTC

## Problem

A host-side change proposed a separate `generate_summary.py` and a second periodic
reporting loop driven from `HEARTBEAT.md`.

## Why This Must Stay Small

The project already has canonical reporting surfaces:

- self-evolving cycle reports in `state/reports/evolution-*.json`
- human-readable latest report projections in outbox/latest report surfaces
- `todo.md`, `done.md`, and `docs/userstory/*` for task/state intent

Heartbeat should trigger bounded work or notifications, not become a second report system.

## Canonical Rule

- `HEARTBEAT.md` may request a short chat summary,
- but that summary must be a projection of existing canonical runtime evidence,
- and must not create a second durable report store or second task tracker.

## Practical Guidance

- prefer "review latest self-evolving report and send one short summary if new"
- avoid custom report generators that invent new summary files as durable state
- treat host-created helper scripts as provisional workspace artifacts unless promoted into the repo

## Host Fix Applied

- workspace `generate_summary.py` was reduced to a thin projection over canonical
  self-evolving reports
- host ACLs were updated so `opencode` can read the canonical `state/reports` and
  `state/outbox` surfaces needed for summary generation
- summary output now also surfaces approval-gate freshness and an explicit next step

## Remaining Alignment Risk

- If host workspace prompt/instruction artifacts still tell the runtime to report an
  "exact blocker" too broadly, the model may overgeneralize and restate missing
  `todo.md` / `done.md` / `docs/userstory/*` as the blocker even after canonical
  task files are restored.
- The intended blocker categories for heartbeat summaries are runtime-state blockers
  such as approval gate, sidecar state, disabled action, or boundary, not a second
  speculative scan of canonical task files.
- If this wording still appears after canonical task files and `sourceRepoPath` are
  fixed, treat it as host instruction/prompt drift, not as another runtime capability bug.
