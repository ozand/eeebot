---
title: Two operator documents, one root each, one resolver per document, and the agent that chooses reads both
status: proposed
date: 2026-09-24
authors: [ozand, architect]
related: ["#1699", "#1665", "#944", "#1735", "#1854", "#1920", "ADR-020", "ADR-022", "ADR-032"]
tags: [charter, priorities, demand, goal-review, provenance]
---

# Status

Proposed 2026-09-24. Becomes accepted when its named tests land on `main` and the
operator flips the status, per `docs/adr/README.md`.

# Context

The operator speaks to the loop through two documents, and #944 made them two on
purpose:

```text
charter      <release root>/goals.md                 immutable to the loop; changes by PR + deploy
priorities   state/goals/goal_text.json  (0600)      operator's ordered targets + "Completed (do not repeat)"; no charter text
derived      state/goals/derived_priorities.json     minted by goal_review; the loop's own priorities
```

A full census of every reader and writer — 32 call sites in `nanobot/` and
`scripts/`, each checked on the host — is in #1699 (2026-09-24). What it
establishes:

**Exactly one live reader of the operator priorities exists in the loop**, and it
works by accident. `demand._priority_items` first asks the *instance* repository
for `goals.md`; the instance has none, so it falls through to `goal_text.json`.
If a `goals.md` ever appears at the instance root, step 1 succeeds with a text
that has no priority section and every operator priority leaves demand, with no
error and no ledger row.

**Neither the executor nor the proposer sees the operator priorities.**

| reader | reads | operator priorities visible |
|---|---|---|
| executor mission block, `bridge.py:3602–3628` | release charter + derived | no |
| executor system prompt, `context.py:229, 600` | release charter | no |
| proposer, `llm_proposer._load_goal_text:499` | release charter + derived; the `goal_text.json` branch is shadowed | no |
| role prompts (proposer, planner), `role_prompt.py:255` | release charter | no |
| strategist inputs, `strategist_inputs.py:121` | release charter; `goal_text.json` shadowed | no |
| demand priorities, `demand._priority_items:745–780` | `goal_text.json` via instance-miss fallback | **yes, by accident** |
| demand charter view, `demand._charter_as_loop_sees_it:884–893` | `goal_text.json` via the same fallback, returned *as the charter* | yes, mislabelled |
| demand artifact gaps, `demand._artifact_gap_items:1812` | instance `goals.md` | dead, returns `[]` every call |
| goal review, `goal_review._load_goal_data` | `goal_text.json`, because its caller omitted `release_root` — fixed by #1920 | — |

The live executor prompt confirms it: its only mention of priorities is *"This
task is not an operator priority; priorities are handled by the proposer"* — and
the proposer cannot see them either.

**One writer.** Only the operator writes `goal_text.json`, by hand on the host.
`goal_review` writes `derived_priorities.json` since #860; the deploy-time seeding
was removed in #1640/#1647; `goal_text_utils.filter_completed_priorities_from_goal_text`
filters in memory and writes nothing.

**Dead paths.** `state_dir/goals.md` (`demand.py:758, 893`) and
`RELEASE_ROOT/host/eeepc/etc/goal_text.json` (`bridge.py:3612`) exist on no host.

**Provenance is lost at the merge.** `merged_goal_text` numbers derived priorities
into the operator's list, so no downstream reader can tell who asked (#1665).

**Today every operator priority is already done.** All eight entries under
"Current priority targets" (11, 12, 13, 16–20) are in `demand/completed.json` and
filtered out before any reader sees them. The operator's channel is not only
misrouted; it is currently empty in effect.

# Decision

### 1. Two documents, by design

The charter says *what the loop is for*; the priority list says *what the
operator wants next*. The charter changes by reviewed PR and deploy; priorities
change when the operator edits state. They stay two files. Neither is ever read
from the instance repository.

### 2. One root per document, one resolver per document

- Charter: `<release root>/goals.md`.
- Operator priorities: `state/goals/goal_text.json`.
- Derived priorities: `state/goals/derived_priorities.json`.

