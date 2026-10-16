---
title: Warn operators after twelve unsuccessful started attempts for one task
status: proposed
date: 2026-10-13
authors: [ozand, pi]
related: ["#2088", "#2050", "#2052", "ADR-035"]
tags: [runtime, observability, privacy, h5]
---

# Status

Proposed under #2088 for design review only. No owner acceptance of the persisted start-event contract, retention policy, implementation, or rollout is implied. Owner: ozand. Target environment: eeebot runtime and its operator-facing health/forecast surfaces; host rollout remains separately authorized.

# Context

Issue #2050 defines a global tripwire: zero successes in six hours OR 12 consecutive started cycles without success, producing an immediate operator signal with no automatic rollback. A distinct user-approved H5 request is a per-task warning after 12 consecutive started attempts for one task without success. Cycle count and task-attempt count are different denominators and must not be conflated.

**Source-verified** against immutable source SHA `6d476b715aa5989d5036e1385f6262d0f2a70f4b`: bridge builds a stable cross-cycle `retry_key` before `record_cycle_started` (`nanobot/runtime/bridge.py:4488-4493, 4511-4527`). The write-ahead `started` row currently includes `cycle_id` and `request_id`, not `retry_key` (`nanobot/runtime/cycle_ledger.py:281-302`). The terminal outcome may include `retry_key`, too late to count an attempt that has no terminal row (`cycle_ledger.py:693-699`). This source check supports the design premise; it does not prove the same code is installed on the current host.

Previously host-reported bounded samples had missing task classification/identifiers in some rows. Such history cannot be safely assigned to a task by nearest timestamp. It must remain UNKNOWN/insufficient coverage.

# Decision

**Proposed:** define a task-scoped H5 warning by counting consecutive `started` attempts that carry the same canonical `retry_key` and have no later successful terminal outcome for that key. Emit an operator-facing warning at 12. The warning is informational only: it does not block planning/execution, wake or prioritize work, alter retry/handled state, or trigger rollback.

This is a proposed persisted-data contract, not accepted implementation authority. Before implementation, the accountable owner must accept the field semantics, key handling, retention/expiry policy, and output boundary.

## What this IS

- An additive optional `retry_key` field on the cycle `started` event, written from the already-computed request key without changing event ordering or retry behavior.
- A per-key count of started attempts since the most recent successful outcome for that key; repeated planning/rest sessions and duplicate-skip rows do not count as starts.
- Missing or unjoinable historical keys yield `UNKNOWN`/insufficient coverage, never zero and never a nearest-time assignment.
- The public/operator presentation exposes aggregate count, threshold state, and coverage only; it never outputs the raw key.

## What this IS NOT

- A replacement for #2050/#2052's global six-hour / 12-cycle tripwire.
- A planner gate, retry-policy change, queue wakeup, task scheduler, or automatic rollback.
- A provider-failure classifier. `executor_llm_error` or any other outcome flag is not itself proof of a provider call failure.
- A mandate to retain raw task keys indefinitely or to backfill historical attempts.

## Success criteria

- Exactly 11 consecutive starts for one key do not warn; the 12th does.
- Starts for different keys never combine; a success for a key resets only that key's streak.
- Duplicate skips and planner sessions do not increment the started-attempt count.
- Missing historical keys, incomplete archive coverage, or ambiguous joins produce UNKNOWN, not a zero/healthy state.
- Public output contains only aggregate status/count/coverage, with tests proving raw-key non-disclosure.
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

Unresolved owner choice. Indefinite retention improves exact streak coverage but increases privacy/storage exposure. Bounded retention limits exposure but makes older streaks UNKNOWN after expiry. No final lifetime or expiry duration is selected here.

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| Start row carries the same canonical key computed for the request before the write-ahead event; no ordering/retry behavior changes | `tests/test_cycle_ledger.py::<new start retry-key serialization test>` | not yet written |
| 11 starts do not warn and the 12th warns for one key | `tests/test_h5_tripwire.py::<new per-task threshold boundary test>` | not yet written |
| Interleaved keys remain isolated and a success resets only its own key | `tests/test_h5_tripwire.py::<new key isolation and reset test>` | not yet written |
| Planner/rest and duplicate-skip rows do not count as starts; outcome joins require exact key | `tests/test_h5_tripwire.py::<new phase and exact-join test>` | not yet written |
| Missing key, incomplete history, or expired history yields UNKNOWN rather than zero | `tests/test_h5_tripwire.py::<new incomplete-coverage test>` | not yet written |
| Operator output aggregates counts/status/coverage and never serializes raw key | `tests/test_h5_tripwire.py::<new public-output privacy test>` | not yet written |
| Warning does not block/wake/rollback or mutate retry/handled state | `tests/test_h5_tripwire.py::<new non-actioning warning test>` | not yet written |
| Global six-hour/12-cycle H5 semantics remain separate and unchanged | `tests/test_h5_tripwire.py::<existing or new global H5 regression test>` | not yet written |

# Rollback

The additive field can be stopped for new rows without changing old rows. Consumers must tolerate absent keys and return UNKNOWN after disable/expiry; no historical backfill is proposed. Removing a shipped field/consumer requires a separately reviewed retirement plan. Rollback of code does not erase already written key-bearing rows; retention/expiry policy must cover that residual data.

# References

- `#2088` — Proposed-H5 per-task warning design issue.
- `#2050` — global H5 tripwire and planner timeout work; separate denominator.
- `#2052` — historical rollout forecast; no per-task key contract.
- `#1899` — shared queue and nested wall-clock limits.
- `ADR-035` — cycle/task attribution context.
- Source anchors verified at `6d476b715aa5989d5036e1385f6262d0f2a70f4b` only.
