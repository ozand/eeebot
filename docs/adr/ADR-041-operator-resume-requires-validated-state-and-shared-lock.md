---
title: Operator resume requires validated state and the bridge lock
status: proposed
date: 2026-10-25
authors: [ozand, implementation writer]
related: ["#2121", "ADR-035", "#1942"]
tags: [runtime, recovery, operator, locking]
---

# ADR-041: Operator resume validates stopped state under the shared bridge lock

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

On a valid stopped state, clear only `stopped`; preserve every other validated
field and the existing JSON schema. Persist using a same-directory temporary
file, flush and `fsync` it, atomically replace the state file, then `fsync` the
parent directory. Record exactly one transition with family `planner`, the
unchanged `consecutive_planner` count, state `normal`, and a fixed bounded
operator command cycle identifier (`operator-resume`). Append the typed JSONL
ledger event using a strict error-reporting path (not best-effort
`append_event()`); flush and `fsync` the ledger and verify the appended event.
Normal success is reported only after both persisted state and ledger event
are verified.

The two-file sequence is explicitly not transactional. If state replacement
succeeds but ledger append/verification fails, do not roll state back and do
not claim unchanged state or full success: return nonzero with a bounded
`partial` result stating `recovery_unblocked: true` and `audit_recorded: false`.
The command refuses retries after that point because the state is no longer
stopped; operator reconciliation is outside this command. If state persistence
fails before replacement, report failure and make no success claim. If a ledger
failure leaves a partial line, do not truncate/repair it here. Errors before
state replacement return nonzero and identify only a safe error category.

Emit one bounded JSON result to stdout with `result` (`resumed`, `partial`, or
`refused`), `recovery_unblocked`, and `audit_recorded`; use nonzero exit for
refusal or partial failure and zero only for fully persisted success. No raw
exception, path contents, counters, or deferred IDs are emitted. Expected
operator refusals use concise safe categories; diagnostics do not include
sensitive filesystem content.

### What this IS NOT

Not a general recovery-store rewrite, CAS framework, transaction/outbox,
service, dashboard/HTTP endpoint, authentication change, provider call, cycle
execution, host operation, or authorization to change ADR-035/#1942 status.
It does not protect files from writers that ignore `bridge.lock`.

### Success criteria

- Exactly one resume transition and only the `stopped` field changes on full
  success; all other fields remain byte-value-equivalent after JSON decoding.
- State and ledger durability/verification both precede success exit/output.
- Every validation, lock, and pre-replacement failure leaves state unmodified;
  post-replacement ledger failure is truthfully reported as partial, never
  rolled back or reported as success.
- Output and exit behavior disclose no state payload or exception/path detail.

## Consequences

### What gets easier

An operator has an explicit local recovery operation whose success means both
the state change and its audit transition were durably observed.

### What gets harder

Strict validation may require manual repair for old or extended state files.
The shared lock only excludes conforming writers. State and ledger cannot be
committed atomically, so a ledger failure after state replacement leaves
recovery unblocked and audit evidence unavailable; the CLI cannot retry that
transition safely without a separate reconciliation authority. `fsync` and
strict ledger handling add failure modes and platform constraints.

### What does not change

Existing runtime callers retain permissive `load_state()`, `_save_state()`,
and `resume()` semantics. Bridge cycle locking behavior is not altered. No
state field, transition meaning, provider/cycle behavior, dashboard, or host
configuration changes.

## Alternatives Considered

### Reuse `resume()` and trust its return

Rejected: it accepts permissively defaulted state and cannot surface swallowed
state-save or ledger failures, so the CLI could falsely report success.

### Make the two existing helpers strict globally

Rejected: changes runtime callers beyond #2121 and turns a local operator
increment into a recovery-storage rewrite.

### Roll state back when ledger append fails

Rejected: the ledger may have partially appended, and rollback creates a
second state write with its own failure window; it cannot establish an atomic
state/ledger outcome or safely overwrite concurrent/nonconforming changes.

### Do nothing

Leaves the current plain API without an operator-facing trigger; the explicit
bounded local command is the issue's requested capability.

## Test Contract

| Claim in Decision | Test | Currently |
|---|---|---|
| Explicit root and bounded safe JSON output/exits; both public CLI entrypoints remain usable | `tests/test_cli.py::test_recovery_resume_requires_explicit_root_and_reports_safe_result` | not yet written |
| Valid stopped state clears only `stopped`, preserving known fields, unknown fields, and deferred IDs; exactly one transition is durable | `tests/test_no_plan_recovery_cli.py::test_resume_preserves_state_and_records_one_transition` | not yet written |
| Missing/unreadable/invalid UTF-8/malformed/wrong-schema/missing-field/wrong-type/negative-count/non-stopped state refuses without state mutation | `tests/test_no_plan_recovery_cli.py::test_invalid_or_nonstopped_state_is_unchanged` | not yet written |
| Busy and unsupported lock refuse before mutation; lock covers state and ledger operation | `tests/test_no_plan_recovery_cli.py::test_resume_refuses_busy_or_unsupported_lock` | not yet written |
| State write failure and ledger append/fsync/verification failure never report full success; post-replace ledger failure is explicitly partial and does not roll back state | `tests/test_no_plan_recovery_cli.py::test_write_and_ledger_failures_are_truthful` | not yet written |

## Rollback

The command and its tests can be removed without changing existing runtime
callers or state format. Once a successful resume has written its ledger event,
that audit event is append-only; rollback removes future command availability
but does not erase the historical event or reinstate `stopped`. A partial
state-success/ledger-failure outcome requires operator reconciliation outside
this command; no automatic reversal is safe.

## References

- Issue #2121 and its research record comment `6391300543`.
- `nanobot/runtime/no_plan_recovery.py` (`load_state`, `_save_state`, `resume`).
- `nanobot/runtime/bridge.py` (`_acquire_bridge_lock`, `main`).
- `nanobot/runtime/cycle_ledger.py` (`append_event`, `record_no_plan_recovery_transition`).
- ADR-035 and issue #1942 (related; unchanged and not accepted here).
