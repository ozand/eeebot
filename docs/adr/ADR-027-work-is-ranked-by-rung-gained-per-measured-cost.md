---
title: Work is ranked by rung gained per measured cost, and estimates are audited against outcomes rather than re-scored
status: accepted
date: 2026-09-19
authors: [ozand]
related: ["#1769", "#1785", "#1773", "ADR-019", "ADR-023", "ADR-024", "ADR-025", "ADR-026"]
tags: [architecture, runtime, demand, scorecard, observability]
---

# Status

**Accepted by the operator, 2026-09-19.** Nothing in the tree satisfies this record. It depends on ADR-025's rung definition (in progress as #1769) for its value term and on ADR-026's day clock for its urgency term. Its cheapest component — size calibration — depends on neither and can be built first.

# Context

The loop picks its next task from a demand queue that has no notion of what a task is worth. Everything that currently shapes the choice is subtractive: the doc-only budget, self-dedup, demand cooling, the existence index. Measured 2026-09-19: 43 self-dedup rejections and 37 cooling events against 16 proposals and 10 successes; over a week, 339 self-dedup rejections across 70 tasks, one of them refused 101 times without ever being done (#1785).

The result is visible in the output. All 8 successful cycles on 2026-09-19 had the same shape — *"add one function to an existing script, plus a test"* — and 5 of the 8 extended an artifact with no production consumer. Nothing in the system said that one of those tasks was worth more than another, so the loop did the shape that was always available.

The operator's instruction is direct: **text nobody reads and code nobody runs must score below work that reaches the goal and gets used.** Not as a prohibition — as a ranking, so the better work wins on merit.

Two inputs that did not exist a day ago now do. ADR-025 defines the rung (*an artifact is finished when something else depends on its working*), which makes value computable. ADR-026 defines the day and its deliverable, which makes urgency computable. Before ADR-026 there was no deadline anywhere in the system, and a scoring scheme containing a term nobody can derive teaches the agent that scoring is decoration.

# Decision

## 1. Two terms, both measurable; no imported term we cannot derive

Weighted-shortest-job-first divides cost of delay by job size. The canonical numerator has three components; two of them do not exist here and are not adopted. The numerator is:

**Value = rung gained + known failure mode reduced.**

The rung comes from ADR-025 and gives an ordering that needs no judgement call:

| the task… | value |
|---|---|
| connects an artifact that is finished and unused — gives it an invoker | **highest**: one cycle of work, one rung gained |
| extends something that already has a production consumer | high: the improvement propagates |
| moves the day's deliverable toward its next stage (ADR-026) | high, and rising with the day clock |
| reduces a failure mode with a before/after measurement | high — already `goals.md` validity rule 3 |
| creates a new artifact with a named consumer | moderate; the claim is checked at integration |
| creates a new artifact with no named consumer | **lowest**: by construction, a new leaf |
| writes a document nothing routes to | **lowest** |

This is the operator's instruction expressed as arithmetic rather than as a quota. The 114 finished-but-unconnected tools sit at the top of the queue automatically, and each costs about one cycle.

## 2. Urgency comes from the day clock, not from a judgement

Time criticality is a function of two facts the harness knows: hours remaining before deep sleep, and the stage today's deliverable has reached (ADR-026 decision 3).

Early in the day with the deliverable unstarted, infrastructure work that makes the pipeline better is rational. Late in the day with nothing rendered, only work that moves the deliverable is. Urgency therefore rises for deliverable-bearing work as the day runs down, and is flat for everything else.

No task is ever urgent because the model said so.

## 3. Size is measured history, not self-report

This is where gaming lives: under-state the size, inflate the ratio, win the queue. The mitigation is not vigilance; it is to stop asking.

The system holds thousands of recorded cycles, and cost is strongly predictable from task shape — *"add a function to an existing script"* has a known distribution of tool iterations and wall-clock. So **the denominator is the measured historical cost of that task shape**. A self-estimate is used only for a shape with no history, is marked as an estimate, and is replaced by measurement the first time that shape completes.

## 4. Estimates are audited against what happened, never re-scored by another model

The operator asked for a nightly check that the agent did not inflate or deflate its scores. One correction to the mechanism, and it is the load-bearing part of this record.

**A second model re-reading the same task description will mostly agree with the first.** Two language-model judgements over one text are correlated; that is a review, and it would look like a control without being one.

The audit compares the estimate with the outcome:

| estimated | audited against | available |
|---|---|---|
| size | tool iterations, wall clock, whether it integrated | **next day** |
| value: rung gained | did the promised edge appear in the graph | weeks |
| value: failure reduced | did the named failure rate move | weeks |

Two properties the audit must have:

- It reports **systematic bias, not individual error**. One task estimated wrongly is noise. *"Tasks of this shape are consistently estimated at half their cost"* is a finding, and *"tasks the loop proposed itself are under-estimated while assigned tasks are not"* is the gaming signal the operator is asking for.
- Its inputs are measurements, never the loop's own reports. The audit is worthless if it audits a self-report against another self-report.

It runs at the deep-sleep boundary (ADR-026 decision 4) and its output crosses the boundary into the next day.

## 5. Provenance: the two halves land on opposite sides of ADR-023

Size calibration is derived from tool iterations, wall clock and integration records — all measured by the harness, all outside the loop's writable surface. **It may be presented to the loop as fact.**

Realised value is derived from the dependency graph, which resolves edges from the loop's own files. Per ADR-023 and ADR-025 it **may not** be presented as fact; it reaches ranking through the harness-side demand pipeline and the dashboard, and reaches the prompt only labelled as self-reported.

This split is stated here because it is not obvious and because getting it wrong in either direction is costly: hiding the size calibration removes the only fast feedback the loop has ever had, and presenting realised value as fact closes a measurement loop onto the thing being measured.

## 6. Ranking is an input, never a gate

No task is refused for scoring low. The queue is ordered; nothing is blocked.

This is the same clause as ADR-024 and ADR-025 and it is repeated because this record is the one most likely to be read as a licence to suppress. The doc-only budget is the standing argument: it reported "exceeded" all day while deferring nothing, rewrote the text of the requests it let through, and pushed the monoculture from documents into eight near-identical script additions a day. The defect in this system has never been that bad work was permitted. It is that good work was invisible.

## 7. Several candidates, one call

The loop proposes more than one candidate and ranks them before choosing. The cost discipline: **one model call returning several scored candidates**, not one call per candidate. The call count per cycle is stated wherever this is implemented.

# Consequences

**The output shape should change, and it is falsifiable.** Today's baseline: 8 of 8 successful cycles were "add a function", 5 of 8 grew an unconnected artifact. If ranking works, the share of cycles that raise a rung goes up and that share is reported daily. If it does not move, the ranking is decoration and should be said so.

**The loop gets its first feedback on its own estimates.** Nothing today tells it that what it called small took 45 calls. That single number, returned the next day, is the cheapest behavioural signal available and does not depend on the graph, the day clock or anything else in this record.

**Value is unverified until the graph exists.** Rung-gained is a claim at proposal time and a measurement weeks later. Report it as a claim until the graph can settle it; do not let an unverified claim ride into the calibration as though it were measured.

**Scoring costs calls inside an 80-step budget.** One call per cycle, stated and measured. If it costs more than that in practice, the scheme is too expensive for this host and should be simplified rather than quietly kept.

**A shape with no history has no denominator.** The first instance of any new kind of work is ranked on an estimate and is therefore the easiest thing to game. The mitigation is exposure, not prevention: estimates are marked, and the audit reports how often the loop introduced a novel shape whose measured cost then exceeded its estimate.

# Alternatives considered

**Full WSJF with the canonical three-term numerator.** Rejected. Two of the three terms have no derivable value in this system, and a score whose majority is unfounded teaches that scores are ceremony. Two measurable terms beat five ritual ones.

**Self-estimated size with a nightly reviewer model.** Rejected — this is the mechanism the operator proposed and the one substantive change this record makes to it. Correlated judgements over the same text do not constitute a control. Comparing the estimate to the measured cost does, and the data is already recorded.

**A quota or a gate on low-value work.** Rejected, and the measured record of the doc-only budget is the argument.

**Rank by test coverage, recency, or artifact count.** Rejected: each is present in the population that is failing. 90 of 114 unconnected artifacts have tests; coverage cannot discriminate readiness (ADR-025 decision 2).

**Wait for the graph before doing anything.** Rejected in part. Value needs the graph; size calibration does not, and it is the component with the shortest path to a behavioural signal. Build it first.

# References

- ADR-025 — the rung; supplies the value term.
- ADR-026 — the day clock and the deliverable; supplies the urgency term.
- ADR-024 — typed edges; what "used" means.
- ADR-023 — provenance; the split in decision 5.
- ADR-019 — an optimisation is measured against the fastest implementation this host can run; the same discipline applied to cost estimates.
- #1769 — the dependency graph (value term's implementation).
- #1785 — self-dedup: 339 rejections over 70 tasks in a week, one refused 101 times; the subtractive regime this record is meant to replace.
- #1773 — the doc-only budget's measured record; the standing argument against decision 6 being read as a gate.
- Measurements, 2026-09-19: 8 successful cycles, all "add a function to an existing script"; 5 of 8 extended an artifact with no production consumer; 43 self-dedup rejections, 37 cooling events, 16 proposals, 10 successes.
