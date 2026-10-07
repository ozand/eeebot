---
title: Reflector selects the authoritative executor attempt
status: proposed
date: 2026-10-07
authors: [eeebot maintainers]
related: ["#2034", "#1546"]
tags: [runtime, reflector, telemetry, provenance]
---

# ADR-039: Reflector selects the authoritative executor attempt

## Status

Proposed — implementation is not authorized by this record until accepted.

## Context

Issue #2034 identifies that `reflector._prompt_records()` chooses the greatest
`seq` per `cycle_id`, while LLM prompt sequence numbers are allocated per
`(cycle_id, component)`. A planner transcript can therefore outrank the executor
transcript. Current `partial_view` measures truncation, not participant identity.

The bridge already resolves the authoritative executor spawn (primary or latest
successful repair) via `_authoritative_subagent_task_id()` (#1546), but prompt
records currently have no spawn/attempt identifier. Reusing that helper without
adding a correlation would not identify which prompt belonged to that spawn.

## Decision

Proposed: use the existing `task_id` assigned to each `SubagentManager.spawn()`
as the correlation value, exposed as optional `subagent_task_id` in executor
prompt records via an explicit `call_context` argument. The bridge passes each
primary/repair task ID to its corresponding executor manager; there is no
inference from `cycle_id`, sequence, or timestamps. The reflector reuses the
existing `_authoritative_subagent_task_id()` resolver, selects only records
whose `subagent_task_id` exactly matches its result and whose component is
`executor`, then chooses the maximum sequence within that attempt.

Legacy records without `subagent_task_id`, planner-only cycles, and resolved
attempts without matching prompt records are declined/unknown; never substitute
a planner or unrelated executor. The reflection records analysed component
and sequence; task ID is selection-only metadata and is not copied into public
reflection output. Prompt contents and credentials remain excluded.

## Consequences

### What gets easier

Reflections can be attributed to the executor attempt that actually represents
the cycle, including a successful repair rather than a failed primary attempt.

### What gets harder

Attempt identity must be propagated consistently to prompt telemetry, retained
for the reflector's bounded read, and safely handled for legacy records.

### What does not change

Prompt text remains private; planner-only work is not represented as executor
analysis; transcript completeness remains a separate measure.

## Alternatives Considered

- **Filter to component `executor`, then max sequence:** fixes planner-versus-
  executor crossover but cannot distinguish primary from repair attempts; does
  not satisfy #2034's successful-repair criterion.
- **Reuse `_authoritative_subagent_task_id()` without correlation:** rejected;
  the helper returns a spawn ID, while current prompt rows contain no spawn ID.
- **Keep max sequence across all components:** preserves the identified defect.

## Test Contract

| Claim | Test | Status |
|---|---|---|
| Executor is selected over a planner with a higher sequence | reflector selection test | not yet written |
| Planner-only cycle is declined/unknown, never silently substituted | reflector selection test | not yet written |
| Successful repair's prompt is selected over failed primary | correlation/resolver integration test | not yet written |
| Component and sequence are recorded; prompt text remains private | metadata/privacy test | not yet written |

## References

#2034, #1546; `nanobot/runtime/reflector.py`; `nanobot/observability/llm_telemetry.py`;
`nanobot/runtime/bridge.py`; `tests/test_reflector.py`.