Every reader obtains each document through its one resolver. No reader
constructs a path to any of them. The instance-repo lookups (`demand.py:745, 884,
1812`), the `state_dir/goals.md` paths, and `RELEASE_ROOT/host/eeepc/etc/goal_text.json`
are deleted, not kept as fallbacks. `demand._charter_as_loop_sees_it` returns the
charter, never the priority text relabelled as one.

`_artifact_gap_items` keeps its own check of `surfaces/*.py` in the instance
repository; only its charter lookup moves to the resolver.

### 3. Absent is a state, and each reader says what it does with it

A resolver returns *text*, *absent*, or *unreadable*. What a reader does with the
last two is fixed here, not left to the reader:

| reader | charter absent / unreadable | operator priorities absent / unreadable |
|---|---|---|
| executor prompt and mission | cycle does not start; reason recorded | cycle runs; block says "operator priorities unavailable"; status recorded |
| proposer, planner, strategist | role does not run; reason recorded | role runs; input marked unavailable, never treated as "no priorities" |
| demand | `artifact-gap` emits nothing, with reader status `unavailable` | `priority-*` emits nothing, with reader status `unavailable` |
| goal review | review skipped; reason recorded | review runs on charter + derived |
| dashboard status | shows `unavailable` for that document | shows `unavailable` — never the file's text |

A missing charter stops work because nothing downstream is meaningful without it;
missing priorities never do, because the operator may legitimately have none.

"Stops" means work that chooses, proposes or executes tasks. Diagnostics, the
recording of why the cycle did not start, health reporting, deploy rollback and
the publisher keep running — a missing charter must be the loudest thing on the
dashboard, not a reason for the dashboard to go quiet.

The operator priority list has four distinguishable states, and every reader
keeps them apart:

| state | meaning | what the chooser sees |
|---|---|---|
| `present` | open priorities exist | the list |
| `all_completed` | every listed priority is in the Completed set | one line saying so |
| `empty` | the document is valid and lists none | one line saying so |
| `unavailable` | absent or unreadable | one line saying the operator's intent could not be read — never that the operator wants nothing |

The document has a size cap enforced by its resolver; over the cap it is
`unavailable` with reason `oversize`, never silently truncated. No status
surface, log line or error message renders the document's text, including the
Completed list.

### 4. Provenance survives every hop

