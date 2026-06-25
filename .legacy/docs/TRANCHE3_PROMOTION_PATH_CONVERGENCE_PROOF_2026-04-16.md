# Tranche 3 Proof: Richer Promotion-Path Convergence

Last updated: 2026-04-16 UTC

## Goal

Reduce the remaining promotion-path mismatch by surfacing more of the durable promotion trail through the repo-side runtime status model.

## Added Fields

The runtime state/status surface now includes:
- `promotion_reviewed_at`
- `promotion_accepted_at`
- `promotion_patch_bundle_path`

These are derived from the durable trail files:
- `state/promotions/decisions/<candidate>.json`
- `state/promotions/accepted/<candidate>.json`

## Why This Matters

Before this slice, the promotion trail was visible only as presence/absence.
After this slice, the status surface can expose the timing and patch-bundle path of accepted/reviewed promotion outcomes.

This moves the repo-side promotion path closer to a complete operator-facing story.

## Tests Passed

- `tests/test_commands.py::test_status_reports_runtime_surface`
- `tests/test_commands.py::test_load_runtime_state_reads_host_control_plane_layout`
- `tests/test_commands.py::test_status_can_report_host_control_plane_authority`
- `tests/test_runtime_coordinator.py`
