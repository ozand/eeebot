---
title: The agent chooses its own work; the outer contour holds the goal and judges the day
status: accepted
date: 2026-09-21
authors: [ozand, architect]
related:
  - ADR-018
  - ADR-022
  - ADR-025
  - ADR-027
  - ADR-030
  - ADR-031
  - "#999"
  - "#1785"
  - "#1844"
tags: [loop, autonomy, strategy, identity, demand]
---

## Status

Accepted 2026-09-21, on the operator's decision. Companion to ADR-031, which
owns the shape of a cycle; this record owns who decides what goes in it.

## Context

The executor's task prompt opens with two lines, and the second is the
architecture stated out loud:

```
Task: Implement and commit: Codify core minimal pattern enumeration guideline in AGENTS.md
...
This task is not an operator priority; priorities are handled by the proposer.
```

The charter is in the same prompt — three vectors and five validity rules,
3,148 characters of it — and the executor has nothing to apply it to. It is
handed a work order and told that choosing is not its business.

The operator's objection is about management, and it is precise: a manager sets
strategy, not tasks. Told what to do, an employee loses autonomy and invention,
and the person degrades — *«его личность будет деградировать, его постоянно кто-то
заставляет делать что-то, что не его воля, не его цель, не его идея»*. Domain
expertise belongs with whoever does the work.

This is not sentiment about a program's feelings. Identity is already an
architectural object in this system: `IDENTITY.md` and `SOUL.md` are resident in
every executor prompt, 1,244 and 1,590 characters. A prompt that carries a self
and then issues an order contradicts itself in its own text.

