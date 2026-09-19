---
title: The day is a cycle — three clocks, a daily deliverable, and deep sleep as a named boundary
status: accepted
date: 2026-09-19
authors: [ozand]
related: ["#1775", "#1766", "#1772", "ADR-015", "ADR-016", "ADR-017", "ADR-022", "ADR-023", "ADR-025"]
tags: [architecture, runtime, charter, prompt, channel]
---

# Status

**Accepted by the operator, 2026-09-19.** Nothing in the tree satisfies this record. It changes the charter (see decision 5) and therefore takes precedence over `goals.md` as currently written until that file is edited.

# Context

## The agent has no clock at any level

Three timescales exist in the running system and the agent is aware of none of them.

- **The step.** A cycle is bounded at 80 tool iterations (`SELFEVO_MAX_TOOL_ITERATIONS=80` in the host preset). The number is stated once in the task text — *"Iteration budget this cycle: 80 tool iterations."* — and the agent is never told which step it is on. #1775 covers that gap.
- **The cycle.** One cycle is one life: it ends, and the next one begins with no recollection of it. Measured 2026-09-19: median 15 executor calls per cycle, longest 45. The loop runs 13–47 cycles in a day.
- **The day.** Nothing in the system names it. The agent does not know a day exists, that it ends, or that anything happens at the end.

## A daily boundary already exists, unannounced

The host runs a nightly cluster: the knowledge curator at 00:00, the action index at 00:05, host capabilities at 01:00, the systemd drift check at 02:00. #1772 added a daily digest of what the loop integrated, plus a revert tool. So the machinery of an end-of-day already exists as a set of operator-facing jobs. What does not exist is a *boundary the agent knows about*, or any path by which the day's conclusions re-enter the next day's work.

## The charter currently forbids the deliverable

`goals.md`, lines 46–50:

> ## FUTURE (deferred, not a current demand source)
>
> Creative works — demoscene-style visuals, generated music, small games — become goals only once the system demonstrably squeezes the maximum from itself and the host.

The operator's decision is that the day's obligation is a published video, and that the infrastructure work is in service of it — demoscene spectacle, editing, narration. That reverses this clause. Recording the decision without recording the charter change would leave the loop holding two instructions that contradict, which is exactly what ADR-022 exists to prevent.

## And the capability does not exist

Of 180 scripts in the instance, **none** concerns video, rendering to a file, editing or publishing. A grep for `video|render|frame|scene|demo|publish|youtube` over the script names returns nothing. What exists is a framebuffer surface, a tile surface, avatar cost measurement and explanation scenes — primitives for putting pixels on the host's own screen, and nothing that assembles a sequence or sends it anywhere. ADR-015/016/017 describe the channel; roughly 40% of it is built and it has no scheduler.

A daily obligation levied against a capability that does not exist produces one of two outcomes every day: a recorded failure, or a fabricated deliverable. Decision 3 exists to stop the second.

# Decision

## 1. Three timescales, named once

| clock | bound | what it is |
|---|---|---|
| **step** | 80 tool iterations | the unit of action inside one cycle |
| **cycle** | one task, ends in a commit or a stated block | one life; nothing of its reasoning survives it |
| **day** | 24 hours, ends in deep sleep | the unit the deliverable is owed in, and the unit over which learning is consolidated |

These are the system's only temporal units. Anything that needs a schedule expresses it in one of the three.

## 2. The agent knows its position in all three, and the day block reports actions, not verdicts

Every turn carries: the current step and the budget; the cycle's own identity; and the day's position — hours elapsed, hours to deep sleep, and what has happened in the day so far.

**What the day block may contain is constrained by a distinction this record introduces.**

ADR-023 holds that a figure may be presented to the loop as harness-owned fact only if every input in its derivation lies outside the loop's writable surface. Taken literally that would bar the day block from reporting the loop's own work — commits are written by the loop.

The distinction that resolves it: **an action is not a verdict.**

- *"You integrated 8 changes today, touching these files"* is a mirror. The loop caused it; reflecting it back adds no authority it did not already have, and withholding it is what produces an agent that cannot see its own day.
- *"Your work today was valuable"*, or any figure standing in for that, is a verdict. It must pass ADR-023's provenance test or be labelled self-reported.

The day block reports actions and clock position. Verdicts stay under ADR-023 unchanged.

## 3. The day has one deliverable, and "done" for it is staged

The day's obligation is a video that reaches people. That end state is not reachable today, and pretending otherwise would put the whole design on a metric that can only be faked. So the deliverable is defined in three stages, each measurable, and **only the last one counts toward the charter's top rung**:

| stage | done when | measurable today |
|---|---|---|
| **rendered** | a sequence exists as an artifact on the host, produced by the loop's own pipeline | yes, once a pipeline exists |
| **published** | it left the host, with the publish event recorded | no — no channel plumbing |
| **observed** | a person outside the system received it, with the observation recorded | no — no observation channel |

Rules that keep this from becoming a counter to game:

