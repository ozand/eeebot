---
title: Count terminal demand cycles as futility attempts
status: proposed
date: 2026-09-05
authors: [eeebot maintainers]
related: ["#996", "#1184", "#1211", "#1328", "#1329"]
tags: [runtime, demand, futility]
---

# Status

Proposed — implemented in the change governed by #1211, pending merge and rollout.

# Context

The goal-gap futility sidecar used `attempt_count` to decide when a flat gap should stop consuming capacity. For gaps without a concrete lever surface, the counter counted only `proposed ∩ outcome: success`. Host evidence showed the resulting semantic failure: `goal-gap-5d4d5a9dc822` consumed 79 suppressed terminal cycles — 49 `existence_index_duplicate` and 30 `recent_duplicate_failure` — while `futility.json` reported `attempt_count: 0`.

Suppression is correct and is not weakened here. The counter question is separate: how many terminal attempts did this demand consume while the metric stayed flat?

# Decision

For `attempt_unit: demand_id`, `attempt_count` is the number of distinct cycles with both a proposal serving the gap and any terminal outcome after the gap's horizon. Successful, partial, failed, and suppressed terminal outcomes count once. A proposal with no outcome remains pending and does not count. Duplicate ledger rows cannot double-count because cycles are set-joined.

For `attempt_unit: lever_surface`, the existing narrower semantics remain unchanged: only integrated non-defect cycles whose changed files hit the declared surface count. Surface futility asks whether a lever was actually changed repeatedly; demand-id futility asks how much terminal capacity one demand consumed. The explicit `attempt_unit` keeps those questions distinguishable.

The three suppression reasons remain separate evidence. This decision does not change `already_done`, `existence_index_duplicate`, `recent_duplicate_failure`, or `_recent_failure_match` matching rules.

# Consequences

## What gets easier

A human reading `79 suppressed attempts` now sees `attempt_count: 79`, not zero. The existing futility threshold, demand filter, proposer surface feedback, strategist funnel, and dashboard receive the number matching their capacity question without new state or a second counter.

## What gets harder

A demand can reach the futility threshold without an integration when repeated guards terminate it. That is intentional: it stops repeatedly spending capacity on a flat gap, but operators must inspect the separate outcome reasons to choose the repair. The count alone never says whether work existed, the index matched, or a prior attempt failed.

## What does not change

Suppression strength, recent-failure matching, lever-surface counting, partial-window never-lower behavior, unavailable-window preservation, metric-improvement reset, and futility TTL are unchanged.

# Alternatives considered

- **Count successful integrations only:** rejected because it reports zero for the measured 79 terminal attempts and cannot stop the most expensive loop.
- **Count every proposal immediately:** rejected because a proposal still pending or lost before terminalization has not completed an attempt and could be double-counted during retries.
- **Add a second `suppressed_count` threshold:** rejected as unnecessary state and decision duplication; suppression reasons remain available in the ledger while `attempt_count` already owns capacity exhaustion.
- **Change suppression rules:** out of scope and explicitly rejected; 628 blocked duplicate outcomes are guards doing their job.

# Test Contract

| Claim | Test | Status |
|---|---|---|
| 49 existence-index plus 30 recent-failure terminal suppressions count as 79 | `tests/test_goal_gap_futility.py::test_suppressed_terminal_attempts_count_toward_demand_futility` | passing |
| A proposal without an outcome does not count | same test | passing |
| Lever-surface counting remains integrated-hit based | `tests/test_goal_gap_lever_surface.py` | passing |
| Partial/unavailable evidence preserves established policy | `tests/test_class_a_windows.py` | passing |

# References

#996, #1184, #1211, #1328, #1329.
