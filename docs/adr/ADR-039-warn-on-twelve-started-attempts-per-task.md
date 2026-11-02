---
title: Warn operators after twelve unsuccessful started attempts for one task
status: accepted
date: 2026-10-13
authors: [ozand, pi]
related: ["#2088", "#2050", "#2052", "ADR-035"]
tags: [runtime, observability, privacy, h5]
---

# Status

Accepted by the delegated delivery executor under the explicit owner-approved technical delegation recorded on #2143, via PR #2147, on 2026-11-02. This is delegated technical acceptance, not personal operator approval. The 12-row test contract was semantically audited against published main `97cc60442ea918d3b63fc2fa6ddb470142936a30`; acceptance verification and exact-revision independent review evidence are recorded in PR #2147. This acceptance authorizes no host rollout.

# Context

Issue #2050 defines a global tripwire: zero successes in six hours OR 12 consecutive started cycles without success, producing an immediate operator signal with no automatic rollback. A distinct user-approved H5 request is a per-task warning after 12 consecutive started attempts for one task without success. Cycle count and task-attempt count are different denominators and must not be conflated.

**Source-verified** against immutable source SHA `99b87f47cb4a8cee8b517ff78730ed391170f3e6`: the existing canonical key is candidate ID verbatim, or `self-` plus a hash of the trimmed first plan-title line; it is computed before the write-ahead start, whose row does not persist `retry_key`. Immediate success includes the key; late `pushed_late` outcomes do not. This source check supports the design premise; it does not prove the same code is installed on the current host.

The exact-key choice intentionally accepts an edited self-directed first-line title as a new identity; it does not establish continuity across changed wording. A late-push success with no exact key lineage cannot reset or be assigned to a key. Neither case may be repaired by fuzzy title/path matching, timestamp proximity, or cycle proximity. Treat any resulting uncertainty as UNKNOWN.

The active cycle ledger is append-driven. `CYCLE_LEDGER_RETENTION_DAYS` defaults to 90 and bounds archive retention, but does not guarantee physical age-based pruning of the active file. Treat 90 days as the effective evidence horizon, not an exact deletion deadline. This design uses no host/provider access or private runtime-history reads.

# Decision

**Proposed:** define a task-scoped H5 warning by counting consecutive `started` attempts that carry the same canonical `retry_key` and have no later successful terminal outcome for that key. Emit an operator-facing warning at 12. The warning is informational only: it does not block planning/execution, wake or prioritize work, alter retry/handled state, or trigger rollback.

This is a proposed persisted-data contract refined under the delegated technical direction recorded in #2143; it is not ADR acceptance or host/rollout authority. Implementation must preserve the following conservative evidence rules.

## What this IS

- Add the already-computed canonical `retry_key` to the write-ahead `started` event before execution, without changing event ordering or retry behavior. A missing key on any legacy or future row is not a match.
- Count only exact `retry_key` equality. A changed self-directed first title line may create a new identity; no lineage is inferred. Planning/rest and duplicate-skip rows are not starts. Keep/resume attempts may legitimately append repeated `started` rows with the same `cycle_id` and same exact `retry_key`; count each as a started attempt. If the same cycle ID is reused with a different key, coverage is UNKNOWN. A duplicate-skip terminal row may subtract one start only when its `cycle_id` maps to one exact key and any supplied `retry_key` matches that key; after repeated starts with the same cycle/key, a keyless skip remains ambiguous and makes coverage UNKNOWN rather than guessing. Repeated or unjoined skip outcomes also yield UNKNOWN.
- Establish success reset only from a successful terminal outcome carrying that same exact key. A `pushed_late` success without exact key remains UNKNOWN, never assigned by time/cycle proximity.
- Emit a warning at 12 only when the retained evidence proves 12 starts for the exact key and no possible later reset; the aggregate task count remains unknown unless global completeness is established. A result below threshold is UNKNOWN because an exact success anchor for observed keys cannot prove that older, possibly pruned keys are absent. Twelve exact starts after a keyless late-push or other unknown reset may prove a warning only when that post-reset suffix is demonstrably complete; a missing active file, capped read, or other coverage gap defeats that proof. Never infer by nearest time/cycle/title.
- Treat missing, expired, malformed (including invalid UTF-8), or partial ledger coverage as UNKNOWN. The H5 strict reader rejects invalid UTF-8 bytes; a valid literal U+FFFD remains valid Unicode and is not itself malformed. The effective evidence horizon is configured by `CYCLE_LEDGER_RETENTION_DAYS` (default 90); the existing ledger exposes no inception/global-coverage watermark, and the active file is not physically pruned by age. Therefore an apparently complete retained slice does not establish globally complete task-key history or justify an aggregate clear. Do not add persistent markers or infrastructure under this proposal.
- Extend the existing operator health/reporting surface; do not introduce another notifier/service. Public output exposes warning/UNKNOWN and coverage only; it never emits raw keys or guessed per-task counts when continuity is uncertain.
- Warning is informational only: no denial at 13, block, wake, prioritization, retry/handled-state mutation, or rollback.

