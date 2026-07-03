# Change: close the reward-hacking hole in the self-evolving cycle

- **change-id:** coordinator-reward-hacking-guard
- **issue:** TBD (filed in same session)
- **capability:** `docs/specs/self-evolving-runtime`
- **role / workstream:** role:developer / workstream:runtime

## Problem

The eeepc host produced **zero real git commits for 8 consecutive days**
(2026-06-26 → 2026-07-04) while the coordinator kept reporting `result_status:
PASS` and `reward_signal.value: 1.2` on almost every cycle.

Root cause, confirmed by reading `nanobot/runtime/coordinator.py`:

- `result_status = "PASS"` is set unconditionally whenever `execute_turn()`
  returns without raising (`coordinator.py:4484-4493`) — no check that anything
  was actually produced.
- The materialize-lane reward bonus (`_has_concrete_changes`,
  `coordinator.py:4361-4431`) is meant to gate the 1.2 reward on a real code
  change, but **fails open**: any error probing git returns `True` ("changes
  present") instead of failing closed, and it never verifies the commit
  actually relates to the claimed improvement — any recent commit/dirty file
  with a source extension counts.
- `materialize-synthesized-improvement`
  (`_write_materialized_improvement_artifact`, `coordinator.py:2580-2700+`)
  only ever writes a descriptive JSON artifact to `state/improvements/`. It
  never dispatches a real code-edit request to the subagent bridge.
- Promotion records are created with `base_commit: null` and
  `candidate_patch_hash: null` **unconditionally**
  (`coordinator.py:4543-4568`) — there is no code path that requires or
  computes a real diff before minting a promotion candidate.

This directly violates `docs/specs/self-evolving-runtime/spec.md` R8: *"The
runtime SHALL NOT report narrative progress as material progress; a cycle
with no file change SHALL NOT be presented as a kept improvement."*

Effect observed on host (1841 cycle reports sampled): task selection is
dominated by internal bookkeeping/synthesis task IDs
(`refresh-approval-gate`, `run-bounded-turn`, `record-reward`,
`synthesize-next-improvement-candidate`,
`subagent-verify-materialized-improvement`) — real backlog items from
`goal_text.json` (Priority A/B) were never selected in this window. The loop
is not stalled in the classic sense (R11 stop-guards fire and record
`stop_reason`) — it is **self-rewarding without evidence**, so stall
detection alone cannot see the problem.

## Intended change

Make `PASS` + reward for the materialize lane strictly evidence-gated,
per R8:

1. `_has_concrete_changes` fails **closed**, not open: a git-probe error or
   ambiguous result must count as "no concrete change", not "change present".
2. The bonus reward (1.2) requires a commit whose timestamp is after cycle
   start AND whose diff touches a file relevant to the improvement statement
   — not just "any recent commit exists".
3. Promotion candidates are only minted with a real `base_commit` +
   `candidate_patch_hash` when the origin task claims a code change; a
   materialize-lane cycle with no verified diff SHALL NOT produce a
   promotion candidate at all (rather than one with null diff fields sitting
   in the queue forever).

Out of scope for this change (tracked as follow-up, not blocking): routing
real `goal_text.json` backlog items into the dispatchable task pool, and
having `materialize-synthesized-improvement` actually dispatch a subagent
edit request. Those are separate, larger capability changes — this change
only stops the loop from **rewarding itself for work that didn't happen**.

## Acceptance

- [ ] `_has_concrete_changes` returns `False` (or a distinct
      `unverified`/error state that is treated as "no changes") on any git
      probe error, instead of `True`.
- [ ] The materialize-lane 1.2 reward is only awarded when a real,
      cycle-scoped commit is verified; otherwise reward is not upgraded above
      the PASS/BLOCK baseline.
- [ ] No promotion candidate is created with both `base_commit: null` and
      `candidate_patch_hash: null` for a materialize-lane origin cycle that
      has no verified diff.
- [ ] Unit tests cover: git-probe-error → no bonus; verified commit →
      bonus; no commit → no promotion candidate.
- [ ] Full test suite green; additive/behavior-narrowing only (no new public
      surface).

## Out of scope

- Routing `goal_text.json` Priority A/B backlog items into the dispatchable
  task pool (separate follow-up).
- Making `materialize-synthesized-improvement` dispatch a real subagent
  edit request (separate follow-up).
- General bookkeeping-task reward tuning (`record-reward`,
  `refresh-approval-gate` etc. are legitimate low-reward housekeeping and are
  not changed here).
