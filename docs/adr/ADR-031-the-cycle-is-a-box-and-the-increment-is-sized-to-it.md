---
title: The cycle is a box of 80 ticks, the increment is sized to fill it, and a planning session opens it
status: accepted
date: 2026-09-21
authors: [ozand, architect]
related:
  - ADR-017
  - ADR-026
  - ADR-027
  - ADR-028
  - ADR-030
  - ADR-032
  - "#1775"
  - "#1776"
  - "#1844"
tags: [loop, cycle, planning, increment, budget]
---

## Status

Accepted 2026-09-21, on the operator's decision, in the same session as ADR-030
and ADR-032. This record owns the *shape of one cycle*; ADR-032 owns *who
chooses what goes in it*. Neither is complete without the other.

## Context

A cycle is given 80 tool iterations. Measured 2026-09-21 across 24 cycles:

```
iterations per cycle:  min 4   median 9   mean 13.8   max 45
budget used at median: 12%
ticks unused that day: 1,692
```

**No cycle has ever reached 80.** The largest was 45. Half the day's cycles
finished in nine iterations or fewer.

This reframes a problem the system has been describing in the wrong vocabulary.
ADR-027 read the monoculture — 8 of 8 successful cycles on 2026-09-19 having the
shape *"add one function to an existing script, plus a test"* — as a **value**
problem: nothing said one task was worth more than another, so the loop did the
shape that was always available. That reading is true and incomplete. *"Add one
function plus a test"* is also a shape that **costs nine iterations**. The work
is sized to an eighth of the box because nothing has ever asked for an increment
that needs the box.

Three constraints were assumed to bind and do not.

**The host does not bind.** The eeepc constrains what code the loop can *run*.
The executor's model is served elsewhere; the host does not compute it. Thinking
is not rationed by this hardware.

**The context window does not bind.** Measured over 330 executor calls the same
day: window 98,304 tokens, median prompt 31,150, peak 74,169. The longest cycle
— 45 iterations — peaked at 74% of the window. Compaction exists and handles the
rest, and the day diary is the memory that survives it, the same way a person
keeps a board or a notebook so a working context can be rebuilt rather than
remembered. Output truncation is negligible: `finish_reason: length` on 2 of 331
calls.

**What binds is the box.** 80 iterations, one focus, then a boundary. It is a
Pomodoro: a fixed span of attention, not a measure of how much work exists.

And the boundary is currently empty. A Pomodoro's pause carries a decision —
continue this, or take the next thing. Our cycle boundary carries none: the next
cycle is simply handed a new task, so *"I am continuing"* is not expressible.

One further finding, from the code rather than the numbers. The session this
record calls a planning session already exists. `nanobot/runtime/strategist.py`
declares `SCHEMA = "strategist-hadi-v1"`, reads lessons, prior decisions, recent
cycles, insights, the funnel and the scorecard, and emits up to three
hypotheses. It runs `OnCalendar=*-*-* 03:00:00` — once a day — on an external
model, and its output is appended to the hypothesis backlog, throttled to
`_MAX_HYPOTHESIS_ITEMS = 1` in flight and ranked fourth of six demand kinds.
Who should own it is ADR-032's subject; that it exists, and at what cadence, is
this record's.

## Decision

### 1. The box is 80 ticks, one focus, and it is meant to be filled

Always treat the iteration budget as the span the work is cut to, never as a
ceiling that good work stays under. A cycle that ends at nine iterations has not
been efficient; it has done an eighth of a cycle's work and discarded the rest
of the box.

Never read a short cycle as a saving. The unused ticks are not banked — the
boundary arrives, the context is dropped, and the next cycle starts from the
written record. Thirty-one of them a day, at today's cadence, is the cost.

This is not a demand for longer transcripts. It is a demand for a larger
**increment**: one verifiable thing, whose verification arrives inside the box.

### 2. An increment is right-sized when it needs the box and is verifiable at its end

