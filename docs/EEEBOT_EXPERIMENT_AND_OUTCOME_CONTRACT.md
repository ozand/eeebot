# eeebot Experiment and Outcome Contract

Last updated: 2026-04-21 UTC

## Purpose

This document defines the minimum required structure for experiments and their outcomes inside the self-improving runtime.

## Experiment contract fields

Each experiment should have a durable contract with fields such as:
- schema_version
- experiment_id
- cycle_id
- goal_id
- current_task_id
- selected_tasks
- task_selection_source
- contract_type
- run_budget
- success_metric
- baseline_ref
- keep_rule
- discard_rule
- crash_rule
- blocked_rule
- mutation_scope

## Required outcome fields

Each completed experiment should persist:
- outcome
- metric_name
- metric_baseline
- metric_current
- metric_frontier
- contract_path
- revert_required
- revert_status
- revert_path (when relevant)

## Valid outcomes

### keep
The experiment produced an acceptable or improved result.

### discard
The experiment did not beat the baseline or is not worth retaining.
A revert work item should be created when appropriate.

### crash
Execution failed before producing a valid evaluable result.

### blocked
Execution could not proceed due to gating or dependency constraints.

## Revert discipline

When outcome = discard, the system should emit a durable revert record.
This ensures discard is an operational decision, not only a label.

## Why this matters

Without an explicit contract and explicit outcome, the runtime degenerates into narration.
With the contract, each cycle becomes measurable and reconstructable.

## Current bounded budget baseline

The current conservative runtime budget baseline is:
- max_requests = 2
- max_tool_calls = 12
- max_subagents = 2
- max_timeout_seconds = 900

This is intentionally still bounded. It is larger than the original ultra-conservative `1 / 8` budget, but it keeps the same safety model while allowing one cycle to do slightly richer work before writing evidence and stopping.
