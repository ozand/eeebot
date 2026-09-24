---
title: A cycle is a HADI loop, and an unknown is a measurement to schedule
status: accepted
date: 2026-09-21
authors: [ozand, architect]
related:
  - ADR-007
  - ADR-009
  - ADR-020
  - ADR-026
  - ADR-028
  - "#1844"
  - "#1847"
tags: [loop, epistemics, diary, hypothesis, measurement]
---

## Status

Accepted 2026-09-21, on the operator's decision. Rule 1 is quoted from him
directly; the rest is the architecture that rule implies, written against
measurements taken the same day.

## Context

Two questions arrived together and turned out to be one question.

The first was narrow. The day diary shipped (ADR-028) and is read by 84% of
cycles, 14 of 16 as their first tool call — seven times the reach of the
resident skills catalogue, seventy times the reach of `search_memory`. What it
carries is `Implement and commit: <task_title>`, written by the bridge. The
executor has no instruction to write to it and never does. So the most
reachable channel in the system carries the one thing every cycle already has:
its own assignment (#1844).

Deciding what the executor should write raised three sub-questions — when to
write, what to write, and whether a second tool call is affordable against a
median of 9 iterations — and there was no data to answer any of them.

The second question was the answer to the first. Asked to decide, the operator
declined to decide in the abstract:

> Не попробуем — не узнаем. Если ты задаёшься вопросом «а нужно ли», значит у
> нас не хватает данных и мы должны эти данные собрать.

And named the shape the loop's own record should take: **hypothesis, action,
data, insight**, each cycle beginning where the last one's insight ended.

Measuring the system against that shape found every container already built and
the sequence not closed:

| letter | where it lives | state on 2026-09-21 |
|---|---|---|
| **H** | `proposed.expected_outcome_claim` | written on 15 of 46 cycles (33%); read only by the narrator as a title fallback and by an offline replay script. Never compared to the outcome. |
| **H** | `state/hypotheses/lifecycle.json` | 153 entries — but 136 of them are task titles (`Implement and commit: …`), not claims. A separate, genuine population of 17 carries real causal hypotheses and real verdicts. |
| **A** | the cycle | fully recorded. The one letter that works. |
| **D** | `outcome` row | answers *did the cycle complete and did the gate accept*. Carries no field that answers the claim. Keys: `branch, change_shape, change_tier, cycle_id, files_changed, lesson_candidate, outcome, phase, prompt_fit_rung, real_result, reason, ts, verdict, verdict_reason`. |
| **I** | `lessons/`, reflections | exist, and are not keyed to any hypothesis. |

The verdict machinery (#878, `hypothesis_verdict.py`) is well built and has the
right trust boundary — a verdict may come only from harness-measured sidecars,
never from anything the instance wrote as prose. Its two sources are in
different health: `demand/completed.json` is alive (543 of 905 entries
confirmed), while `heldout/microbench.json` does not exist and `state/heldout/`
has not been written since 17 July. The dead one was the **causal** source. What
remains answers "was this used", which cannot separate *the belief was wrong*
from *nobody got round to it*.

Across 153 hypotheses, `refuted` has been returned **zero** times. Part of that
is youth — `CONFIRM_WINDOW_DAYS = 14` and the oldest verdict is 13 days old —
and part of it is that the only source that can refute on measurement is gone.
Either way it is a fact about the loop, not about the world.

And the degenerate hypotheses say why the sequence never closed. A live example:

> `scripts/check_readonly_streak.py exits 0 when invoked without command-line arguments`

That is an acceptance criterion wearing a hypothesis's clothes. It is true by
construction of the change that accompanies it; no data could have refuted it;
nothing is learned when it holds. The same string — the task title — is written
into the demand queue, the proposal, the diary and the hypothesis store, and is
called a task, an intent, and a belief in turn. One string wearing four hats is
why three of the four hats are empty.

## Decision

### 1. An unknown is a measurement to schedule, not a question to hold

When the value of a change is unknown, the change ships behind a counter and the
unknown becomes an observation. Always convert "should we?" into "what would we
see if we did?" — an architect who cannot answer a design question from data
owes a measurement, not a deferral (#1844).

This is not permission to ship carelessly. It binds two ways:

- **The counter is designed before the feature**, and the feature is not done
  until the counter reads. ADR-028 rule 5 already shipped this way — the diary's
  read instruction went out with its read-rate counter, which is why the 84%
  above is a number and not a guess. That precedent is now the rule.
- **A shipped experiment is reversible.** Prefer a change that can be withdrawn
  on its own evidence over one that must be argued about first. Withdrawal on a
  measurement is a success of this rule, never a failure of the change.

What this rule does not license: manufacturing the event it wants to observe.
A detector is never tested by causing the failure it detects — that poisons the
store every later measurement reads. Ship, wait, read.

### 2. A cycle records hypothesis, action, data, insight

The cycle already performs the action and already records the data. What is
missing is a stated belief at the start and a stated lesson at the end, both in
the cycle's own words, and the day diary is where they go (#1844):

- **H — at cycle open.** The bridge's mechanical entry stays: it is the anchor
  that survives a cycle dying on its LLM call, committed and pushed at a clean
  `main` boundary (ADR-028 rules 2-3). Beside it, the executor states what it
  expects to become true.
- **A — the cycle.** Unchanged. Already recorded.
- **D — the outcome.** Unchanged. Already recorded, and deliberately harness-owned:
  the instance states the claim, the harness states what happened.
- **I — before the cycle reports.** One line the next cycle could not have
  derived from the ledger: what was tried, what actually happened, what that
  implies for the next attempt.

Never write the insight at open and never write the hypothesis at close. An
entry written at open can only restate the assignment; an entry written only at
close is lost by every cycle that dies late. Both ends are load-bearing and they
carry different cargo.

### 3. A hypothesis names what would make it wrong

Always state a hypothesis so that some observation could refute it. A claim that
the change makes true by construction — "the script exits 0", "the file exists",
"the test passes" — is an acceptance criterion, and belongs in the acceptance
criteria where it is already recorded. Prefer a claim about a consequence the
change does not directly produce: a rate that should fall, a repetition that
should stop, a cost that should drop.

Never let a task title stand as a hypothesis. A title says what will be done; a
hypothesis says what will then be true that is not true now, and how we would
know if it were not.

The test for whether this rule is being followed is not lexical and cannot be
linted. It is the next rule.

### 4. Refutation must be reachable, and its absence is a defect in the loop

A loop that has never refuted anything is not running an experiment, whatever
its vocabulary says. Always keep at least one source of verdict that can say
*no* on a measurement rather than on an absence, and treat a long run of zero
refutations as an instrument fault to diagnose — never as evidence that the
system has been right (#1847).

Never read "unconfirmed" as "refuted" without a stated window, and never read
"used" as "true": usage answers whether something was reached, not whether the
belief behind it held. ADR-009 already separates unverdictable from undecided in
the reporting; this rule says the separation must also exist upstream, in what
the sources can physically distinguish.

### 5. One meaning, one field

Never let the same string serve as demand item, task title, diary intent and
hypothesis at once. Each store either carries its own text or carries a
reference to the one place the text lives (ADR-022's rule, applied to the
loop's records rather than to its prompt).

Where a store cannot yet be given its own text, it carries the reference and
says so, rather than copying a title and renaming it. A copied title reads as
content to every consumer downstream, which is how a hypothesis store came to
hold 136 task titles and how a diary came to promise intent and deliver an
assignment.

## Consequences

- #1844 is unblocked and ships as an experiment: the executor gets a write
  instruction, the fitness counter grows a `diary_written` column beside
  `diary_read`, and the decision to keep or withdraw is taken on the numbers
  after a run of days, not before.
- The 84% read rate becomes a quantity to protect. A cycle asked to write may
  stop reading; that trade going the wrong way is a reason to withdraw.
- `expected_outcome_claim` gains a reader or loses its place. A field written on
  a third of cycles and compared to nothing is a container, and this ADR does
  not permit containers to be mistaken for the thing (#1847).
- The hypothesis store must separate its two populations, or stop accepting
  task titles. Its verdict yield is already ADR-009's subject; what is new here
  is that most of what it is scoring was never a hypothesis.
- A causal verdict source must be restored or the loss declared. `microbench`
  is the only source that can refute on measurement; while it is absent, every
  verdict rests on usage, and that limit belongs in what the dashboard says
  rather than in what a reader has to infer.
- Insight becomes a cheap instrument against repetition. The day the diary was
  measured it carried four separate entries proposing index.md exclusions in the
  lessons tests; a line saying "tried this, it was already done" sits in front of
  84% of cycles at their first tool call (#1785).

## Alternatives considered

**Decide #1844 by argument and ship once.** Rejected by the operator, and the
day's own data says why he is right: every strong claim in this record is a
number that did not exist a week ago, and none of them would have been produced
by more discussion. The reachability figures, the 84%, the zero refutations —
each arrived from a shipped counter.

**Put HADI in a new store.** Rejected. The containers exist and are mostly
empty; a fifth store would be a fifth hat for the same string. The diary is
already in front of 84% of cycles, which is the scarcest property in the system
and the one thing a new store would not have.

**Require a falsifiable hypothesis via a validator.** Rejected for now. A
lexical check on a claim's shape is the same instrument that rejected the
narrator's only story for containing the word "failure" (#1840) — presence
tested where role was meant. Rule 4 measures the outcome instead: if
refutations stay at zero, the hypotheses are not hypotheses, and that is
visible without parsing a single one.

**Write the insight into `lessons/` instead of the diary.** Not rejected, but
not this record's decision. `lessons/` is a curated, promoted corpus with its
own integrity tests; the diary is a day's scratch. An insight worth keeping past
the day should reach `lessons/` through the curator's existing path, which is
ADR-028 rule 6's boundary fold, not a second write from the cycle.

## References

- #1844 — the diary carries only what the ledger already holds; the experiment
  this record unblocks.
- #1847 — the hypothesis loop's containers, sources and zero refutations.
- ADR-028 — the day diary, its read obligation, its counter, and its boundary fold.
- ADR-009 — hypothesis loop verdict yield; unverdictable versus undecided.
- ADR-007 — deterministic hypothesis claim identity.
- ADR-020 — direction comes from reflection over a span, never an instantaneous signal.
- ADR-026 — the day is a cycle.
- ADR-022 — one question, one file, one owner.
- #878 — the verdict machinery and its trust boundary.
- #1785 — `self_dedup` rejecting the same ideas repeatedly.
- Measurements: `state/diary_fitness/cycle_scans.jsonl` (19 cycles),
  `state/hypotheses/lifecycle.json` (153 entries), `state/demand/completed.json`
  (905 entries, 543 confirmed), `state/ledger/cycles-2026-09-20.jsonl.gz`
  (46 proposals, 15 with a claim), all read 2026-09-21.

## Addendum 2026-09-24 — the power limit of this environment (#1455)

The standalone experiment ledger (`nanobot/runtime/experiment_ledger.py`,
`state/experiments/results.jsonl`) is retired in #1455: it never had a
production writer or reader, and the host never held the file. **Retiring it
is not retiring experiments.** Hypotheses, their measurements and their
verdicts live where this ADR puts them — `state/hypotheses/` and the day
diary — and every HADI loop keeps its own before/after measurement.

What #1455 established is worth more than the file, because it bounds what any
measurement here can conclude. From 104 scorecard snapshots over 3.8 days
(2026-09-13 → 2026-09-16) on `eeepc`:

- **Intra-day wobble** of `confirmed_integration_ratio` was 3.20–4.26 pp per
  day (6.16 pp across the window); of `repeat_failure_rate`, 2.50–4.21 pp per
  day (7.97 pp across the window). Task-mix drift moves the baseline 4–6 pp.
- **Detecting a 5 pp effect** (two-sided α = 0.05, power 0.80) with two
  independent arms needs about **2,188 observations** (CIR, 75 → 80 %) or
  **2,660** (RFR, 33 → 28 %) — at the host's ~40 terminal outcomes a day,
  roughly 55–67 days.
- A **paired replay** of the same proposal under two variants needs about
  **626 pairs** (McNemar, discordant rate ≈ 20 %) — achievable in 10–16 days,
  but only with double the inference load or an offline replay harness that
  does not exist.

Consequences for how results here are read:

1. A rate compared across a few dozen cycles is **directional, not
   conclusive**, unless the effect is far larger than the daily wobble. A
   24-cycle measurement (as in #1903) yields a preliminary verdict and says so.
2. Prefer paired or within-proposal comparisons, and structural evidence that
   does not depend on sample size, over between-cycle rate comparisons.
3. "No significant change" at this sample size is absence of evidence, not
   evidence of absence.
