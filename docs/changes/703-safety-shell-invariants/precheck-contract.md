# Per-cycle precheck contract

- **Issue:** #703 (loop-redesign ticket D, stacked on #702's architecture
  decision)
- **Status:** design/contract — no code changed by this document
- **story_id:** docs/specs/subagent-bridge/spec.md ("Immutable safety shell
  (loop-independent)")

## Purpose

The bridge's per-cycle safety shell (frozen in
`docs/specs/subagent-bridge/spec.md`, "Immutable safety shell
(loop-independent)") already enforces every hard invariant — green-only
integration, mutation-surface/no-secret checks, the suite-shrink guard, the
rollback record, the concurrency lock, and stop-guard budgets — but it
enforces most of them **after** a subagent has already been spawned and has
already spent turn/tool budget (integration-time checks in `main()`, R12a).

This contract defines a lightweight **precheck** step that runs *before* a
subagent is spawned, so an obviously-doomed or obviously-redundant proposal is
rejected (or skipped) cheaply instead of burning a full bounded-subagent cycle
only to be blocked at integration time. The precheck is consumed identically
by:

- the current control-plane loop (today),
- the shadow experiment (#706), which evaluates LLM-proposed tasks side by
  side with the current planner under the same shell,
- the eventual replacement core loop (#707), gated on #706 passing.

**This is a design contract, not an implementation.** No new prechecking code
is introduced by this ticket; the contract below is what any of the three
consumers above must implement or reuse when they build/rebuild the
propose-a-task step. Two of its checks already have partial, related
implementations in the bridge today (cited below) that a precheck
implementation should reuse rather than re-derive.

## Relationship to the gate

The precheck is a **pre-filter**, not a second gate. It never replaces the
hard gate described in the "Immutable safety shell" section of
`docs/specs/subagent-bridge/spec.md` — that gate (smoke tests +
`_validate_mutation_surfaces` + the suite-shrink guard, all evaluated inside
`main()` after the subagent runs) remains the **sole hard arbiter** of whether
a cycle's work integrates. The precheck exists only to avoid spending a bounded
subagent turn on a proposal that the gate (or an existing safety check) would
certainly reject or that is already known to be redundant. A precheck pass is
never sufficient for integration; a precheck failure must not be treated as
if the gate had run.

## Inputs

The precheck runs once per cycle, immediately before a subagent would be
spawned (i.e. at the point in `main()` that today calls `mgr.spawn(...)`,
after `find_pending_request`/`_task_already_done` and after
`_setup_cycle_branch`). It takes:

- **`proposed_task.target_paths`** — the file path(s) or path-prefix(es) the
  proposal declares it intends to touch. (Today's bridge does not carry this
  field on a request; a proposal-producing loop that wants this precheck must
  add it to the request/backlog schema — see #704's ledger-schema design.)
- **`proposed_task.title`** — the task/backlog title, the same string
  `_task_already_done` already fuzzy-matches against recent commit subjects.
- **Bridge/repo state**: whether `HEAD` is on `main`, whether the shared
  checkout's tree is clean, and whether the bridge concurrency lock
  (`bridge.lock`, `_acquire_bridge_lock`, R26) is held by this process.
- **The done-ledger** (or, until #704 lands, the git-log proxy
  `_task_already_done` already uses) for the novelty/duplicate check.

## Checks and reject/skip reasons

| # | Check | Outcome | Reason string (suggested) |
|---|-------|---------|----------------------------|
| P1 | `proposed_task.target_paths` has any entry outside the mutable surface (`_ALLOWED_PATH_PREFIXES`: `surfaces/`, `scripts/`, `memory/`, `lessons/`, `docs/`, `tests/`) | **reject before spawn** | `precheck_mutation_surface_violation` |
| P2 | `proposed_task.title` (or its target artifact) matches the done-ledger / `_task_already_done`-style novelty check | **skip** (not an error — proposal already accomplished) | `precheck_duplicate_vs_done_ledger` |
| P3 | Shared checkout `HEAD` is not on `main`, OR the tree is dirty in a way `_restore_to_main` cannot repair, OR the concurrency lock is not held by this process | **abort the cycle** (no spawn, no bookkeeping beyond the existing R27 blocked-result path) | `precheck_head_not_on_main` / `precheck_dirty_tree` / `precheck_lock_not_held` |

Notes:

- P1 is a **precheck-time approximation** of the same rule the gate enforces
  authoritatively via `_validate_mutation_surfaces` at integration time
  (R12a) — the precheck only has the *proposed* target paths, not the
  subagent's actual diff, so it cannot substitute for the gate; a subagent
  could still drift outside its declared paths, and R12a remains the hard
  block that actually protects `main`. P1 existing without R12a would be
  unsafe; R12a existing without P1 is safe but wasteful. Both are required —
  P1 for cost, R12a for safety.
- P2 reuses the shape already implemented by `_task_already_done` (fuzzy
  keyword match against recent non-maintenance commit subjects, 7-day
  window) as a stopgap; once #704's ledger schema exists, P2 should check the
  done-ledger directly rather than approximating it via git log.
- P3 reuses the existing preconditions already enforced in `main()`: the
  HEAD-on-main/clean-tree precondition (R27, `_restore_to_main`) and the
  concurrency lock (R26, `_acquire_bridge_lock`). A loop redesign does not
  need to reimplement these — it needs to keep calling them before it
  proposes/spawns, in the same order the bridge already does.
- A precheck failure is recorded the same way an R27 precondition failure is
  today: a `blocked` result with a `rollback.reason` naming which precheck
  failed, no subagent spawned, no branch created. It is cheap, git-visible,
  and does not count as a gate run.

## Outputs

- **Pass** → proceed to `_setup_cycle_branch` and spawn exactly one bounded
  subagent (S6 in the safety-shell section), unchanged.
- **Reject (P1)** → do not spawn; record a `blocked` result with
  `rollback.reason = "precheck_mutation_surface_violation"`; the proposal is
  discarded, not retried automatically.
- **Skip (P2)** → do not spawn; treat identically to today's
  `_task_already_done` short-circuit (mark done in bookkeeping, no subagent
  turn spent, not an error).
- **Abort (P3)** → do not spawn; behave exactly like the existing R27 path
  (`rollback.reason = "head_on_main_precondition_failed"` today; a precheck
  implementation should use one of the more specific reasons above when it
  can distinguish which precondition failed).

## Non-goals

- The precheck does **not** attempt to predict test outcomes, run any tests,
  or evaluate code quality — that is the gate's job, and only the gate's.
- The precheck does **not** replace `_validate_mutation_surfaces`,
  `_run_smoke_tests_with_shrink_guard`, or any other post-spawn hard check
  enumerated in the "Immutable safety shell" section — it is additive and
  advisory-at-the-margin, hard only insofar as "no spawn happens", never in
  the sense of "this proposal is therefore safe to integrate".
- The precheck is deliberately minimal: over-rejecting legitimate proposals
  defeats the purpose (the loop redesign's whole point is sustained novelty,
  per the #702 decision record). New precheck rules beyond P1-P3 require a
  new issue and an explicit amendment of this contract, not silent expansion.

## Cross-links

- Safety shell this contract sits inside:
  `docs/specs/subagent-bridge/spec.md`, "Immutable safety shell
  (loop-independent)".
- Architecture decision motivating the loop redesign:
  `docs/changes/702-ledger-loop-architecture-decision/decision.md` (§4,
  "Safety shell independence").
- Consumers: #706 (shadow experiment — must run proposals through this
  precheck before treating them as evaluable), #707 (replacement core loop —
  gated on #706, must reuse this same precheck, not invent a new one).
- Ledger schema this contract will eventually read against for P2: #704.
