# Tranche 1 Proof: Richer Host-Control-Plane State Convergence

Last updated: 2026-04-16 UTC

## Goal

Close the remaining mismatch between the live eeepc host control-plane outbox/report shape and the repo-side `runtime/state.py` normalization.

## Missing Fields Closed In This Slice

The host authority root already emits richer proof fields through:
- `outbox/report.index.json`
- the live report referenced by `outbox.source`

This slice adds normalization and operator-facing rendering for:
- `goal.text`
- `follow_through.status`
- `improvement_score`
- `goal_context.subagent_rollup`

## Repo-Side Changes

Updated:
- `nanobot/runtime/state.py`
- `tests/test_commands.py`

## Resulting Status Surface

When reading `host_control_plane`, `nanobot status` can now render:
- `Goal text`
- `Follow-through`
- `Improvement score`
- `Subagents: enabled=..., total=..., done=...`

in addition to the previously normalized fields.

## Test Proof

Focused tests passed:
- `tests/test_commands.py::test_load_runtime_state_reads_host_control_plane_layout`
- `tests/test_commands.py::test_status_can_report_host_control_plane_authority`
- `tests/test_commands.py::test_status_reports_runtime_surface`
- `tests/test_runtime_coordinator.py`

## Why This Matters

The live host is now emitting real subagent rollup and richer goal/follow-through state after the approval+subagent repair. Without this slice, the repo-side operator surface would still drop those fields, leaving a gap between live truth and repo-side status rendering.

This slice reduces that mismatch and moves the project closer to one canonical summary contract across:
- repo-side bounded runtime
- live eeepc host control-plane
