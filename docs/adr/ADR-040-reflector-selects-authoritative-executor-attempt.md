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

Use the existing private prompt telemetry as the correlation store. This is a
proposed application-path contract for the runtime writer and reflector reader,
not a claim that filesystem ACLs currently enforce isolation. Add a
private `attempt_id` field to each prompt row, set from the subagent manager's
own spawn ID in the existing telemetry-context seam. At cycle completion, the
bridge persists one private authoritative-resolution record containing the
resolved `attempt_id` selected by `_authoritative_subagent_task_id()`.
Prompt rows are append-only; because authoritative resolution exists only
after the attempt loop, mutating a prompt row to mark the authoritative attempt
would break the existing JSONL writer contract. The smallest additional store
is therefore one atomic per-cycle resolution sidecar under
`<resolved_llm_calls_root>/prompts/attempt_resolutions/`, named by SHA-256 of
the cycle ID (the raw ID remains only inside the private JSON). Resolve the
existing telemetry root once per producer/consumer/prune operation using the
existing precedence: explicit `LLM_CALLS_DIR` first; otherwise the operation's
passed `state_dir / "llm_calls"`; otherwise preserve the telemetry resolver's
existing `STATE_DIR / "llm_calls"` and home default for callers without an
explicit state dir. Resolve once at the operation/cycle boundary, then pass
that same root to prompt writing, bridge correlation writing, reflector
reading, and prompt pruning; the prompt directory is always
`<resolved_llm_calls_root>/prompts/`. When the selected root is invalid or
unreadable, fail closed as unavailable; never retry a lower-precedence root.
Do not independently resolve paths in those stages, read both roots, or
silently mix an environment root with a passed state root. The resolution record stays in the prompt retention domain,
not the 90-day cycle ledger or public reflection journal, and contains no
prompt text, task title, task request, or public identifier.

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
`<resolved_llm_calls_root>/prompts/attempt_resolutions/<sha256(cycle_id)>.json`,
beside the existing daily prompt files written and pruned by telemetry and
read by `reflector._prompt_records()`. Each operation explicitly passes the
resolved root to `record_llm_prompt()`, reflector prompt reading, bridge
attempt-resolution writing, and prompt pruning; none re-resolves it from a
different state root midway through that operation. Add the resolution subpath and its bridge writer to
`nanobot/runtime/state_paths.py`; the existing `llm_calls` segment already
names prompt and call writers. A custom `LLM_CALLS_DIR` configures the
existing telemetry path; it is not a new access path or feature. When selected,
correlation metadata does not read, copy, or create files under the passed
`state_dir/llm_calls` location. Each resolution stores the selected prompt's
UTC timestamp and expiry, and expires no later than that prompt row. The prompt
prune operation under that same resolved root must also remove a resolution
when its exact associated prompt is pruned; the reader independently rejects
expired resolution or missing associated prompt. An invalid or unreadable
resolved root fails closed as unavailable and must not fall back to another
root or instance. Bind the sidecar to the selected prompt's exact sequence and
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
- **Writes:** use only the resolved existing telemetry root; create missing
  files privately with restrictive permissions (directory `0700`, file `0600`
  on POSIX), write complete records and use same-directory temporary file plus
  flush/fsync and atomic replace for any replace/update operation. Do not
  follow symlinked destination or temporary paths. Keep Windows behavior
  platform-qualified: test POSIX mode bits where supported, but do not claim
  they establish equivalent Windows ACL isolation; no new OS sandbox or role
  boundary is implied. The ownership and ACL of the actual resolved telemetry
  root and its parents are the outer boundary. The `state_dir` ownership/ACL
  applies only when that directory is actually an ancestor of the selected
  default telemetry root; it does not protect a separate custom
  `LLM_CALLS_DIR`. Do not claim effective ACL enforcement without
  implementation and platform verification.

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
retention. The application-path reader restriction does not confer OS-role
isolation. Filesystem permissions are governed by the actual resolved
telemetry-root parent chain; a separate custom root is not protected by
`state_dir` ACLs. Current readers and older prompt rows remain intentionally
unavailable.

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

## Test Contract (implementation evidence on this branch; not yet on main)

The named tests below exist in this implementation branch. They use synthetic prompt and bridge records; representative end-to-end fixtures exercise prompt production, bridge resolution, retained sidecar, and reflector consumption. No live provider, host, or private runtime data is used. CI-on-main and independent exact-revision review remain required before any ADR status change.

