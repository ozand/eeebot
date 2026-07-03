# Self-Evolving Runtime — spec

_Status: current. Last updated: 2026-07-04._

## Purpose

The self-evolving runtime is the bounded autonomous engineering operator at the
core of the eeebot **product**. Each cycle it observes durable state, selects one
bounded task, runs an experiment, and writes durable proof before any change is
promoted. It behaves like a bounded engineering operator — orchestrator, planner,
evaluator, bounded-executor manager, evidence-producing control plane — not like
an open-ended chat session. The learning signal is the HADI arc
(Hypothesis→Action→Data→Insight), where each Insight shapes the next Hypothesis.

> This is **product** behavior. How *we* develop this product is in `AGENTS.md` /
> `CONSTITUTION.md`. Explanatory detail is in `docs/ARCHITECTURE.md`,
> `docs/SYSTEM_OPERATION_REFERENCE.md`, and `docs/OBSERVABILITY.md`.

## Requirements

### Cycle contract
- R1. Each cycle SHALL run the model `Observe → Reframe → Specify → Execute →
  Evaluate → Persist`.
- R2. Each cycle SHALL run under a bounded budget (`max_requests`, `max_tool_calls`,
  `max_subagents`, `max_timeout_seconds`, `mutation_scope`) and SHALL NOT silently
  widen scope mid-cycle.
- R3. Every cycle SHALL write durable evidence under the state root:
  `state/reports/evolution-<ts>-<cycle_id>.json` (top-level `result_status` ∈
  `PASS|BLOCK|CRASH`; `experiment.outcome` ∈ `keep|discard|blocked|crash`;
  `changed_files`; `promotion.readiness`), `state/goals/current.json`, and a
  `state/promotions/` candidate when a change is ready to graduate.
- R4. Every experiment SHALL end with exactly one outcome (`keep|discard|blocked|
  crash`), evaluated against a baseline, a current value, and a frontier/best-so-far.
  `blocked`, `crash`, and `discard` SHALL be treated as distinct, never conflated.

### Learning (HADI Insight → next Hypothesis)
- R5. When the active backlog is empty, the next hypothesis SHALL be derived from
  accumulated insights/lessons or a metric delta — not from a hardcoded template.
- R6. "Backlog empty" SHALL NOT be a terminal stall state while actionable insights
  or metric deltas exist.

### Evidence / observability
- R7. From durable state alone, the runtime SHALL be able to answer: active goal,
  current blocker, backlog hypotheses, why the current task was selected, what
  subagents are doing, the measurable result, and the outcome. (See
  `docs/specs/observability/spec.md` once written; `docs/OBSERVABILITY.md` today.)
- R8. The runtime SHALL NOT report narrative progress as material progress; a cycle
  with no file change SHALL NOT be presented as a kept improvement.
