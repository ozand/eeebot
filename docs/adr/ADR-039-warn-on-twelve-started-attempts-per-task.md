---
title: Warn operators after twelve unsuccessful started attempts for one task
status: proposed
date: 2026-10-13
authors: [ozand, pi]
related: ["#2088", "#2050", "#2052", "ADR-035"]
tags: [runtime, observability, privacy, h5]
---

# Status

Proposed under #2088 and refined for the design-only follow-up #2143. This records delegated technical design choices, not personal operator acceptance, ADR acceptance, implementation authority, or rollout. Owner: ozand. Target environment: eeebot runtime and its existing operator-facing health/reporting surfaces; host rollout remains separately authorized.

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
- Count only exact `retry_key` equality. A changed self-directed first title line may create a new identity; no lineage is inferred. Planning/rest and duplicate-skip rows are not starts.
- Establish success reset only from a successful terminal outcome carrying that same exact key. A `pushed_late` success without exact key remains UNKNOWN, never assigned by time/cycle proximity.
- Emit a warning at 12; 11 does not warn when the count is known. Expose a below-12 count only when start and reset coverage is known. If 12 exact starts since the most recent known reset are evidenced, but any intervening outcome/retention gap could hide a success reset, the result is UNKNOWN rather than a warning. Exact continuous coverage for the 12-start interval can establish the threshold even if older history before that interval is incomplete; a possible unknown reset within the interval cannot.
- Treat missing, expired, malformed, or partial ledger coverage as UNKNOWN. The effective coverage horizon is configured by `CYCLE_LEDGER_RETENTION_DAYS` (default 90); do not claim the active file is physically pruned at that age.
- Extend the existing operator health/reporting surface; do not introduce another notifier/service. Public output is aggregate-only and never includes raw keys.
- Warning is informational only: no denial at 13, block, wake, prioritization, retry/handled-state mutation, or rollback.

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

Indefinite retention could improve historical coverage but increases privacy/storage exposure and is rejected. Use the existing bounded ledger-retention mechanism with `CYCLE_LEDGER_RETENTION_DAYS` defaulting to 90 as the effective evidence horizon. This does not assert that the active ledger file is age-pruned; when expired archive evidence or active-history continuity is missing, the count is UNKNOWN. Pruning is append-driven, so this is not a guarantee of physical deletion exactly at day 90.

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| Start row carries the same canonical key computed for the request before the write-ahead event; no ordering/retry behavior changes | `tests/test_cycle_ledger.py::<new start retry-key serialization test>` | not yet written |
| 11 starts do not warn and the 12th warns for one key | `tests/test_h5_tripwire.py::<new per-task threshold boundary test>` | not yet written |
| Interleaved keys remain isolated and a success resets only its own key | `tests/test_h5_tripwire.py::<new key isolation and reset test>` | not yet written |
| Planner/rest and duplicate-skip rows do not count as starts; outcome joins require exact key | `tests/test_h5_tripwire.py::<new phase and exact-join test>` | not yet written |
| Missing start key, incomplete coverage, expired evidence, or possible unknown reset (including keyless late success) yields UNKNOWN rather than zero or an inferred reset | `tests/test_h5_tripwire.py::<new incomplete-coverage and late-push test>` | not yet written |
| Fully evidenced exact-key starts can establish the 12 threshold despite incomplete earlier history; a possible reset gap within the relevant interval must remain UNKNOWN | `tests/test_h5_tripwire.py::<new threshold under partial-history test>` | not yet written |
| Operator output aggregates counts/status/coverage and never serializes raw key | `tests/test_h5_tripwire.py::<new public-output privacy test>` | not yet written |
| Warning does not block/wake/rollback or mutate retry/handled state | `tests/test_h5_tripwire.py::<new non-actioning warning test>` | not yet written |
| Global six-hour/12-cycle H5 semantics remain separate and unchanged | `tests/test_h5_tripwire.py::<existing or new global H5 regression test>` | not yet written |
| Retention uses the existing effective `CYCLE_LEDGER_RETENTION_DAYS` horizon (default 90), without claiming active-file physical pruning at that age | `tests/test_cycle_ledger.py::<new effective-retention coverage test>` | not yet written |

# Rollback

The additive field can be stopped for new rows without changing old rows. Consumers must tolerate absent keys and return UNKNOWN after disable/expiry; no historical backfill is proposed. Removing a shipped field/consumer requires a separately reviewed retirement plan. Rollback of code does not erase already written key-bearing rows; retention/expiry policy must cover that residual data.

# References

- `#2088` — Proposed-H5 per-task warning design issue.
- `#2050` — global H5 tripwire and planner timeout work; separate denominator.
- `#2052` — historical rollout forecast; no per-task key contract.
- `#1899` — shared queue and nested wall-clock limits.
- `ADR-035` — cycle/task attribution context.
- Source anchors verified at `6d476b715aa5989d5036e1385f6262d0f2a70f4b` only.