| Claim | Existing branch test | Verified assertion and limitation |
|---|---|---|
| Exact attempt defeats a higher-sequence planner | `tests/test_reflector.py::test_reflector_selects_resolved_executor_over_higher_sequence_planner` | Real telemetry prompt writer and resolver sidecar; returned prompt is executor seq 1 despite planner seq 3; raw attempt ID is not returned. |
| Successful repair is selected through bridge resolution | `tests/test_reflector.py::test_reflector_selects_bridge_authoritative_successful_repair_attempt` | Synthetic prompt producer + `_authoritative_subagent_task_id()` + persisted sidecar + reflector; primary error and successful repair select repair seq 2. |
| Primary fallback behavior | `tests/test_bridge_authoritative_subagent.py::TestHelpers::test_authoritative_resolution_falls_back_to_primary_when_no_repair_ok` | Existing bridge resolver unit test; prompt production/reflector join is not exercised in this case. |
| Missing, legacy, unmatched, cross-cycle, expired and malformed metadata does not fall back | `tests/test_reflector.py::test_reflector_missing_unmatched_cross_cycle_and_expired_resolution_are_unavailable`; `tests/test_reflector.py::test_reflector_legacy_prompt_without_resolution_is_unavailable`; `tests/test_reflector.py::test_reflector_invalid_expiry_metadata_is_unavailable`; `tests/test_reflector.py::test_reflector_duplicate_sidecar_keys_fail_closed`; `tests/test_reflector.py::test_reflector_aware_expiry_accepts_exact_value_and_rejects_at_boundary` | Reader returns no usable prompt; naive/null/wrong-type/malformed expiry cases, duplicate JSON keys, and exact aware expiration boundary are exercised. Other malformed/unsupported metadata variants remain uncovered. |
| Custom root is authoritative over a default-root decoy | `tests/test_reflector.py::test_reflector_custom_root_and_decoy_default_root` | Producer and reader use the resolved custom root; a higher-sequence matching decoy under the passed state root is ignored. Default-root decoy repetition is not separately covered. |
| Retention couples sidecar deletion and prompt pruning | `tests/test_llm_telemetry.py::test_authoritative_attempt_sidecar_is_pruned_with_prompt`; `tests/test_llm_telemetry.py::test_attempt_resolution_cleanup_failure_preserves_prompt` | Sidecar expires/deletes with selected prompt; failed sidecar cleanup preserves the prompt. Full retention-policy boundary matrix is not covered. |
| Missing correlation remains distinct from pruning and can recover within bounded retries | `tests/test_reflector.py::test_reflector_missing_prompt_correlation_is_not_pruned`; `tests/test_reflector.py::test_reflector_retries_unavailable_correlation_when_evidence_arrives`; `tests/test_reflector.py::test_reflector_capture_disabled_does_not_claim_prompt_was_pruned`; `tests/test_reflector.py::test_legacy_skipped_pruned_row_is_reopened_when_prompt_and_resolution_arrive` | Missing sidecar with an existing prompt is unavailable; publishing valid resolution makes that cycle eligible later; capture-off does not claim a transcript was pruned; legacy skip rows remain compatible. |
| Real manager spawn propagates its actual ID through prompt writer, authoritative bridge selection, and reflector reader | `tests/test_subagent_manager.py::test_subagent_spawn_id_reaches_executor_prompt_telemetry` | Real `SubagentManager.spawn()` and stub provider persist the actual spawn ID; bridge primary selection writes the sidecar and the reflector consumes it. Successful repair selection remains covered by a separate synthetic bridge/consumer fixture. |
| Metadata safety and platform-qualified permissions | `tests/test_reflector.py::test_reflector_rejects_symlinked_attempt_metadata_and_checks_private_mode` | Sidecar symlink fails closed; POSIX directory/file modes checked only on POSIX. Other malformed schema/size/UTF-8 cases and Windows ACL behavior are not claimed. |

Post-deployment mismatch-rate evidence remains unverified. The manager-to-primary-bridge-to-reflector path is exercised with a stub provider; successful repair selection uses a synthetic bridge fixture rather than a live manager repair orchestration. ADR acceptance remains gated on independent review and test-on-main evidence.

## References

#2034, #1546; [owner research delegation](https://github.com/ozand/eeebot/issues/2034#issuecomment-6526852989);
`nanobot/runtime/reflector.py`; `nanobot/observability/llm_telemetry.py`;
`nanobot/runtime/bridge.py`; `nanobot/agent/subagent.py`;
`nanobot/runtime/state_paths.py`; `tests/test_reflector.py`.
