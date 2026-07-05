# Change: mutable surface + tool-harness phase 2 (edit/write)

- **change-id:** 603-mutable-surface-phase2
- **issue:** #603 ("bound self-evolution blast radius: fixed harness + one
  mutable surface + enforced rollback")
- **capability:** `docs/specs/subagent-bridge/spec.md` (harness R17-R25),
  `docs/specs/self-evolving-runtime/spec.md` (stop-guards R11-R13)
- **role / workstream:** runtime / self-improvement

## Context

Phase 1 of the tool-harness (#643) shipped read-only tools (`read`/`grep`/`ls`)
confined to a workspace root, gated by one `before_tool_call` veto hook, tool
calls journaled to `state/subagents/tool_calls/<request_id>.jsonl`
(`nanobot/runtime/tool_harness.py`, live on the host). #643's design gated
phase 2 (`edit`/`write`) on "autonomy-surface sign-off": mutation must not
land without a bound on what it can touch.

Separately, #603 is an idea-backlog entry (from the agents_library research
KB, catalogs/patterns.md Card 12 and
agents/a-evolve/architecture/system-architecture-deep-dive.md) proposing a
"fixed harness + one mutable surface + enforced rollback" shape for bounding
self-evolution blast radius.

**Operator directive (2026-07-05, on #603):** these ship as ONE capability.
The harness's single veto seam is the natural enforcement point for a
mutable-surface boundary; the bridge's cycle-branch-isolation-and-smoke-gate
contract (`docs/specs/subagent-bridge/spec.md` R8-R15) is the git-backed
accept/rollback authority #603 asks to harden. Edit/write without a bound, or
a bound with no tools to apply it to, both miss the point.

**Load-bearing finding (verified against running code):** R8-R9 and R12-R15
of `docs/specs/subagent-bridge/spec.md` describe cycle-branch isolation
(`selfevo/cycle-<id>`) and integrate-only-on-green-gate as already true. They
are not. `nanobot/runtime/bridge.py` has no `_setup_cycle_branch`,
`_integrate_cycle_to_main`, or `_cleanup_cycle_branch` — the subagent commits
and the bridge auto-pushes to `origin/main` directly, *before* the smoke gate
runs. `_run_smoke_tests` runs the full `pytest tests/` suite, not an
import-smoke check of the changed files (contra R10). On repeated smoke
failure, `stop_guards.revision_outcome` records `result_status="blocked"` in
the result JSON — a narrative field only; no git rollback happens and the
failing commits stay live on `origin/main`. `design.md` designs the gap-fill
this requires before edit/write autonomy ships.

## Intended change

Ship phase 2 (`edit`/`write`, confined workspace) together with:

1. A per-request `mutable_surface` path-prefix allowlist declared in the
   request artifact, enforced in the existing `before_tool_call` veto hook —
   no new policy engine.
2. A hardcoded protect-list (harness, stop-guards, bridge, specs, CI,
   packaging) `mutable_surface` can never widen into.
3. An enforced-rollback design closing the gap above: on gate failure, the
   cycle's commits are kept off `main` as a git operation, recorded with a
   stop reason in the result artifact.
4. Journal/result-artifact extensions so every edit/write is reviewable via
   its audit unified diff.

## Sequencing / preconditions

- Phase 1 (#643) is shipped and stable.
- The rollback gap-fill (item 3) is a **precondition for enabling edit/write
  on any live request**, not a parallel workstream — mutation with no working
  rollback is strictly worse than today's read-only harness. It lands in the
  same PR/rollout, gated behind a new `tool_harness_mutate` profile opt-in;
  no existing request path is affected.
- Design sign-off (this PR) precedes any tool-mutation code, per #643's
  existing phase-2 gating rule.

## Acceptance

- [ ] `mutable_surface` shape defined; edit/write outside workspace-root ∩
      mutable-surface is vetoed, never an exception.
- [ ] Protect-list entries are vetoed even when nominally inside a declared
      `mutable_surface`.
- [ ] A mutation-enabled request with no `mutable_surface` declared is
      rejected up front (documented, justified default).
- [ ] Enforced rollback: a failed gate deterministically leaves the cycle's
      changes off `main` via a git operation, with a recorded stop reason.
- [ ] Every successful edit/write is journaled with its audit unified diff.
- [ ] Adversarial test plan (surface escape, protect-list bypass via
      symlink/near-miss prefix, rollback-on-failed-gate) passes.
- [ ] `docs/specs/subagent-bridge/spec.md` is corrected/extended to match
      verified running behavior once this ships.

## Out of scope

- Command/bash execution (phase 3 of #643) — stays gated, unimplemented.
- Any change to promotion-gating semantics beyond the rollback gap-fill
  above (no new promotion authority, no smoke-gate bypass).
- A configurable/pluggable protect-list — it is code, changed only by a
  human PR.
- Retrofitting cycle-branch isolation into the pre-existing direct-
  `SubagentManager` bridge path — this proposal fixes the gap only for the
  new mutation profile; that broader refactor is a candidate follow-up
  Issue (see design.md open questions).

story_id: docs/specs/subagent-bridge/spec.md