- R25. Evidence checks that gate reward or promotion (e.g. "does a concrete code
  change exist") SHALL fail CLOSED: a git-probe error, a non-git workspace, or any
  other inability to verify SHALL be treated as "no evidence", never as "evidence
  present". A materialize-lane reward bonus SHALL require a verified commit
  timestamped at or after the cycle start; a promotion candidate SHALL NOT be
  minted with both `base_commit` and `candidate_patch_hash` null for a
  materialize-lane origin that has no verified diff.

### Roles
- R9. The coordinator SHALL maintain goal alignment, backlog/prioritization,
  experiment contracts, subagent launches, evaluation, and durable state — and SHALL
  run with `ALLOW_CODE_EDITS=false` (it does bookkeeping, not code edits).
- R10. Subagents SHALL execute bounded tasks only, remain correlated to the parent
  goal/cycle/task, and return concrete artifacts — not invent broader mission scope.

### Termination and progress
- R11. The runtime SHALL track consecutive stalled cycles and, on reaching a
  bounded threshold (default 2), SHALL stop the active goal/lane and record
  `stop_reason="no_progress"` — it SHALL NOT continue iterating. A cycle is
  **stalled** when at least one observable signal holds: the same blocker
  repeats, the cycle produced no `changed_files`, or the verifier/evaluation
  result is unchanged with no frontier movement.
- R12. A failed gate (e.g. the smoke gate) SHALL be revised at most a bounded
  number of times (default 3) before the experiment ends with
  `experiment.outcome="blocked"`; the revision count SHALL be recorded and
  revisions SHALL NOT be unbounded.
- R13. A cycle/lane SHALL terminate on an explicit, enumerated stop condition
  and SHALL record which one in `stop_reason`: `gate_clean` (experiment reached
  `keep`/`discard`), `max_iterations`, `no_progress` (R11), or `budget_<name>`
  (any R2 cap exceeded). Termination SHALL NOT rely on budget exhaustion alone.

## Autonomous subagent operating directive

This is the directive given to subagents spawned by the coordinator on the host
(relocated here from `AGENTS.md`, where it did not belong — it is product runtime
behavior, not our dev process).

A spawned subagent is there to **implement, not just review**. If the artifact it
is asked to verify is metadata-only (no file change, no measurable improvement), it
SHALL make the improvement itself:

1. Pick a complete logical task/capability from the runtime backlog (not a micro-step)
   and implement it end-to-end.
2. Write or edit the file(s).
3. Run a quick smoke check on the changed files (import/syntax; full pytest is not
   required for the gate).
4. Commit on the cycle branch (`selfevo/cycle-<id>`), not directly on `main`.
5. The bridge integrates the cycle branch to `main` only after the smoke gate passes.
6. Append a one-line entry to `memory/HISTORY.md`; update `memory/MEMORY.md` if a
   durable lesson was learned.

A session that ends with no edit when a concrete bounded task existed is a FAILURE,
not a success.

## Scenarios

### Scenario: bounded cycle produces durable proof
- Given an active goal and a non-empty bounded budget
- When a cycle runs
- Then `state/reports/evolution-*.json` is written with a single `result_status` and
  `experiment.outcome`, and `state/goals/current.json` reflects the active goal/task.

### Scenario: empty backlog does not stall
- Given the active backlog is empty and ≥1 fresh actionable insight exists
- When the coordinator forms the next hypothesis
- Then the hypothesis is derived from that insight (its title/acceptance reflect the
  insight content), not from a generic template.

### Scenario: subagent that finds metadata-only work still produces a change
- Given a subagent is dispatched to verify a materialized improvement that has no
  file change
- When the subagent runs under an open approval gate
- Then it implements a concrete bounded improvement and commits it on the cycle
  branch, rather than reporting "nothing to do".

### Scenario: stalled loop stops instead of spinning
- Given two consecutive cycles whose deliveries have no `changed_files` and the
  same blocker
- When the next cycle would start
- Then the runtime stops the lane and writes `stop_reason="no_progress"` with
  `stall.consecutive >= 2`, rather than running another bookkeeping cycle.

### Scenario: a failing gate is not retried forever
- Given a subagent change that fails the smoke gate
- When it has been revised the bounded number of times (default 3) without
  passing
- Then the experiment ends with `outcome="blocked"` and `experiment.revisions`
  records the attempts.

### Scenario: every stop has a recorded reason
- Given any terminating cycle/lane
- When the cycle report is written
- Then `stop_reason` is set to exactly one enumerated value, so R7 can answer
  "why did the loop stop" from durable state alone.

### Scenario: an unverifiable evidence check never fakes a passing result
- Given a materialize-lane cycle where the git-probe used to detect a concrete
  change errors, or the workspace is not a git repository
- When the coordinator evaluates whether to award the reward bonus or mint a
  promotion candidate
- Then the check returns "no evidence" (fails closed), the 1.2 reward bonus is
  NOT applied, and no promotion candidate is created with null `base_commit`/
  `candidate_patch_hash` — matching issue #565.

## References

- External reference (design input, not a dependency):
  [`ksimback/looper`](https://github.com/ksimback/looper) `loop.yaml`
  (`loop_control.no_progress`, `gates.*.max_revisions`, `stop_conditions`).
- Reference docs: `docs/ARCHITECTURE.md`, `docs/SYSTEM_OPERATION_REFERENCE.md`,
  `docs/OBSERVABILITY.md`, `docs/EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE.md`.
- Code: `nanobot/runtime/coordinator.py` (`run_self_evolving_cycle`),
  `nanobot/runtime/subagent_materializer.py`, `nanobot/runtime/state*.py`,
  `scripts/eeepc_self_evolving_subagent_bridge.py`.
- Related specs: `subagent-bridge`, `promotion-and-release`, `observability`,
  `model-routing`.