And the behavioural claim is measured rather than argued. Changing one line of
the **charter** — that an artifact's runner must be something other than the
artifact itself (#1814) — moved the output shape within a night: monoculture
from 76% to 41%, with three shapes appearing that had never been seen, including
the first task ever aimed at the screen. No suppressor has ever done that:
`self_dedup` rejects the same ten ideas 43 times a day and the shape does not
move (#1785). **The loop responds to a goal and does not respond to a
prohibition.** That is the strongest evidence available for what follows.

There is a second finding, from the code. The agent already thinks
strategically, and the thought is routed away from it.
`nanobot/runtime/strategist.py` — `SCHEMA = "strategist-hadi-v1"`, #999 — reads
lessons, prior decisions, recent cycles, insights, the funnel and the scorecard
and emits up to three hypotheses. Those hypotheses are appended to the
hypothesis backlog, become demand items of kind `hypothesis`, are throttled to
`_MAX_HYPOTHESIS_ITEMS = 1` in flight, rank fourth of six
(`defect > goal-gap > skill-candidate > hypothesis > decay > reflection`), and
return to the executor rewritten by the proposer as `Implement and commit: …`.

The agent's own strategic thought is laundered through the dispatcher and comes
back as an order it cannot recognise as its own.

A third gap sits behind all of it. `goals.md` ranks **outcomes** — what reached a
person, what gained a runner, what improved something already depended on. It
says nothing about **stages of capability**: that a small improvement to work you
repeat daily buys you the capacity to do, next week, something you cannot do
today. Without that axis every cycle optimises the rank it can reach now, and
investment is never rational.

## Decision

### 1. The agent chooses its own work

Always give the executor the goal, the history and a ranked list of candidates,
and let it choose. Never hand it a single task as an instruction. The line
*"priorities are handled by the proposer"* is removed from the task prompt,
because it is the instruction that makes every other part of this record
impossible.

The demand queue does not disappear and its ranking does not weaken: it becomes
an **input** to the choice rather than a dispatcher of it. ADR-027 decision 6
already says ranking is an input and never a gate; this is that rule carried to
its conclusion.

Choosing happens in the planning session (ADR-031 rule 5), where the agent reads
the charter, its own record of what it has been doing, and the ranked
candidates, and commits to an increment.

### 2. The strategist returns to the agent

The planning role already exists and is wired outward. Three parameters change,
and nothing is built:

| | now | decided |
|---|---|---|
| cadence | once a day, `OnCalendar 03:00` | between cycles |
| actor | an external model | the executor's own model, clean session |
| destination | hypothesis backlog → demand → proposer → order | the day diary, read by the agent itself next session |

Never route the agent's own plan through the dispatcher. A plan that returns as
an assignment has lost the one property that made it a plan.

### 3. The outer contour holds the goal and judges the day

The external contour keeps a real job, and it is larger than dispatching: at the
day's boundary it asks whether the day moved toward the goal or produced the
appearance of work — documentation rewritten, tests adjusted, an artifact grown
that nothing runs.

Always judge the day, never the task. Judging a task is management by
assignment, which rule 1 removes; judging the day is strategy, and it is the one
thing the agent cannot do for itself, because no actor may evaluate its own
work. The claim is the instance's and the verdict is the harness's — ADR-018,
unchanged.

Two instruments belong to this role rather than standing alone: the narrator,
which turns the day's ledger into an account of it, and the scorecard. That they
have had no owner is why the narrator was built and left unrun for six days.

The judgement classifies the strength of its own evidence — independently
reproducible, measured once, inferred, asserted — and says which it had. A
verdict derived from usage must not read like one derived from a measurement
(#1847).

### 4. The capability ladder: raw material → component → complex component → product, and a product becomes a component

**The operator's ladder, and the missing axis of `goals.md`.**

| stage | for this loop |
|---|---|
| raw material | the ledger, lessons, the diary, traces — what accumulates on its own |
| component | a tool or skill that removes one repeated manual step |
| complex component | components combined into a capability that did not exist: a pipeline, an automatic run, a search |
| product | something that leaves the machine — Vector 3, a frame a person can watch |

**The ladder has no top.** A finished product re-enters as a component of a
larger one. This is ADR-025's rung seen from the producing side: a product that
becomes a component is exactly an artifact something has come to depend on.

Always let the ladder justify work that today's validity rules score low. A
component does not reach a person and ranks near the bottom of `goals.md`'s
list — and without it there is no complex component and no product. This axis is
what makes investment rational: *optimise the work you repeat, and the work you
could not afford becomes affordable.*

Never let the ladder become an excuse. A stage is claimed by naming what the next
stage will be and what currently makes it unreachable; a component whose complex
component is never named is a leaf with a story attached.

### 5. Autonomy in belief, never in judgement

Always keep the boundary where ADR-018 put it. The agent chooses its work,
states what it expects to become true, and says what it learned. It never issues
the verdict on whether it succeeded — that is measured by the harness, from
sidecars the instance cannot write.

This is why rule 1 does not conflict with "no actor evaluates its own work". The
two questions are different: *what shall I do and why will it work* is the
worker's; *did it work* is not.

## Consequences

- **The proposer stops authoring tasks.** Its remaining work — judging the day,
  grading evidence, rejecting — is a different job with a different cadence, and
  the daily strategist slot has the same cadence and the same inputs. Whether
  they merge is left open here deliberately; the operator's stated preference is
  to merge them.
- **The planning session is worthless until the diary carries content.** It reads
  the written record by construction (ADR-031 rule 5). Ship #1844 first. This is
  an ordering, not a preference.
- **`goals.md` gains an axis and must stay under its cap.** The charter is
  immutable to the loop and lives in a pooled character budget; the ladder
  competes with what is already there. Adding it is an edit to be made within
  the budget, not beside it.
- **Autonomy is falsifiable.** If the share of cycles raising a rung does not
  rise, or the monoculture returns, the change is withdrawn. ADR-030 rule 1:
  withdrawal on a measurement is the experiment working.
- **Attribution changes.** Work chosen by the agent and work offered by the
  ranked queue must remain distinguishable in the ledger, or no later
  measurement can tell whether choosing helped.

## Alternatives considered

**Keep the proposer authoring tasks, and merely widen them.** Rejected. A task
written by something that has never spent a tool iteration cannot size an
increment (ADR-031 rule 3), and the identity objection is untouched: a better
order is still an order.

**Give the agent the queue but keep a single mandated pick.** Rejected as the
same thing with extra steps. Offering a ranked list and then choosing for it
teaches that the charter is decoration, which is the failure mode ADR-027
decision 6 names for ranking.

**Let the agent also verdict its own increments.** Rejected, and named because
the autonomy argument reads as though it should follow. It does not: measurement
by the measured is the one thing this architecture has never allowed, and #878's
trust boundary is the reason the few real verdicts in the system can be
believed.

**Write the ladder as a fifth validity rule in `goals.md`.** Rejected: the
validity rules rank finished outcomes, and the ladder explains why unfinished
work is worth doing. Folding one into the other would make the ladder read as a
claim of progress, which is exactly what it must not become.

## References

- ADR-031 — the box, the increment, and the planning session.
- ADR-030 — HADI; an unknown is a measurement to schedule.
- ADR-027 — ranking as an input, never a gate (decision 6).
- ADR-025 — an artifact is finished when something depends on it.
- ADR-022 — one question, one file, one owner.
- ADR-018 — the harness judges, the instance draws.
- #999 — the strategist and `strategist-hadi-v1`.
- #1785 — `self_dedup` rejecting the same ideas without moving the shape.
- #1814 — the charter change that moved the monoculture in one night.
- #1844 — the diary's content, the planning session's only input.
- #1847 — evidence grading; verdicts from usage versus from measurement.