## What this IS NOT

- A replacement for #2050/#2052's global six-hour / 12-cycle tripwire.
- A planner gate, retry-policy change, queue wakeup, task scheduler, or automatic rollback.
- A provider-failure classifier. `executor_llm_error` or any other outcome flag is not itself proof of a provider call failure.
- A mandate to retain raw task keys indefinitely or to backfill historical attempts.

## Success criteria

- Exactly 11 consecutive starts for one key do not warn; the 12th does.
- Starts for different keys never combine; a success for a key resets only that key's streak.
- Duplicate skips and planner sessions do not increment the started-attempt count; same-key keep/resume starts sharing a cycle ID each count. Conflicting-key cycle reuse and ambiguous, mismatched, unjoined, or repeated skip joins yield UNKNOWN.
- Missing historical keys, incomplete archive coverage, or ambiguous joins produce UNKNOWN, not a zero/healthy state.
- Public output contains only warning/UNKNOWN and coverage, with tests proving raw-key non-disclosure and no guessed count when continuity is uncertain.
- Warning emission has no effect on planner eligibility, retry counters, marker state, wake behavior, or rollback.

# Consequences

## What gets easier

Operators can distinguish repeated attempts of one task from a global cycle-level stall and can see when evidence is incomplete.

## What gets harder

The append-only start ledger gains a sensitive correlation field. Retention, access, redaction/logging, migration, and old-row coverage need explicit policy. Consumers must preserve UNKNOWN rather than coerce absent keys to zero. A full retention horizon may be needed to compute a true streak.

## What does not change

The global H5 tripwire, executor behavior, retry budget, candidate selection, per-call telemetry, systemd/bridge limits, and deployment policy remain unchanged. No automatic action follows either warning.

# Alternatives Considered

## Count only cycle IDs

Rejected for the per-task warning: retries/resumes may reuse or vary cycle identity, while the intended denominator is a stable task across cycles. Cycle IDs remain appropriate for the separate global tripwire.

## Infer task identity by title, target, or nearest timestamp

Rejected: titles/targets can change or collide, and timestamp proximity is not a reliable causal join. The existing canonical key is the source identity proposed for the exact join.

## Do not persist a key on start events

Valid alternative if the owner rejects the privacy/retention cost. It leaves per-task counts UNKNOWN whenever an attempt lacks a terminal outcome and cannot fulfill the warning contract reliably; the global H5 tripwire remains available.

## Retain keys forever vs bounded retention/expiry

