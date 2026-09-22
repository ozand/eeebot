---
name: task-writing
description: Define one bounded, externally verifiable increment before implementation.
version: "1.0.0"
tags: [planning, tasks, dor, dod]
---

# task-writing

Use this release-owned contract before emitting a task for the executor. It defines the increment; it does not implement it and it does not replace the cycle rules in `OPERATING.md`.

## Required planning output

The planner response remains the JSON schema in `roles/planner.md`: `plan`, `iterations_planned`, `dor`, `dod`, and optional `hypotheses`.

### Definition of Ready (DoR)

For a bounded increment, state:

1. the claim or intended change;
2. the external baseline or observable pre-condition before editing;
3. the expected iteration forecast inside the cycle box;
4. the evaluation kind: `metric`, `script_exit_zero`, `test_count_increase`, or `file_exists`;
5. the external evaluation target;
6. what observation will count as movement.

If no allowlisted metric or external criterion applies, say so explicitly instead of inventing one.

### Definition of Done (DoD)

State the externally verifiable movement and its evaluation target. The target must be outside the loop's mutable commit surfaces: a harness metric, a state/ledger invariant, or another external evaluator. The change must not be able to satisfy its own criterion merely by editing the target artifact or its tests.

Use the existing `nanobot.runtime.task_criteria` rules. Do not create a new state file or metric for a single task.

## Prohibited forms

- Do not define completion as a lexical or prose property such as “contains a heading”, “mentions a keyword”, or “file length increased”.
- Do not duplicate executor rules, mutation policy, git workflow, or test commands from `OPERATING.md`.
- Do not emit a multi-cycle epic as one increment.
- Do not write code or edit files during planning; emit the task definition only.
