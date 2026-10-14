---
title: Reflector selects the authoritative executor attempt
status: proposed
date: 2026-10-07
authors: [eeebot maintainers]
related: ["#2034", "#1546"]
tags: [runtime, reflector, telemetry, provenance]
---

# ADR-040: Reflector selects the authoritative executor attempt

## Status

Proposed — implementation is not authorized by this record until accepted.

## Context

Issue #2034 identifies that `reflector._prompt_records()` chooses the greatest
`seq` per `cycle_id`, while LLM prompt sequence numbers are allocated per
`(cycle_id, component)`. A planner transcript can therefore outrank the executor
transcript. Current `partial_view` measures truncation, not participant identity.

The bridge resolves the authoritative executor spawn (primary fallback or
latest successful repair) via `_authoritative_subagent_task_id()` (#1546), but
prompt records currently have no spawn/attempt identifier. The helper's primary
and repair IDs are transient bridge values; the reflector runs later and cannot
reconstruct the authoritative attempt from the existing prompt rows alone.

## Decision

Proposed: authoritative attempt identity must be recoverable by the later
reflector from a bounded private durable record. Adding `subagent_task_id` to
prompt rows is necessary to correlate a prompt to a spawn, but not sufficient
by itself: the primary/repair IDs and successful-repair resolution are
currently transient bridge inputs. The implementation contract must persist a
private per-cycle resolution containing the authoritative task ID (or an
unambiguous primary/repair role and ordered IDs) where the reflector can read
it under a bounded retention policy. The exact storage owner/path and retention
period remain implementation design details to settle before coding; the
record must be access-controlled as runtime state, not placed in public
reflection output.

Once durable resolution is available, the reflector joins prompt rows to that
exact ID and requires component `executor`, then selects max sequence only
within that attempt. No inference from cycle-wide sequence, timestamp, or
planner ordering is permitted. `subagent_task_id` and resolution evidence are
selection-only private metadata. Public reflection may record analysed
component and sequence, but never task IDs or prompt text.

Legacy cycles without correlation/resolution evidence, planner-only cycles,
missing or malformed durable records, and attempts with no matching executor
prompt are declined/unknown with an explicit reason; never substitute a
planner or unrelated executor. Implementation requires explicit owner
acceptance of the durable private correlation, access, and retention contract.
This proposed ADR does not authorize source, telemetry, schema, or host changes.

## Consequences

### What gets easier

Reflections can be attributed to the executor attempt that actually represents
the cycle, including a successful repair rather than a failed primary attempt.

### What gets harder

Attempt identity and authoritative primary/repair resolution must be persisted
privately and retained long enough for a later bounded reflector read. Access,
retention, legacy unknown handling, and the privacy boundary need explicit
owner acceptance before implementation.

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
| Executor is selected over a planner with a higher sequence using exact durable attempt identity | `tests/test_reflector.py::test_selects_resolved_executor_over_higher_seq_planner` | not yet written |
| Planner-only cycle is declined/unknown, never silently substituted | `tests/test_reflector.py::test_planner_only_cycle_is_unknown` | not yet written |
| Successful repair's prompt is selected over failed primary using persisted authoritative resolution after bridge exit | `tests/test_reflector.py::test_selects_persisted_successful_repair_attempt` | not yet written |
| Missing/ambiguous durable resolution or unmatched executor prompt yields unknown | `tests/test_reflector.py::test_missing_attempt_resolution_is_unknown` | not yet written |
| Component and sequence are recorded; task ID and prompt text remain private | `tests/test_reflector.py::test_reflection_records_safe_provenance_only` | not yet written |

## References

#2034, #1546; `nanobot/runtime/reflector.py`; `nanobot/observability/llm_telemetry.py`;
`nanobot/runtime/bridge.py`; `tests/test_reflector.py`.
