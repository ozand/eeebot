# Closing the Insight → Hypothesis arc (HADI loop)

Status: proposal / design-note
Owner: self-evolving runtime (`nanobot/runtime/coordinator.py`)
Related: `docs/EEEBOT_SELF_IMPROVING_RUNTIME_OPERATING_CONTRACT.md`, `AGENTS.md`

## Why this exists

The self-evolving engine is intended to be the business **continuous-improvement
cycle — HADI**: form a **Hypothesis** aimed at a goal, take **Action**, collect
**Data**, analyze the data into **Insight**, and let that insight shape the
**next** hypothesis. The insight → next-hypothesis arc is what makes HADI a
*learning* loop rather than a *busywork* loop.

Conceptually this is the same shape as karpathy/autoresearch's overnight loop
(modify → fixed-budget run → keep/discard on a metric), but generalized: our
"metric/gradient" is not a single scalar (bits-per-byte) — it is the **Insight**
carried forward. That is richer, but only if the arc is actually closed in code.

## Current state (as of 2026-06-23)

Three of the four arcs are closed and honest:

| Stage | Implementation | State |
|-------|----------------|-------|
| **H** — Hypothesis | `experiment.hypothesis` (coordinator.py:3419), `hadi_cycle.hypothesis` (coordinator.py:280) | present each cycle |
| **A** — Action | bounded subagent: `subagent_materializer.py` + `bounded_subagent_executor.py` | present (after executor-gate fix, PR #170) |
| **D** — Data | `state/reports/evolution-*.json`: `changed_files`, `result_status`, `reward_signal` (coordinator.py:4687), budget/subagent utilization | collected |
| **I** — Insight | `_derive_insight` (coordinator.py:975) → `_write_structured_lesson` (coordinator.py:999) → `lessons.yaml`; `update_lessons_from_cycle` (coordinator.py:4789) | derived and stored |
| **I → next H** | `LessonsDB.query_for_task(current_task_id)` (coordinator.py:2458) attaches `reusable_insight` as `lessons_context` to an **already-selected** task (coordinator.py:2453-2492) | **open / generic** |

### The gap

Insights accumulate in `lessons.yaml`, but they are only read back as
"avoid known pitfalls" context for whichever task is **already** selected
(`query_for_task` keyed by `current_task_id`). That is a D→A safety net, not an
I→H generator.

When the Active backlog empties, the "next hypothesis" comes from **hardcoded
templates** — `_synthesized_next_improvement_candidate` ("Synthesize one new
bounded improvement candidate", coordinator.py:250) and
`_synthesized_materialize_improvement_candidate` ("Materialize one bounded
improvement", coordinator.py:270) — or from an exhaustible
`state/research/feed.json` snapshot. **None of these paths read the accumulated
insights to formulate a new, specific, goal-directed hypothesis.**

### Consequence: stagnation

Because Insight does not push Hypothesis, the engine has no content gradient
once the enumerated backlog is exhausted. It falls back to generic
synthesize→materialize→discard bookkeeping that produces no material progress.
This is the observed stall mode (e.g. backlog fully `[Done]` → research feed
returns already-done candidates → auto-seed skips them all → no new work).

## Proposed change

Close the I→H arc: make next-hypothesis generation **consume accumulated
insights/lessons plus metric deltas** and emit a *specific* hypothesis, instead
of a static template or a feed snapshot.

Concretely:

1. `_synthesized_*_candidate` and/or `_write_research_feed` receive, on input,
   the top-N fresh `reusable_insight` / `generalized_insight` from `LessonsDB`
   and the recent `reward_signal` / material-progress delta.
2. The candidate's `title` / `acceptance` are derived **from those inputs**
   (e.g. "Address insight: <reusable_insight> — <bounded acceptance>"), not from
   a hardcoded string.
3. Goal-directedness is preserved: the generated hypothesis must still attach
   HADI metadata and a Definition-of-Ready/Done (existing `task_readiness`).

### Definition of Done

- When the Active backlog is empty, the coordinator forms a concrete next
  hypothesis derived from **at least one fresh insight** (or a metric delta),
  not from a generic template.
- Unit test: given a `LessonsDB` with ≥1 recent lesson and an empty backlog,
  the generated candidate's title/acceptance reflect that lesson's content.
- No regression to the existing HADI escalation / goal-rotation behavior.
- "Backlog empty" is no longer a terminal stall state while insights or
  metric deltas exist.

## Non-goals

- Not a rewrite of the scorer or reward model.
- Not manual backlog seeding (treats the symptom, not the open arc).
- No change to the executor-gate or auto-seed mechanics (both already correct).

## Rollout

dev (canonical `ozand/eeebot`, branch + PR) → CI green → rollout to running
`ozand/eeebot-self-evolving` (branch + PR), per `AGENTS.md` dev→test→rollout.
