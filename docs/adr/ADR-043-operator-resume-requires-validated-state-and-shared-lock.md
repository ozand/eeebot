---
title: Operator resume requires validated state and the bridge lock
status: proposed
date: 2026-10-25
authors: [ozand, implementation writer]
related: ["#2121", "ADR-035", "#1942"]
tags: [runtime, recovery, operator, locking]
---

# ADR-043: Operator resume validates stopped state under the shared bridge lock

## Status

Proposed — design only; implementation requires independent review and this ADR does not accept ADR-035 or #1942.

## Context

Issue #2121 calls for a local explicit operator action to resume a stopped
no-plan recovery state. At source `468a95fa9d633e3d181c95fcf4a48c593ef89056`,
`no_plan_recovery.load_state()` maps absent, malformed, and invalid data to
defaults, `_save_state()` suppresses persistence exceptions, and `resume()`
clears `stopped` before delegating to a best-effort ledger append. Those APIs
serve existing runtime callers and must not be silently made stricter as a
side effect of an operator command.

`bridge._acquire_bridge_lock()` uses an exclusive, non-blocking POSIX `flock`
on `<state_dir>/bridge.lock`, held through a bridge cycle. The bridge itself
continues without exclusion when `fcntl` is unavailable, which is unacceptable
for this operator writer. `cycle_ledger.record_no_plan_recovery_transition()`
uses fail-open `append_event()` and cannot prove that audit evidence persisted.
The state JSON is atomically replaced by the existing writer, but state and
ledger are separate files: no current primitive makes them one transaction.
These observations are verified in the cited source; no live host state was
read.

## Decision

### What this IS

A local Typer command named `recovery-resume` on the existing `nanobot` app,
available through both shipped CLI entrypoints. It requires
`--runtime-state-root PATH` with no inferred default; it calls no provider,
starts no cycle, and does not expose an HTTP or dashboard mutation surface.

Before any state mutation, it acquires the exact bridge lock path and protocol:
exclusive, non-blocking `flock` on `<runtime-state-root>/bridge.lock`. Hold the
open handle through strict state read/validation, state persistence, ledger
append, and post-write verification. Busy or any lock/path/locking failure
(including unavailable `fcntl`) refuses without state mutation. The lock
coordinates only writers that honor this same lock; it is not general CAS or
protection against unrelated writers, hostile filesystem changes, or a
concurrent nonconforming process.

Read `planner/no_plan_recovery.json` directly, not through permissive
`load_state()`. Require readable UTF-8 JSON object with schema exactly
`no-plan-recovery-v1` and every current `RecoveryState` field. Retain and
round-trip unknown JSON object fields unchanged for forward compatibility.
Validate exact types for known fields (`bool` is not an integer), nonnegative
integer counters, booleans, and a list of strings for deferred verdict IDs.
Reject missing known fields, malformed or invalid UTF-8/JSON (including parser
recursion failures and duplicate keys), wrong schema, non-stopped state, and
symlinked root/lock/state/ledger paths before changing recovery state. Refuse
an existing active ledger that is not newline-terminated; do not repair or
rewrite its history. Do not include state payload or OS exception text in
output. Do not create missing state directories or defaults.

On valid stopped state, atomically persist only `stopped=false` via a
same-directory temporary file, flush/`fsync`, replace, and parent-directory
`fsync`; preserve every other known and unknown field. A failure before replace
is `refused`, with state `unchanged` and audit `not_attempted`. If replace
occurred but directory `fsync` fails, return nonzero `partial`: state is
`unverified` unless read-back verifies its value; audit is `not_attempted`.
Never roll state back.

Only after state durability is confirmed, preserve the cycle ledger's normal
append preparation: rotate/prune a stale active file and apply its existing
day-boundary cutover-marker preparation before appending. Append exactly one
`no_plan_recovery` transition (`cycle_id=operator-resume`, `family=planner`,
unchanged planner count, `state=normal`) with strict error reporting, flush/
`fsync`, and verification using the ledger reader's complete-JSONL-line
semantics. The two files are not transactional. An audit failure after state
replacement is nonzero `partial`; report audit as `verified` only if the full
appended line is proven readable, otherwise `unverified` (never imply the event
is absent). Do not truncate partial ledger lines or retry this command once
state is no longer stopped.

Emit bounded JSON with `result` (`resumed`, `partial`, `refused`),
`state_status` (`unchanged`, `verified`, `unverified`),
`state_durability` (`not_attempted`, `verified`, `unverified`), and
`audit_status` (`not_attempted`, `verified`, `unverified`). Only verified state
durability and audit permit zero-exit success. Emit no raw exception, path, or
state payload.

### What this IS NOT

Not a general recovery-store rewrite, CAS framework, transaction/outbox,
service, dashboard/HTTP endpoint, authentication change, provider call, cycle
execution, host operation, or authorization to change ADR-035/#1942 status.
It does not protect files from writers that ignore `bridge.lock`.

### Success criteria

- Only verified state durability and verified transition evidence permit
  success; all other state fields are preserved.
