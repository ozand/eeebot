---
title: Preserve suppression reasons separately in the scorecard
status: proposed
date: 2026-09-05
authors: [eeebot maintainers]
related: ["#1211", "#1215", "#1218", "#1328", "#1329"]
tags: [runtime, scorecard, observability]
---

# Status

Proposed — implemented for #1329 pending merge and rollout.

# Context

Host measurement through 2026-09-05 found 628 `skipped-duplicate` outcomes split into three different causes: 163 `already_done`, 195 `existence_index_duplicate`, and 270 `recent_duplicate_failure`. Their demand-linked counts were 1, 158, and 194. The existing scorecard preserved only `skips_by_class={"skipped-duplicate": 628}`, which erased the distinction everywhere consuming the current snapshot.

The upstream investigations show different closures. `already_done` is a coordinator-era fossil: all 163 rows occurred from 2026-07-12 through 2026-07-15, 141 without a proposer row, and none have occurred since the demand-vetted bypass landed. `existence_index_duplicate` consumes proposal title/path plus the current script and evidence-backed ledger-title index; #1218 already retired 973 poisoned titles, but exact post-release reason counts remain the way to verify it. `recent_duplicate_failure` consumes proposal title/path plus recent failed results; its possible pre-LLM cooling is #1328 and is deliberately held until #1332's futility change is observed.

# Decision

Keep the existing aggregate `skips_by_class` for compatibility and add `loop.skips_by_reason`, counted from the ledger outcome `reason` without normalizing the vocabulary. Missing reasons are labelled `unknown`, never folded into one of the three known causes.

This is reporting only. It does not add suppression, change matching, cool a demand, modify `_recent_failure_match`, or alter the existence index. It is the cheapest safe increment while the preceding futility rollout is being observed.

# Consequences

## What gets easier

Each upstream closure can be measured independently from the scorecard snapshot. A decline in one reason cannot be incorrectly credited to another mechanism, and dormant `already_done` history no longer inflates the apparent current cost of existence or failure suppression.

## What gets harder

Consumers that want causal detail must read one additional map. The reason vocabulary remains an executable contract: new reasons must be intentionally interpreted by any consumer that enumerates them, while unknown values remain visible.

## What does not change

`skips_by_class`, repeat-failure rate, wasted-attempt accounting, suppression decisions, and all three ledger reason strings remain unchanged.

# Alternatives considered

- **Implement another upstream suppressor now:** rejected because #1332 has just changed futility suppression for up to three of five live gaps; adjacent suppression increases cannot be causally separated.
- **Remove the dormant `already_done` branch:** rejected in this increment because exact replay/non-demand compatibility callers still exist in code even though live autonomous cost is zero.
- **Combine all duplicate reasons:** rejected because the three decisions consume different evidence and need different closures.

# Test Contract

| Claim | Test | Status |
|---|---|---|
| Three skipped-duplicate reasons remain separately countable while aggregate compatibility remains | `tests/test_scorecard.py::TestLoopSection::test_only_recent_duplicate_failure_skips_count_as_repeat_failures` | passing |
| Only recent-failure skips feed repeat-failure rate | same test | passing |

# References

#1211, #1215, #1218, #1328, #1329.
