# Change: route goal_text.json backlog into real dispatch, prefer backlog progress on lane-switch

- **change-id:** backlog-routing-real-dispatch
- **issue:** #568
- **capability:** `docs/specs/self-evolving-runtime`
- **role / workstream:** role:developer / workstream:runtime

## Problem

#565/#566 closed the reward-hacking evidence gap, but the underlying reason
there was no real work to reward is still present: the eeepc host has zero
new git commits since 2026-06-26.

Contrary to issue #568's original framing, the coordinator already **has** a
working dispatch mechanism — `_write_materialized_improvement_artifact`
(`coordinator.py:2580-2711`) → `_write_subagent_request_artifact`
(`coordinator.py:2758-2843`) already writes real "implement and commit"
requests into `state/subagents/requests/` when it has a concrete backlog
task. Two separate things are broken upstream of that mechanism:

1. **The one backlog source that's actually current is never read.**
   `_write_materialized_improvement_artifact` falls back through, in order:
   `memory/MEMORY.md` (curriculum-gated) → `workspace/todo.md` →
   `state/research/feed.json`. On the host: MEMORY.md's backlog is exhausted
   (`Priority 12 [Done]`, nothing after it), `todo.md` is stale (references
   already-closed GitHub issues #549-554), so every materialize cycle falls
   through to the vague, self-invented research-feed candidate ("Priority 99:
   exploit and expand improvements in scripts/eeebot_dashboard.py") — which is
   exactly the pattern that produced fake progress before #566. Meanwhile
   `state/goals/goal_text.json` — freshly seeded on every deploy
   (`deploy_release.sh`) and containing the operator's actual current
   priorities ("Priority 5 — write scripts/cycle_logger.py", "Priority 6 —
   write scripts/smoke_test_loop.py") — is **never read by the coordinator at
   all**. It's only used by the bridge to build an LLM prompt string.

2. **Lane-switch has no backlog-priority awareness.** `pick_alternative_task`
   (`stop_guards.py:209-227`) picks "the first task whose id differs from
   current" from whatever list `_switch_off_stalled_lane`
   (`coordinator.py:4280-4311`) hands it — no ordering. Across 1841 sampled
   host cycles, only 231 ever reached `subagent-verify-materialized-improvement`
   (the task ID that triggers real dispatch) vs. 1268 on pure bookkeeping
   (`refresh-approval-gate`/`run-bounded-turn`/`record-reward`). On every
   stall, the switch is as likely to bounce back to bookkeeping as to progress
   toward the dispatch chain.

## Intended change

1. Add `_parse_backlog_task_from_goal_text(state_root)` — parses
   `state_root/goals/goal_text.json`'s `"Current priority targets:"` section
   (lines shaped `(A) Priority N — Title: instructions`) into the same shape
   `_parse_backlog_task_from_memory` returns (`{"priority", "title",
   "instructions"}`). Wire it into `_write_materialized_improvement_artifact`'s
   existing fallback chain as a new tier, between MEMORY.md and todo.md:
   `MEMORY.md → goal_text.json → todo.md → research feed`. This slots into an
   existing, already-tested mechanism — no new dispatch code needed.
2. In `_switch_off_stalled_lane`, sort the candidate `tasks` list before
   calling `pick_alternative_task` so that backlog-progression task IDs
   (`synthesize-next-improvement-candidate`, `materialize-pass-streak-improvement`,
   `MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID`, `subagent-verify-materialized-improvement`)
   sort ahead of pure bookkeeping (`refresh-approval-gate`, `run-bounded-turn`,
   `record-reward`, etc.). `pick_alternative_task`'s signature/contract is
   unchanged — only the order of the list passed to it changes.

## Acceptance

- [ ] `_parse_backlog_task_from_goal_text` correctly parses the current
      on-host `goal_text.json` shape into `{"priority": 5, "title": "Write
      scripts/cycle_logger.py", "instructions": "..."}` (unit test with the
      real host format as fixture).
- [ ] `_write_materialized_improvement_artifact`, given an exhausted
      MEMORY.md and a populated `goal_text.json`, produces a
      `next_bounded_candidate` sourced from `goal_text.json` rather than the
      research feed (unit test).
- [ ] `_switch_off_stalled_lane` prefers a backlog-progression task ID over a
      bookkeeping task ID when both are present in the candidate list on
      stall (unit test — regression guard: when only bookkeeping tasks
      exist, behavior is unchanged).
- [ ] Full test suite green; deployed to eeepc and verified (dev → test →
      rollout) — at least one subsequent cycle selects/dispatches a
      goal_text.json-sourced task rather than the research-feed fallback.

## Out of scope

- Redesigning `_derive_bounded_tasks_from_plan`'s selectable pool broadly (no
  new task-pool architecture) — this only reorders an existing list and adds
  one new fallback source.
- Fixing `todo.md` staleness or MEMORY.md curriculum content — those are
  pre-existing data-quality issues, not this change's concern.
- Removing/deprioritizing bookkeeping lanes themselves — they remain the
  correct fallback when the backlog (all sources) is genuinely empty (R5/R6).