Prefer work that plausibly consumes the box and ends in something checkable —
a test that passes, a measurement that moved, an artifact that now has a runner.
Never pad a small task to fill ticks; the fill is a consequence of the increment
being substantial, never a target pursued on its own.

The two failure directions are named, and they are not symmetric:

- **Under-fill** — the increment was smaller than the box. This is the current
  and dominant failure: median 12%.
- **Over-run** — the increment did not fit. The work is not abandoned; the
  boundary arrives, the state is written to the day diary, and the next cycle
  continues it (ADR-028, ADR-017 for work that structurally cannot fit a cycle).

An increment spanning several cycles is therefore normal and expected, not an
exception to be avoided.

### 3. Under-fill is a forecast error, and the forecast is what improves

Never treat a short cycle as a fault of the executor's diligence. It is a
mis-sized increment, which is a **prediction** about how much work a piece of
work is — and a prediction is improved by comparing it with what happened, not
by exhortation.

Always feed the comparison back: the plan states what it expects to consume, the
harness records what was consumed, and the difference is returned to the next
planning session. This is ADR-027 decision 4's audit mechanism applied to a
direction it was not built for.

ADR-027 built that audit to catch **under-stated** size — the gaming move where a
task is claimed cheap to win the queue. The data says nobody is doing that. The
loop is not claiming small things are cheap; it is choosing things that are
genuinely small. The guard is pointed the wrong way round, and this rule turns
it around rather than adding a second one.

### 4. The size of an increment enters its value

**Amends ADR-027 decision 1.** `classify_value` scores a candidate by *shape* —
`connects_leaf`, `extends_component`, `moves_deliverable` — and WSJF divides by
measured cost. Two candidates of the same shape therefore differ only in their
denominator, and the cheaper one wins by construction.

So connecting one artifact in nine iterations outranks connecting one artifact
in seventy, and the ranking that shipped on 2026-09-20 actively selects for
under-filling the box. The conflict was created by that merge and nothing
currently reports it.

Never let value be a function of shape alone. The numerator must carry how much
is gained, not only what kind of gain it is — an increment that connects three
artifacts is worth more than one that connects one, and a ranking that cannot
say so will keep choosing the smallest available instance of the best-scoring
shape.

The correction belongs in the numerator, not the denominator. Dividing by a
smaller cost stays correct; pretending a larger increment gains no more is what
is wrong.

### 5. A planning session opens the box, on 20 ticks, in a clean context

Between cycles, the **same model as the executor** runs a separate session whose
task never changes: *read what was done, and plan what to do next.*

Its properties are each load-bearing:

- **Same model.** The planner and the doer are one agent separated in time, not
  a manager and a subordinate (ADR-032).
- **Clean context.** It reads the *written record*, not the previous
  transcript. This is what makes the record matter: a planning session can only
  see what a cycle bothered to write down.
- **Fixed task.** The prompt is constant, so this is a role, not an assignment —
  ADR-022's ontology applied to a session instead of a file.
- **20 ticks.** Bounded, and separate from the 80. A cycle is therefore 20 + 80.

Its output is written where the agent itself will read it: the day diary
(ADR-028), not a harness-side backlog.

**The session is also where "look before you build" lives.** Always put the
obligations to consult what already exists — skills, memory, the earlier
record — in this session's fixed prompt, and state them unconditionally.

Never leave them as a conditional in the execution turn. `OPERATING.md`
currently says *"if a skill describes this work, `read_file` its `SKILL.md`"*,
which lets a cycle rule on applicability without opening anything; the catalogue
costs 4,048 resident characters and is read in 1 cycle of 24, while the diary's
unconditional *"read today's diary as the first action of the cycle"* is read in
84%. A conditional that the reader evaluates before looking is not an
instruction to look.