- Failures after replacement report uncertainty honestly and never roll state
  back; ledger is not attempted when state durability is unverified.
- Output is bounded and contains no state payload, raw exception, or path.

## Consequences

### What gets easier

Operators can explicitly resume stopped recovery and distinguish full success
from an incomplete audit write.

### What gets harder

Strict validation may require manual repair; the lock excludes only
conforming writers. State and ledger cannot be committed atomically, so a ledger
failure after replacement leaves recovery unblocked without audit and requires
reconciliation outside the command.

### What does not change

Existing recovery API callers and bridge lock behavior are unchanged. No
provider/cycle behavior, dashboard, or host configuration changes.

## Alternatives Considered

- **Reuse `resume()` directly:** rejected because permissive load and swallowed write/ledger errors cannot establish safe success.
- **Make existing helpers strict globally:** rejected because it changes runtime callers beyond #2121.
- **Roll back state after ledger failure:** rejected because partial append and second-write failures cannot make the files transactional.
- **Do nothing:** leaves no operator-facing trigger for the requested bounded capability.

## Test Contract

| Claim in Decision | Test | Currently |
|---|---|---|
| Explicit root and bounded safe JSON output/exits; both public CLI entrypoints remain usable | `tests/test_no_plan_recovery_cli.py::test_recovery_resume_requires_explicit_root_and_reports_safe_result` | passing on Windows local and CI Python 3.11/3.13; Python 3.12 failure fixed in PR #2122 follow-up |
| Valid stopped state clears only `stopped`, preserving known fields, unknown fields, and deferred IDs; exactly one transition is durable, with non-stopped repeat refused | `tests/test_no_plan_recovery_cli.py::test_recovery_resume_requires_explicit_root_and_reports_safe_result` | passing |
| Invalid state and unsafe root/lock/state/ledger paths refuse before mutation; deep JSON recursion and permission failures stay bounded | `tests/test_no_plan_recovery_cli.py::test_invalid_or_nonstopped_state_is_unchanged`; `tests/test_no_plan_recovery_cli.py::test_deeply_nested_json_refuses_without_mutation`; `tests/test_no_plan_recovery_cli.py::test_symlink_state_paths_refuse_without_mutation`; `tests/test_no_plan_recovery_cli.py::test_ledger_symlink_refuses_before_state_or_outside_file_changes`; `tests/test_no_plan_recovery_cli.py::test_permission_error_is_bounded_refusal_on_every_platform`; `tests/test_no_plan_recovery_cli.py::test_unreadable_state_refuses_without_mutation` | passing; POSIX permission test skipped on Windows and when run as root |
| Busy and unsupported lock refuse before mutation; lock covers state and ledger operation | `tests/test_no_plan_recovery_cli.py::test_resume_refuses_busy_or_unsupported_lock`; `tests/test_no_plan_recovery_cli.py::test_unsupported_lock_fails_closed`; `tests/test_no_plan_recovery_cli.py::test_real_bridge_flock_contention_refuses_without_state_change` | passing; POSIX lock test skipped on Windows |
| Replace followed by directory `fsync` failure and audit preparation/append/fsync/verification failure report truthful state/audit status; only complete readable ledger lines count as verified; normal rollover preparation is preserved | `tests/test_no_plan_recovery_cli.py::test_state_directory_fsync_failure_is_partial_and_skips_ledger`; `tests/test_no_plan_recovery_cli.py::test_rotation_archive_failure_is_partial_and_not_success`; `tests/test_no_plan_recovery_cli.py::test_cutover_marker_failure_is_partial_and_not_success`; `tests/test_no_plan_recovery_cli.py::test_ledger_failure_is_partial_and_never_rolls_back`; `tests/test_no_plan_recovery_cli.py::test_ledger_fsync_failure_reports_event_if_readback_proves_it`; `tests/test_no_plan_recovery_cli.py::test_ledger_write_that_cannot_be_verified_is_not_claimed_absent`; `tests/test_no_plan_recovery_cli.py::test_existing_unterminated_ledger_is_not_verified_as_a_new_row`; `tests/test_no_plan_recovery_cli.py::test_unterminated_ledger_line_refuses_before_state_change`; `tests/test_cycle_ledger.py::TestRotation::test_rotates_stale_active_file_to_gz`; `tests/test_no_plan_recovery_cli.py::test_ledger_rotation_and_cutover_preparation_precede_resume_transition` | passing |

## Rollback

Removing the command does not alter runtime callers or state format. It cannot
erase an append-only transition or reinstate a previously cleared stop; partial
outcomes require separate operator reconciliation.

## References

- Issue #2121 and its research record comment `6391300543`.
- `nanobot/runtime/no_plan_recovery.py` (`load_state`, `_save_state`, `resume`).
- `nanobot/runtime/bridge.py` (`_acquire_bridge_lock`, `main`).
- `nanobot/runtime/cycle_ledger.py` (`append_event`, `record_no_plan_recovery_transition`).
- ADR-035 and issue #1942 (related; unchanged and not accepted here).
