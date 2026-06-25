# Self-Evolving Runtime — spec

_Status: current. Last updated: 2026-06-24._

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

### Roles
- R9. The coordinator SHALL maintain goal alignment, backlog/prioritization,
  experiment contracts, subagent launches, evaluation, and durable state — and SHALL
  run with `ALLOW_CODE_EDITS=false` (it does bookkeeping, not code edits).
- R10. Subagents SHALL execute bounded tasks only, remain correlated to the parent
  goal/cycle/task, and return concrete artifacts — not invent broader mission scope.

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

## References

- Reference docs: `docs/ARCHITECTURE.md`, `docs/SYSTEM_OPERATION_REFERENCE.md`,
  `docs/OBSERVABILITY.md`, `docs/EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE.md`.
- Code: `nanobot/runtime/coordinator.py` (`run_self_evolving_cycle`),
  `nanobot/runtime/subagent_materializer.py`, `nanobot/runtime/state*.py`,
  `scripts/eeepc_self_evolving_subagent_bridge.py`.
- Related specs: `subagent-bridge`, `promotion-and-release`, `observability`,
  `model-routing`.