- An observation is **never inferred** — not from a render completing, not from a publish call returning success. Consistent with ADR-014, ADR-015 and ADR-025 clause 4.
- A day that produced no deliverable is recorded as such and is a normal outcome, not an error to work around. The same sentence `OPERATING.md` already applies to a cycle that does not integrate applies to a day that does not publish.
- **Quantity is never the metric.** One video that was watched outranks seven that were rendered. Until the observation channel exists, the honest report is *"stage reached: rendered; observed: unknown"*, and `unknown` is never rendered as zero or as success.

## 4. Deep sleep is a named boundary with declared inputs and outputs

At the end of the day the system stops taking new work and runs one bounded pass whose job is to close the day. It consolidates what the day learned, evaluates what the day did, and produces the one artifact the next day starts from.

What crosses the boundary is exactly this, and is stated here so nothing else is assumed to:

- integrated commits;
- `memory/` and `lessons/`, as consolidated by the curator — which under #1768 the executor may add to and may not delete;
- the day's report: what was done, what stage the deliverable reached, how the day's estimates compared with what actually happened (ADR-027);
- the handoff line for the next day.

Nothing else. No recollection of reasoning, no in-flight state, no unfinished work held in mind. The agent is told this in the same words: it will wake without the details, and only what it wrote down will be there.

The existing nightly jobs — curator, action index, the #1772 digest — are the first components of this boundary rather than a separate arrangement to be duplicated.

## 5. The charter changes: creative output is a current obligation

`goals.md` `## FUTURE` is superseded for creative output. Demoscene-style visuals, narration and the assembled video move from deferred future work to the day's standing deliverable, and Vector 1 self-improvement is explicitly in service of it: the tools, the rendering, the editing, the storytelling.

Two consequences that must be handled and not discovered:

- **The edit does not fit.** `goals.md` is 3,027 characters against a 3,200-character block cap — 173 characters free. Promoting a vector needs more than that. The ontology's budget must gain room before this edit can land; that is the prerequisite, not a detail.
- **Vector 2 is unchanged.** Operator-facing transparency stays secondary to self-improvement. The video is not a third vector; it is the output that makes Vector 1's ladder terminate somewhere.

# Consequences

**The agent gains a horizon.** Today every cycle is a standalone life with a step budget it cannot see the end of. With three clocks it can pace inside a cycle, and it can tell that infrastructure work early in the day is rational while the same work with two hours left and nothing rendered is not.

**Urgency becomes computable, and therefore ranking becomes possible.** This is the input ADR-027 needs: without a deadline, "time criticality" is a number nobody can derive, and a scoring scheme containing a term nobody can derive teaches the agent that scoring is decoration.

**The day's first honest report will be a failure, and that is correct.** No pipeline exists, so the earliest days end at *"stage reached: none"*. That is the measurement that makes the missing capability visible and rankable, and it is strictly better than a system in which the gap is invisible because nothing was owed.

**Deep sleep gives the day's learning somewhere to go.** Today the curator consolidates memory on a timer and the loop never learns that it happened. Naming the boundary makes the consolidation an event with a before and an after, which is the precondition for measuring whether consolidation helps.

**A daily deadline is a reward-hack incentive and is treated as one.** Decision 3's staging, the refusal to infer an observation, and "quantity is never the metric" are the three guards. They are not optional refinements; they are the reason the deadline can be introduced at all.

# Alternatives considered

**Leave the day unnamed and keep only the step budget.** Rejected: it is the status quo, and it produced 8 successful cycles in a day all of the same shape with nothing reaching the screen. Without a day, there is no unit in which "nothing reached the screen" is a fact about anything.

**Make the deliverable "a published video" with no staging.** Rejected. Publishing is not implemented, so the obligation would be unmeetable on day one, and an unmeetable daily goal is either ignored or satisfied dishonestly. Staging keeps the obligation real and the honesty enforceable.

**Count rendered artifacts as the metric.** Rejected — it is the version of this design that fails. A count of renders is trivially inflatable and says nothing about whether anyone saw anything. It is the same error as counting scripts and calling them components.

**Defer the charter change until the pipeline exists.** Rejected by the operator, and the reasoning is sound: the pipeline does not exist *because* nothing has ever been owed. Ranking cannot pull work toward a goal the charter calls deferred.

**Put the day's evaluation inside a cycle.** Rejected. It is the day's judgement, it needs the whole day's data, and a cycle that judges its own day is the provenance problem ADR-023 describes, at a larger scale.

# References

- #1775 — the step counter; this record's step clock is that issue's scope.
- #1766 — the harness-owned state block; the day block extends it, under the action-versus-verdict distinction in decision 2.
- #1772 — the daily digest and revert tool; the first components of decision 4's boundary.
- ADR-015, ADR-016, ADR-017 — the channel as an instrument; the deliverable's destination.
- ADR-023 — provenance; refined here by the action/verdict distinction, not weakened.
- ADR-025 — the rung; the deliverable's `observed` stage is that record's terminal-artifact case.
- ADR-027 — how work is ranked; consumes this record's day clock.
- `goals.md` lines 46–50 as of `1f3cae08` — the deferral clause this record supersedes.
- Host `eeepc`, 2026-09-19: nightly timers (curator 00:00, action index 00:05, capabilities 01:00, drift 02:00); 180 instance scripts, none matching `video|render|frame|scene|demo|publish|youtube`; `goals.md` 3,027 of 3,200 characters.
