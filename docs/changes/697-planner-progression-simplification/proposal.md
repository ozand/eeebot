# Change: simplify the self-evolving task-progression state machine + add an idle backstop

- **change-id:** 697-planner-progression-simplification
- **issue:** #697
- **capability:** `docs/specs/self-evolving-runtime`
- **role / workstream:** role:developer / workstream:runtime

## Problem

The self-evolving loop's task-progression logic (`nanobot/runtime/
cycle_observe.py`, `cycle_planning.py`, `cycle_feedback.py`,
`cycle_persist.py`) has stalled repeatedly — each time in a **new** shape,
each requiring a point-fix that only revealed the next layer underneath:

- **#656**: the verify lane never retired on a clean bridge completion
  (stale result-file correlation), so `should_retire_subagent_lane` never
  tripped and the loop sat on `subagent-verify-materialized-improvement`
  forever.
- **#664**: once the chain (synthesize→materialize→verify) completed, no
  branch in `_derive_feedback_decision` re-opened it — "done" tasks are
  never re-selected as current, so the loop rotated the bookkeeping-only
  `CORE_TASK_IDS` (`refresh-approval-gate`/`run-bounded-turn`/
  `record-reward`) indefinitely.
- **#690**: the synthesize step drew from a finite candidate list with dead
  and circular fallbacks, so novelty eventually exhausted itself even when
  the state machine's plumbing was otherwise healthy.
- **#695**: the restart added by #664 depended on a fragile **two-cycle
  memory** (`post_materialization_reward_already_confirmed`, a same-task
  `record-reward → record-reward` decision read back on the next cycle) —
  and R11's stall-switch (`cycle_persist._switch_off_stalled_lane`) treats
  any same-task decision as evidence of a stalled lane and overwrites it
  via `pick_alternative_task` *before* the second cycle can land, permanently
  erasing the confirmation.
- **Now (the live gap this proposal starts from):** #695's fix
  (`cycle_feedback.py:777-828`) reopens the chain in one step *if* the
  persisted status of `subagent-verify-materialized-improvement` is already
  `COMPLETED_TASK_STATUSES` (`_chain_complete_for_reward_check`,
  `cycle_feedback.py:794-798`). That persisted status is written in exactly
  one place, `should_retire_subagent_lane`
  (`cycle_planning.py:1825-1893`), gated on `current_task_id ==
  "subagent-verify-materialized-improvement"`
  (`cycle_planning.py:1826`). When the materialize→verify handoff is
  bypassed by the `repeated_synthesized_materialization_completion`
  shortcut (`cycle_planning.py:1675-1730`, which routes materialize
  completion straight to `record-reward` without ever making verify
  *current*), the verify task's status field is stuck at `"pending"`
  forever — `_chain_complete_for_reward_check` is permanently false, and
  the loop falls into the #695 fallback branch
  (`cycle_feedback.py:829-847`), which returns the exact same-task
  `record-reward → record-reward` shape #695 was meant to eliminate, one
  layer up. It is confirmed live: the synthesize→materialize→verify chain
  is complete, `_has_live_verify_request_queue` returns `False`
  (nothing pending, nothing in flight), yet the generation restart does
  not fire.

Each fix so far has patched the newly-discovered instance of the same
underlying shape: **a "chain complete" check keyed on persisted,
single-writer task-record status rather than live state**, combined with
**a same-task decision used as a fragile cross-cycle memory that R11's
stall-switch treats identically to a genuine stall and can erase before it
resolves.** `design.md` documents three independent implementations of
"is this chain complete" (`cycle_feedback.py:794-798`, `:1007-1011`,
`:1226-1230`) and two independent definitions of "reward already
confirmed" (`:689-699` vs `:1188-1200`) — this is product-simplicity debt,
not a one-off bug.

## Intended change

This issue is the **design pass only** — no runtime code changes. It
produces `design.md`, which:

1. Maps the current machine exhaustively (every task_id, status, feedback
   mode, transition — with file:line citations) so the next implementer
   works from ground truth, not memory.
2. Pins the exact current record-reward→restart gap and enumerates every
   terminal sink / erasable-confirmation deadlock in today's machine
   (including the ones already fixed, to show the recurring shape).
3. Proposes a simpler progression model built on **one deterministic
   driver function** and **one hard invariant** — the loop always advances
   toward generating or doing new productive work; no state is a
   permanent sink; no decision depends on multi-cycle memory a stall-switch
   can wipe — and shows, for each historical stall, why it becomes
   structurally impossible under the new model.
4. Specifies an **idle backstop**: a cycles-since-last-productive-spawn
   counter that force-restarts generation if it exceeds N, independent of
   whatever the state machine's own logic concludes — a liveness net that
   makes the entire stall class self-healing even if a future refactor
   reintroduces a sink the driver function doesn't yet cover.

Implementation (editing `cycle_planning.py`/`cycle_feedback.py`/
`cycle_persist.py`/`cycle_observe.py` to match the new model) is **out of
scope for this issue** and follows as a separate, reviewed PR once the
design is accepted.

## Goals / non-goals

**Goals:** eliminate the recurring-stall *class* (not just the current
instance); collapse the scattered/duplicated decision branches into one
driver; add a backstop that is correct even if the driver still has a gap
we haven't found yet; preserve existing safety behavior (R5/R6 bookkeeping
fallback, R11 stall detection as a *signal*, the #690 open-ended
hypothesis generator, the #653/#678/#686 bounded-gate and consumption-grace
mechanics).

**Non-goals:** rewriting the subagent dispatch/materialization mechanics
themselves; changing the bounded-turn/approval-gate CORE bookkeeping
lanes' purpose; introducing new external infrastructure for the backstop
counter (it must reuse existing persisted state); touching
`docs/specs/*` or `memory/HISTORY.md` in this pass (design only).

## Acceptance

- [ ] `design.md` presents a complete current-state map with file:line
      citations for every task_id, status, and feedback-decision branch.
- [ ] `design.md` pins the exact record-reward→restart gap (the branch,
      the guard condition, why #695's restart isn't reached) and lists all
      terminal sinks in today's machine.
- [ ] `design.md` proposes a single-driver simplified model with the
      always-advances invariant, and shows each historical stall
      (#656/#664/#690/#695 + the current one) is structurally impossible
      under it.
- [ ] `design.md` fully specifies the idle backstop: N and its
      justification, where the counter is persisted, what resets it, and
      where the force-restart check sits in the decision path.
- [ ] `design.md` includes a migration/compat section (how #653/#678/#686
      behavior is preserved) and a test plan for the eventual
      implementation PR.
- [ ] Reviewed by the operator before any implementation PR is opened.

## Out of scope

- Any change to `nanobot/runtime/*.py` or `tests/*` in this PR — docs only.
- Redesigning the subagent materializer, bridge, or gate mechanics.
- Backfilling or replaying past stalled cycles on the host.
