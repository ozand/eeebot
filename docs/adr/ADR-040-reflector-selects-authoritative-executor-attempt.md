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

Proposed. This bounded design refinement records the technical selection
requested by the owner in [#2034 research delegation](https://github.com/ozand/eeebot/issues/2034#issuecomment-6526852989).
The delegation permits source-only design work, not implementation or ADR
acceptance. Owner acceptance and the repository's test-on-main acceptance
procedure remain required.

## Context

Issue #2034 identifies that `reflector._prompt_records()` chooses the greatest
`seq` per `cycle_id`, while LLM prompt sequence numbers are allocated per
`(cycle_id, component)`. A planner transcript can therefore outrank the
executor transcript. `partial_view` measures truncation, not participant
identity.

The bridge resolves the authoritative executor spawn using
`_authoritative_subagent_task_id()` (#1546): the most recent successful repair,
otherwise the primary spawn. This resolution and the spawn IDs are currently
transient bridge values, and prompt rows do not identify their spawn. The
reflector runs later and cannot reconstruct that resolution from prompt rows.

## Decision (proposed; owner acceptance pending)

Use the existing private prompt telemetry as the correlation store. Add a
private `attempt_id` field to each prompt row, set from the subagent manager's
own spawn ID in the existing telemetry-context seam. At cycle completion, the
bridge persists one private authoritative-resolution record containing the
resolved `attempt_id` selected by `_authoritative_subagent_task_id()`.
Prompt rows are append-only; because authoritative resolution exists only
after the attempt loop, mutating a prompt row to mark the authoritative attempt
would break the existing JSONL writer contract. The smallest additional store
is therefore one atomic per-cycle resolution sidecar under
`state/llm_calls/prompts/attempt_resolutions/`, named by SHA-256 of the cycle
ID (the raw ID remains only inside the private JSON). It is in the existing
prompt telemetry retention domain, not the 90-day cycle ledger or public
reflection journal. The resolution record is strictly selection metadata; it
contains no prompt text, task title, task request, or public identifier.

The reflector accepts a candidate only when exactly one well-formed
resolution record for its cycle names an attempt and there is at least one
well-formed prompt row with the same cycle, exact attempt ID, and
`component == "executor"`. It chooses greatest non-negative integer `seq`
within that exact attempt. Sequence ties are ambiguous and unavailable; never
break ties by file order, timestamp, planner sequence, or another component.
Reject duplicate/conflicting resolution records, duplicate/reused attempt IDs
within the cycle, malformed or missing IDs, unsupported schema, and malformed
sequence values as unavailable. A cycle with only planner rows, only a
planner-selected attempt, no matching prompt row, or a legacy row without
attempt identity is unavailable. Do not fall back to a planner, another
executor attempt, or cycle-wide maximum sequence.

### Storage and retention contract

The exact path seam is
`state/llm_calls/prompts/attempt_resolutions/<sha256(cycle_id)>.json`, beside
the existing daily prompt files read by `reflector._prompt_records()` and
written by `record_llm_prompt()`. Add this state segment and its bridge writer
to `nanobot/runtime/state_paths.py`; the existing `llm_calls` segment already
names both prompt and call writers. Each resolution stores the selected prompt's
UTC timestamp and expiry, and expires no later than that prompt row. The prompt
prune operation must also remove a resolution when its exact associated prompt
is pruned; the reader independently rejects expired resolution or missing
associated prompt. Bind the sidecar to the selected prompt's exact sequence and
UTC timestamp. Compute expiry from that prompt's UTC daily-file date plus the
effective retention period (the same date cutoff used by prompt pruning), not
from bridge completion time. Prompt rows remain the deletion authority; a
sidecar is invalid if the exact selected prompt row is absent, even when its
expiry timestamp has not elapsed. No independent 90-day lifetime. Use the existing effective
`LLM_PROMPTS_RETENTION_DAYS` policy (`nanobot/observability/llm_telemetry.py`,
default 14 days, invalid value falls back to 14, values clamp to at least one)
and test that resolution cannot outlive its prompt. Prompt capture disabled or
missing/expired prompt evidence means no usable correlation and therefore
unavailable. The implementation must account for retention/pruning and
concurrent daily-file rotation; if the existing daily-file format cannot meet
this coupling without retaining a record longer than its prompt, stop and
revise this design rather than add an unbounded second store.

### Writer, reader, validation, and permissions

- **Correlation writer:** `SubagentManager` supplies its own existing spawn ID
  to the prompt telemetry context; `record_llm_prompt()` writes that opaque ID
  only in private prompt metadata. Do not infer it from cycle ID or task order.
- **Resolution writer:** the bridge writes the result of its existing
  `_authoritative_subagent_task_id()` resolution after the attempt loop. A
  primary fallback is valid under that helper's current contract, including
  when no attempt has status `ok`; reflector still requires an exact executor
  prompt for the selected ID.
- **Reader:** the runtime reflector only, through its existing bounded prompt
  reader. This is an application-path restriction, not OS-role isolation:
  reflector and bridge may run under the same OS identity.
- **Record format:** versioned, bounded JSON object with exact fields `schema`,
  `cycle_id`, `attempt_id`, `prompt_seq`, `prompt_ts`, `created_at`, and
  `expires_at`.
  Validate exact key/types, non-empty bounded strings, allowed schema version,
  parseable UTC timestamps, and expiry no later than the associated prompt
  expiry. Prompt rows independently require a non-negative integer `seq`;
  reject booleans as integers. Reject duplicate/conflicting records,
  invalid UTF-8/JSON, oversized rows, and symlinked or escaping input paths.
  No raw IDs, prompts, or private paths appear in public output or logs.
- **Writes:** use the existing state directory only; create missing files
  privately with restrictive permissions (directory `0700`, file `0600` on
  POSIX), write complete records and use same-directory temporary file plus
  flush/fsync and atomic replace for any replace/update operation. Do not
  follow symlinked destination or temporary paths. Keep Windows behavior
  platform-qualified: test permissions where supported, document that POSIX
  mode assertions do not establish equivalent Windows ACL isolation, and test
  atomic-replace behavior on supported platforms. The state directory's
  existing ownership/ACL remains the outer boundary; file modes alone do not
  claim role separation.

### Public provenance and unavailable outcomes

A successful reflection may expose only an allowlisted safe provenance tuple:
`component: "executor"`, selected non-negative `seq`, and an allowlisted
selection disposition (for example `authoritative_attempt`). Never expose the
attempt ID, spawn/task ID, prompt content, task/request IDs, or private paths.
Missing, legacy, malformed, expired, planner-only, ambiguous, conflicting,
or unmatched evidence produces an explicit `unavailable` disposition and a
bounded reason code; it never produces a generic executor fallback or silent
empty success.

## Consequences

### What gets easier

The reflector can select the actual primary-or-successful-repair executor
attempt without relying on sequence ordering across components or attempts.

### What gets harder

The bridge and prompt writer must share a private attempt-correlation seam, and
the resolution record's deletion must stay coupled to its exact prompt's
retention. State-file permissions and atomic-write behavior need platform-aware
tests. Current readers and older prompt rows remain intentionally unavailable.

### What does not change

Prompt text stays private; planner-only work is not represented as executor
analysis; transcript completeness remains separate from participant identity.
No host access, provider calls, new dependency, database, generic correlation
framework, or new public identifier is part of this design.

## Alternatives considered

- **Filter to component `executor`, then max sequence:** prevents
  planner-versus-executor crossover but cannot distinguish primary from repair.
- **Use `_authoritative_subagent_task_id()` without prompt correlation:** the
  helper returns a spawn ID, while current prompt rows contain no spawn ID.
- **Read the 90-day cycle ledger or existing subagent result as a durable
  resolution store:** rejected for this design because it lengthens retention
  of private identifiers beyond prompt retention and adds another privacy
  lifetime to reconcile.
- **Keep max sequence across components:** preserves the identified defect.

## Test Contract (prospective; no tests added in this design task)

Each named test is a required prospective contract, not evidence it currently
exists. Fixture tests must use synthetic prompt and bridge records and exercise
the actual manager spawn-ID -> prompt writer -> bridge authoritative resolver
-> retained sidecar -> reflector consumer data shape; no live provider, host,
or private runtime data.

| Claim | Prospective test | Required fixture/assertion |
|---|---|---|
| Exact attempt defeats a higher-sequence planner and unrelated executor | `tests/test_reflector.py::test_selects_resolved_executor_over_higher_seq_planner` | Use the actual manager context and prompt writer with synthetic attempt IDs; planner and other executor have larger seq; only exact selected executor may be reflected; public output has only allowlisted provenance. |
| Successful repair is selected after bridge exit | `tests/test_reflector.py::test_selects_persisted_successful_repair_attempt` | Synthetic primary failure and successful repair; bridge's existing resolver selects repair; persisted resolution then joins its prompt. |
| Primary fallback uses bridge's resolver, not reflector inference | `tests/test_reflector.py::test_selects_primary_fallback_from_bridge_resolution` | No successful repair; exact primary prompt selected. |
| Planner-only, missing, legacy, expired, malformed, unmatched, or unsupported metadata is unavailable | `tests/test_reflector.py::test_missing_attempt_resolution_is_unavailable` | Parameterized absent/legacy/planner-only/malformed/unmatched/expired cases; assert no fallback. |
| Duplicates, reused IDs, ties, conflicting resolutions, and invalid sequence cannot choose a row | `tests/test_reflector.py::test_ambiguous_attempt_evidence_is_unavailable` | Synthetic collisions and `bool`/negative/non-integer sequence values; explicit unavailable reason. |
| Private correlation never enters public output | `tests/test_reflector.py::test_reflection_records_safe_provenance_only` | Assert no attempt/task/request IDs, prompt text, or paths in row or logs. |
| Resolution expires/deletes with associated prompt under effective configured retention | `tests/test_llm_prompt_recording.py::test_attempt_resolution_retention_does_not_outlive_prompt` | Fake clock/files across default, configured, invalid, and minimum retention; exact selected prompt row disappears at prune and no orphan private ID remains. |
| File safety is bounded and platform-honest | `tests/test_reflector.py::test_attempt_metadata_rejects_symlink_and_invalid_record` | Symlink/escape, schema/size/UTF-8 failures; POSIX mode check when available and atomic replacement behavior; Windows assertions do not claim POSIX ACL equivalence. |
| Prompt, resolution, and reflection payloads do not leak private identifiers or content | `tests/test_reflector.py::test_attempt_correlation_privacy_boundaries` | Inspect synthetic private prompt/resolution records and public reflection/log output; assert private fields exist only in protected telemetry and never in public output/logs. |
| Historical post-deployment wrong-component evidence remains unverified without exact-attempt correlation | `tests/test_reflector.py::test_historical_wrong_component_evidence_is_unverified` | Use only the original sanitized/source-checked fixture, if available; no live/private transcript reads. Assert the component mismatch remains `unavailable`/`unverified`, with no inferred attempt identity or fallback. |

The contract is not eligible for ADR acceptance until every item maps to an
existing test on `main` citing `ADR-040`, or is explicitly deferred under the
ADR acceptance procedure, and the operator records acceptance by commit.

## References

#2034, #1546; [owner research delegation](https://github.com/ozand/eeebot/issues/2034#issuecomment-6526852989);
`nanobot/runtime/reflector.py`; `nanobot/observability/llm_telemetry.py`;
`nanobot/runtime/bridge.py`; `nanobot/agent/subagent.py`;
`nanobot/runtime/state_paths.py`; `tests/test_reflector.py`.
