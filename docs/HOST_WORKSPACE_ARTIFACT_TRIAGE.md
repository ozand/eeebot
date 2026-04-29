# Host Workspace Artifact Triage

Last updated: 2026-04-29 UTC

## Purpose

This note classifies host-created workspace artifacts so the self-evolving runtime
does not accidentally treat every file as a canonical task or governance surface.

## Runtime Workspace Policy

`workspace/` is runtime state, not canonical source.

Current policy:
- keep `.gitignore` ignoring `workspace/`;
- do not commit cycle reports, promotion candidates, outbox files, local runtime releases, approvals, caches, or other `workspace/state/...` artifacts;
- preserve useful observations by moving the durable lesson into a tracked document under `docs/` or `docs/userstory/` rather than committing the raw runtime artifact;
- when a runtime artifact is needed as proof, cite its local path, timestamp, and summary in the GitHub issue/PR comment instead of adding the file to the repository.

For issue #133, the live `workspace/` tree was confirmed to be ignored by `.gitignore` and has zero tracked entries in `git ls-files workspace`.

## Durable Recovery Drill Policy

`docs/userstory/BROKEN_HOST_RECOVERY_DRILL.md` is product-worthy durable documentation, not runtime noise.

It should remain tracked and indexed because it defines a read-only broken-host recovery decision drill:
- classify `degraded_but_recoverable` vs `unrecoverable_without_rebuild`;
- stop before unsafe live-host mutation;
- preserve rollback/rebuild criteria and proof boundaries.

The user-story index references it alongside `BROKEN_HOST_RECOVERY_DECISION_TRACE.md`.

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
