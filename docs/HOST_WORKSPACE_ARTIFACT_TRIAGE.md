# Host Workspace Artifact Triage

Last updated: 2026-03-31 UTC

## Purpose

This note classifies host-created workspace artifacts so the self-evolving runtime
does not accidentally treat every file as a canonical task or governance surface.

## Keep As Useful Evidence / Notes

- focused analysis reports that summarize a bounded experiment or runtime check
- scripts that directly support the bounded workspace runtime lane
- files that are clearly referenced by a runtime report, experiment, or validation result

Examples already seen on the host:

- `tool_iteration_tracker.py`
- `tool_usage_templates.md`
- `experiment_analysis.md`
- `best_practices_documentation.md`
- `final_optimization_report.md`
- `optimization_experiment.py`
- `simple_optimization_report.md`

These should be treated as provisional workspace artifacts, not as a second backlog.

## Likely Noise / Duplicate System Risk

- any new `tasks/` directory used as a second task registry
- free-form progress logs that duplicate `todo.md`, `done.md`, or `state/reports`
- long-lived markdown trackers that restate the same work without adding evidence

## Decision Rule

- keep artifacts that support one bounded experiment, one runtime check, or one evidence-backed report
- avoid artifacts that try to become a second planning or release system
