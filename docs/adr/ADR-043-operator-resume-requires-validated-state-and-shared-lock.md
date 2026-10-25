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
Reject missing known fields, malformed or invalid UTF-8/JSON, wrong schema, and
non-stopped state. Do not include state payload or OS exception text in output.
Do not create missing state directories or defaults.

On valid stopped state, atomically persist only `stopped=false` via a
same-directory temporary file, flush/`fsync`, replace, and parent-directory
`fsync`; preserve every other known and unknown field. A failure before replace
is `refused`, with state `unchanged` and audit `not_attempted`. If replace
occurred but directory `fsync` fails, return nonzero `partial`: state is
`unverified` unless read-back verifies its value; audit is `not_attempted`.
Never roll state back.

Only after state durability is confirmed, append exactly one
`no_plan_recovery` transition (`cycle_id=operator-resume`, `family=planner`,
unchanged planner count, `state=normal`) with strict error reporting, flush/
`fsync`, and read-back verification. The two files are not transactional. An
audit failure after state replacement is nonzero `partial`; report audit as
`verified` only if read-back proves it, otherwise `unverified` (never imply the
event is absent). Do not truncate partial ledger lines or retry this command
once state is no longer stopped.

Emit bounded JSON with `result` (`resumed`, `partial`, `refused`),
`state_status` (`unchanged`, `verified`, `unverified`), and `audit_status`
(`not_attempted`, `verified`, `unverified`). Only verified state and audit
permit zero-exit success. Emit no raw exception, path, or state payload.

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
| Explicit root and bounded safe JSON output/exits; both public CLI entrypoints remain usable | `tests/test_cli.py::test_recovery_resume_requires_explicit_root_and_reports_safe_result` | not yet written |
| Valid stopped state clears only `stopped`, preserving known fields, unknown fields, and deferred IDs; exactly one transition is durable | `tests/test_no_plan_recovery_cli.py::test_resume_preserves_state_and_records_one_transition` | not yet written |
| Missing/unreadable/invalid UTF-8/malformed/wrong-schema/missing-field/wrong-type/negative-count/non-stopped state refuses without state mutation | `tests/test_no_plan_recovery_cli.py::test_invalid_or_nonstopped_state_is_unchanged` | not yet written |
| Busy and unsupported lock refuse before mutation; lock covers state and ledger operation | `tests/test_no_plan_recovery_cli.py::test_resume_refuses_busy_or_unsupported_lock` | not yet written |
| Replace followed by parent-directory `fsync` failure reports state durability unverified and ledger not attempted; ledger append/fsync/read-back failures report audit verified only when proved, otherwise unverified; neither case rolls back state or reports success | `tests/test_no_plan_recovery_cli.py::test_post_replace_and_ledger_failures_report_truthful_phase_status` | not yet written |

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