Indefinite retention could improve historical coverage but increases privacy/storage exposure and is rejected. Use the existing bounded ledger-retention mechanism with `CYCLE_LEDGER_RETENTION_DAYS` defaulting to 90 as the effective evidence horizon. This does not assert that the active ledger file is age-pruned; when expired archive evidence or active-history continuity is missing, the count is UNKNOWN. Pruning is append-driven, so this is not a guarantee of physical deletion exactly at day 90.

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| Start row persists the supplied exact retry key while existing callers retain their prior row shape | `tests/test_cycle_ledger.py::test_record_cycle_started_persists_retry_key_when_supplied`, `tests/test_cycle_ledger.py::test_record_cycle_started_writes_phase_started` | implemented |
| 11 known starts do not warn; the 12th warns, distinct keys do not combine, exact-key success resets only its key, duplicate skips are excluded only after exact unique cycle/key joins; repeated same-key resume starts count individually, while keyless skips after repeated cycle IDs remain UNKNOWN | `tests/test_h5_tripwire.py::test_task_warning_threshold_exact_key_success_reset_and_privacy`, `tests/test_h5_tripwire.py::test_task_warning_excludes_cycles_ending_in_duplicate_skip`, `tests/test_h5_tripwire.py::test_task_warning_mismatched_or_unjoined_duplicate_skip_is_unknown`, `tests/test_h5_tripwire.py::test_task_warning_duplicate_skip_key_must_match_started_cycle_and_is_unique`, `tests/test_h5_tripwire.py::test_task_warning_counts_same_cycle_keep_resume_start_writes`, `tests/test_h5_tripwire.py::test_task_warning_reused_cycle_duplicate_skip_is_unknown`, `tests/test_h5_tripwire.py::test_task_warning_conflicting_key_reused_cycle_is_unknown` | implemented |
| Missing legacy key, keyless late-push, future timestamps, and timezone-naive timestamps do not become known counts or inferred success; 12 exact starts after unknown reset can prove warning while 11 remains UNKNOWN | `tests/test_h5_tripwire.py::test_task_warning_unknown_reset_does_not_emit_under_threshold_count`, `tests/test_h5_tripwire.py::test_task_warning_proves_12_after_late_push_unknown_reset`, `tests/test_h5_tripwire.py::test_task_warning_future_dated_starts_are_unknown`, `tests/test_h5_tripwire.py::test_task_warning_naive_ledger_timestamps_are_unknown` | implemented |
| Edited self-directed title produces a distinct exact retry key | `tests/test_h5_tripwire.py::test_self_title_edit_creates_distinct_warning_identity` | implemented |
| The effective horizon honors `CYCLE_LEDGER_RETENTION_DAYS`; expired, malformed, empty, unanchored, oversized, or byte-capped evidence is UNKNOWN | `tests/test_h5_tripwire.py::test_task_warning_uses_cycle_ledger_retention_not_other_reader_env`, `tests/test_h5_tripwire.py::test_task_warning_empty_or_unanchored_ledger_activity_is_unknown`, `tests/test_h5_tripwire.py::test_task_warning_extreme_retention_returns_unknown`, `tests/test_h5_tripwire.py::test_task_warning_capped_prefix_does_not_ignore_later_success_reset` | implemented |
| Public cycle-health summary exposes warning/UNKNOWN without raw keys or unproven low counts; CLI text and JSON preserve UNKNOWN and warning states; absent global history watermark prevents below-threshold clear | `tests/test_h5_tripwire.py::test_cycle_health_cli_reports_clear_and_unknown_warning_states`, `tests/test_h5_tripwire.py::test_task_warning_expired_and_pruned_other_task_history_never_proves_clear` | implemented |
| A successful outcome with unjoinable timestamp breaks any prior exact-key streak and yields UNKNOWN until enough current starts follow | `tests/test_h5_tripwire.py::test_task_warning_unparseable_success_breaks_streak_before_recount` | implemented |
| Ledger rotation preserves bounded multi-file task streaks; incomplete earlier task history may still prove a warning from a complete 12-start suffix, while capped or errored reads remain UNKNOWN | `tests/test_h5_tripwire.py::test_task_warning_includes_rotated_attempts_and_keeps_other_task_warning`, `tests/test_h5_tripwire.py::test_task_warning_proves_12_after_late_push_unknown_reset`, `tests/test_h5_tripwire.py::test_task_warning_capped_prefix_does_not_ignore_later_success_reset`, `tests/test_h5_tripwire.py::test_task_warning_ledger_io_error_does_not_crash` | implemented |
| Malformed JSON field types/empty identifiers for phase, outcome, retry key, cycle ID, and timestamps cannot crash health or forge a warning | `tests/test_h5_tripwire.py::test_task_warning_malformed_ledger_fields_fail_closed`, `tests/test_h5_tripwire.py::test_task_warning_malformed_skip_cycle_id_cli_is_unknown`, `tests/test_h5_tripwire.py::test_task_warning_malformed_outcome_does_not_crash`, `tests/test_h5_tripwire.py::test_task_warning_malformed_phase_success_resets_to_unknown` | implemented |
| Malformed typed outcome values and ledger read errors fail closed without crashing health output | `tests/test_h5_tripwire.py::test_task_warning_malformed_outcome_does_not_crash`, `tests/test_h5_tripwire.py::test_task_warning_ledger_io_error_does_not_crash` | implemented |
| Evaluating the task warning is read-only and does not alter the existing cycle-progress health signal | `tests/test_h5_tripwire.py::test_task_warning_read_is_read_only_and_separate_from_global_cycle_progress` | implemented |
| The 12-start task-warning threshold remains distinct from the cycle-progress signal's own 20-cycle/8-hour thresholds | `tests/test_h5_tripwire.py::test_task_warning_read_is_read_only_and_separate_from_global_cycle_progress` | implemented |

# Rollback

The additive field can be stopped for new rows without changing old rows. Consumers must tolerate absent keys and return UNKNOWN after disable/expiry; no historical backfill is proposed. Removing a shipped field/consumer requires a separately reviewed retirement plan. Rollback of code does not erase already written key-bearing rows; retention/expiry policy must cover that residual data.

# References

- `#2088` — Proposed-H5 per-task warning design issue.
- `#2050` — global H5 tripwire and planner timeout work; separate denominator.
- `#2052` — historical rollout forecast; no per-task key contract.
- `#1899` — shared queue and nested wall-clock limits.
- `ADR-035` — cycle/task attribution context.
- Source anchors verified at `6d476b715aa5989d5036e1385f6262d0f2a70f4b` only.