Discovery is a **stage**, not a judgement about whether this particular task
needs one. The session's prompt never varies, so the obligation cannot be
reasoned away, and its 20 ticks are separate from the execution box, so looking
costs the work nothing (#1857).

The analysis half and the planning half are the same two letters ADR-030 names:
the insight of the increment that just ended, and the hypothesis of the one
about to start. **HADI closes inside the agent's own recurring session**, with
no external contour in the loop at all.

### 6. The session is worthless before the record is worth reading

**Ordering, not preference.** The planning session sees only what was written.
Today's diary carries nineteen lines of `Implement and commit: <task title>` and
nothing else (#1844). A planner reading that has no material and will invent,
which is precisely the failure this whole design exists to remove.

Always ship the write instruction before the session that consumes it: #1844,
then the planning session, then ADR-032's change of who chooses.

### 7. The overhead is the experiment's own withdrawal condition

Planning costs 20 ticks whatever happens. At today's median of 9 execution
iterations, planning would be 69% of the cycle's calls; at a filled box, 20%.

So the design pays for itself exactly when it works. Always report the ratio
from the first day. If the increment does not grow, planning becomes the most
expensive part of the cycle and is withdrawn — per ADR-030 rule 1, that is the
experiment succeeding at telling us something, not the change failing.

## Consequences

- **`#1776` is promoted from an improvement to a prerequisite.** A 45-iteration
  cycle already peaks at 74% of the window. A filled box crosses it, so
  compaction runs every cycle instead of a few, and how much meaning survives
  compaction becomes the ceiling on how complex an increment can be.
- **`#1775` is part of this record's implementation.** The executor is told its
  budget once and never its position in it. Work cut to a box requires knowing
  where in the box you are.
- **The month file gets its first reader.** It has had none since ADR-028,
  because nothing existed that needed to remember across days. A multi-cycle
  increment does.
- **The day's cycle count falls and its work per cycle rises.** Any rate
  expressed per cycle — success share, monoculture share, rung share — changes
  meaning on the day this lands, and every such reader must be identified
  before it ships, in the shape ADR-029 used.
- **Terminal-state statistics will shift.** A longer cycle has more chances to
  hit a gateway error or a truncation. Distinguish *the increment was too big*
  from *the infrastructure failed inside a longer window* before reading a rise
  in abnormal terminations as evidence against this record.

## Alternatives considered

**Raise the iteration budget above 80.** Rejected: the budget is not the
binding constraint — 12% of it is used. Raising a ceiling nothing reaches
changes nothing.

**Let the proposer size the tasks larger.** Rejected as the wrong actor
(ADR-032), and as the wrong mechanism: a task written by something that has
never spent an iteration cannot forecast iterations. Sizing belongs with whoever
will feel the cost.

**Pad short cycles to reach the budget.** Rejected explicitly, and named here
because it is the obvious way to satisfy the metric without satisfying the
intent. Fill is a consequence of the increment, never a goal. A cycle that spends
seventy iterations producing nine iterations' worth of value is worse than
today.

**Plan inside the execution session.** Rejected: the planning session's value
comes from its clean context. A planner holding the previous transcript is
reasoning from the trace rather than from the record, which both costs the
window and removes the pressure that makes the record good.

## References

- ADR-030 — HADI, and an unknown as a measurement to schedule.
- ADR-032 — who chooses the work; the capability ladder.
- ADR-027 — ranking by rung gained per measured cost; decision 4's audit,
  turned around by rule 3 here and amended by rule 4.
- ADR-028 — the day diary, its read obligation and its boundary fold.
- ADR-026 — the day is a cycle.
- ADR-017 — work too long for a cycle runs in its own unit.
- #1775 — the executor is never told its position in the budget.
- #1776 — compaction drops bytes, not meaning.
- #1844 — the diary carries only what the ledger already holds.
- Measurements, all 2026-09-21: `state/llm_calls/2026-09-21.jsonl` (331 executor
  calls, 24 cycles), `state/diary_fitness/cycle_scans.jsonl`,
  `nanobot/runtime/strategist.py`, `nanobot/runtime/demand_ranking.py`.
