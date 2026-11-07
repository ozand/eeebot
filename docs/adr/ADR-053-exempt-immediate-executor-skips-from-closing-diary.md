---
title: Exempt immediate executor skips from the closing diary line
status: proposed
date: 2026-11-06
authors: [delegated delivery]
related: ["#2167", "#2038", "ADR-030", "ADR-028"]
tags: [diary, executor, skip]
---

# Status

Proposed, not binding. This is a narrow proposed partial override of ADR-030 rule 2 for executor-discovered already-done or not-applicable skips only; ADR-030 remains accepted for all other decisions and outcomes. The owner delegated the technical selection recorded in #2167; this is not personal operator acceptance. The instruction draft preceded discovery of the ADR conflict and is not retroactively approved.

# Context

ADR-030 rule 2 requires an insight before a reporting cycle ends and distinguishes it from the bridge's opening intent; it gives already-done as a useful insight. Existing `OPERATING.md` also says to stop immediately with no more tool calls when the executor discovers already-done or inapplicable work. These instructions conflict. The bridge attempts an opening entry before execution, but it records the task, not the eventual insight, and can fail. A pre-start duplicate has no executor response; that is separate from an executor-discovered skip.

# Decision

For an executor-discovered already-done or not-applicable skip, report `skipped` immediately without a closing diary edit, bookkeeping commit or further tool calls. This is an explicit exception to ADR-030 rule 2's closing insight, not a replacement insight. All other executor outcomes retain the closing diary obligation; all other ADR-030 decisions remain unchanged. The bridge's opening attempt is not an outcome record and is not guaranteed to succeed. No new bridge diary writer, ledger contract, runtime behavior, host change or provider call is authorized.

# Consequences

### Easier

The executor has one unambiguous stopping rule and need not make a diary tool call solely to report that no work is needed.

### Harder

The next reader loses a potentially useful diary insight about why that work was already done or inapplicable. The opening intent and existing ledger are not equivalent to that insight. The exception trades that information for immediate termination; no later recovery of the insight is claimed.

### Unchanged

Other reporting outcomes still require the executor's closing line. ADR-030's remaining decisions and accepted history stay intact; pre-start skip and ledger behavior are not changed.

# Alternatives considered

- Keep the closing insight for executor skips, then stop: preserves ADR-030 in full but requires another diary tool call; rejected for this narrowly delegated choice of immediate stop.
- Add a harness post-skip diary writer: rejected as unnecessary runtime and persistence scope; it would also need an independently evidenced insight rather than inventing one.
- Treat the opening line or ledger as equivalent insight: rejected because neither establishes the executor's learned conclusion.

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| Proposed and accepted instruction branches enforce their respective skip and diary rules, including negative fixtures | `tests/test_operating_md.py::test_executor_skip_diary_exception` | status-aware test; operative text remains baseline while proposed |
| Pre-start duplicate ends the cycle without another attempt | `tests/test_bridge_bulk_skip.py::TestSingleCandidatePerCycle::test_duplicate_plan_ends_run_no_second_attempt` | existing test with ADR citation |
| Opening diary write call precedes branch setup | `tests/test_diary_open_entry.py::test_diary_write_call_site_precedes_cycle_branch_setup_in_source` | existing source-order test with ADR citation |
| Prompt prose names only executor tools | `tests/test_prompt_ontology.py::test_prompt_prose_names_only_executor_registry_tools` | existing test with ADR citation |

# References

- Original conflict and owner-directed scope reconciliation: https://github.com/ozand/eeebot/issues/2167#issuecomment-6698308287
- ADR-030 rule 2 and its already-done example; ADR-028 opening diary boundary.
- `OPERATING.md`; `nanobot/runtime/bridge.py` opening entry and pre-start dedup paths; prior PR #2169 review is retained as history.