Operator and derived priorities are never merged into one numbered list. Each
carries `source: operator | derived` through demand, ranking and the prompt. This
supersedes the merge in `merged_goal_text` (#1665). Filtering of completed
priorities applies to each list separately and keeps the label.

### 5. The executor, the proposer and the planner see the operator priorities

Every role that chooses or proposes work — executor, proposer, planning session —
receives the charter, the operator priorities with their Completed list, and the
derived priorities, each under its own heading, operator first. This lands
**before** ADR-032's choosing change (#1854).

The block states its own status in the prompt: the order of the list conveys the
operator's intent, not an instruction to execute the first item. The executor
chooses, and names which priority — if any — its choice serves and why. The
Completed list is a constraint against repetition, never a source of new work.
Nothing in the prompt assembly may pick an item for the executor.

When every operator priority is completed, the block says so in one line rather
than disappearing, so the absence of operator intent is itself visible to the
chooser and on the dashboard.

# Consequences

- **#1854 gains a prerequisite.** Removing "priorities are handled by the
  proposer" while no role can see the priorities would hand the agent a free
  choice with the operator's intent withheld. Order: this record's rule 5, then
  #1854.
- **The operator learns his channel is empty.** With rule 5's one-line "all
  completed", the state that has held silently — every operator priority done,
  the loop working only from charter and its own derived list — becomes visible.
  Setting new priorities is the operator's decision, not this record's.
- **Prompt cost is new and bounded.** The priority blocks are their own section
  with their own limit of 3,000 characters, counted inside the overall system
  prompt limit (`MAX_SYSTEM_PROMPT_CHARS`), not inside the pooled release budget:
  measured on 2026-09-24 that pool had about 129 characters free, and a block
  placed there would have displaced `OPERATING.md` (#1940). The Completed list is
  shown by titles only, including when every operator priority is completed.
  Derived priorities are shown in compact form — number, label, vector and
  source; their bodies are not rendered. Over the limit the block is `unavailable` with reason
  `oversize`, never truncated (rule 3). Measure its reach the way #1856 measures
  every block, and test that the overall limit holds and that it cannot displace
  the charter or the mandatory instructions.
- **The goal review's input changed with #1920**, before this record. Its before/after
  comparison is recorded there.
- **`artifact-gap` becomes reachable and today still emits nothing**, for the
  right reason: the instance already has a surface.
- **The accident path closes.** A `goals.md` in the instance repo no longer changes
  what any reader sees.

# Alternatives considered

**One document.** Fold the priorities into `goals.md`. Rejected: every priority
edit would become a PR and a deploy, and the operator's one fast channel into the
loop would be lost.

**Keep the fallback chains, document the order.** Rejected: the census shows the
chains agree only because a file is *absent*; documenting that makes the accident
official.

**Operator priorities reach the executor only through demand, as now.** Rejected
by ADR-032: demand becomes an input to the agent's choice, not a dispatcher.

**Fix `_priority_items` alone, now.** Rejected: pointing its step 1 at the release
charter would make step 1 succeed and drop every operator priority from demand —
the trap this record exists to close. The readers change together or not at all.

## Test Contract

| Decision claim | Test | Currently |
|---|---|---|
| A `goals.md` in the instance repo changes no reader's output (all readers in the #1699 census) | `tests/test_operator_documents.py::test_instance_goals_md_is_ignored_by_every_reader` | not written |
| Every charter reader resolves from the release root only; every priority reader from state only | `tests/test_operator_documents.py::test_each_document_resolves_from_its_one_root` | not written |
| Each of the three documents, absent and unreadable separately, produces the rule-3 behaviour for every reader; absence of one never silences the others | `tests/test_operator_documents.py::test_absent_and_unreadable_follow_the_reader_table` | not written |
| Dashboard status for `goal_text.json` shows availability, never its text | `tests/test_operator_documents.py::test_status_surface_never_renders_priority_text` | not written |
| Operator and derived priorities keep `source` through demand, ranking and the prompt; completed filtering is per list | `tests/test_operator_documents.py::test_priority_provenance_survives_end_to_end` | not written |
| Proposer end to end: with operator priorities present and not completed, a proposal can cite one; with all completed, it sees the one-line notice | `tests/test_operator_documents.py::test_proposer_sees_operator_priorities_end_to_end` | not written |
| Executor and planner prompts carry the three blocks under their headings, with the "intent, not instruction" line, within budget, without displacing charter or mandatory instructions | `tests/test_operator_documents.py::test_prompt_blocks_present_and_bounded` | not written |
| `_charter_as_loop_sees_it` never returns the priority text as the charter | `tests/test_operator_documents.py::test_charter_view_is_the_charter` | not written |
| A missing charter stops choosing/proposing/executing but not diagnostics, reason recording, health or publishing | `tests/test_operator_documents.py::test_missing_charter_keeps_diagnostics_running` | not written |
| The four priority states are distinguishable in every reader; `unavailable` is never rendered as "no priorities" | `tests/test_operator_documents.py::test_four_priority_states_are_distinct` | not written |
| No prompt assembly selects an item for the executor; the first priority is never turned into the task | `tests/test_operator_documents.py::test_prompt_never_picks_a_priority_for_the_executor` | not written |
| Every consumer of the former merged numbered text (completed filtering, ranking, number references) keeps `source` after migration | `tests/test_operator_documents.py::test_merged_text_consumers_migrated_with_source` | not written |
| Empty, corrupt and oversize priority documents produce the rule-3 states; no private text reaches status, logs or error messages | `tests/test_operator_documents.py::test_boundary_documents_leak_no_text` | not written |

# References

#1699 (full census, 32 call sites, host checks); #1665 (provenance lost at the
merge); #1920 (goal review caller); #944; #1735; #1854; ADR-020 rule 3; ADR-022;
ADR-032 rules 1 and 5; `nanobot/runtime/goal_review.py`, `demand.py`, `bridge.py`,
`llm_proposer.py`, `role_prompt.py`, `context.py`, `strategist_inputs.py`,
`state.py`.
