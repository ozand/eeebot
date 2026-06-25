# Design: cycle stop-guards

Source of the three new requirements: a sverka of our
`self-evolving-runtime/spec.md` against the `loop.yaml` model of
[`ksimback/looper`](https://github.com/ksimback/looper) (example
`ai-workflow-mapping`).

## What we already cover (no change)

| Looper `loop.yaml` invariant | Where we already have it |
|---|---|
| `goal.statement` + `context_sources` + `definition_of_done` | R1 (Specify), experiment contract |
| `loop_control.budget` (usd/tokens/wall_clock) | R2 (`max_requests`, `max_tool_calls`, `max_subagents`, `max_timeout_seconds`, `mutation_scope`) — broader |
| `verification` (programmatic) | smoke gate (import/syntax), R3 |
| `execution.side_effects.requires_approval` | apply.ok gate (host-runtime) |
| `observability.state_file` / `run_log` | R3 durable evidence, R7 |
| run outcome | R4 `keep\|discard\|blocked\|crash` vs baseline/current/frontier — stronger than Looper's pass/fail |

We also have capabilities Looper lacks entirely (so nothing to borrow): HADI
learning loop (R5/R6), multi-cycle durable state, promotion/release + rollback,
subagent lifecycle.

## The three gaps → R11/R12/R13

### R11 — no-progress STOP guard
Looper: `loop_control.no_progress.max_stalled_iterations: 2` + enumerated
`signals` (`same blocking issue repeats`, `delivery artifact has no material
change`, `verifier output is unchanged`) + `action: stop`.

We have R8 (no-file-change ≠ kept improvement) and R6 (empty backlog not
terminal), but nothing requires the loop to **stop** when stalled. Add a
normative stall counter in durable state and a STOP action at the threshold.

Stall signals (observable, in durable state):
- same blocker repeats across N consecutive cycles, or
- consecutive cycle with no `changed_files`, or
- identical verifier/evaluation result with no frontier movement.

### R12 — bounded revisions
Looper: `gates.*.verdict_policy: revise_until_clean` + `max_revisions: 3`.

Our smoke gate has no revision cap — a subagent can keep retrying. Add: a failed
gate is retried at most K times (default 3), after which the experiment ends
`blocked` (R4) with the revision count recorded.

### R13 — enumerated stop conditions
Looper: explicit `stop_conditions` list. Ours are implicit (budget exhaustion).
Make the terminating set explicit and normative:
1. all gates clean (experiment reaches `keep`/`discard`),
2. `max_iterations` reached,
3. R11 no-progress guard tripped,
4. any R2 budget cap exceeded.

Each stop terminates with a recorded `stop_reason` in the cycle report so R7 can
answer "why did the loop stop".

## Durable-state surface (checkability)

| Requirement | New/used field in `state/reports/evolution-*.json` |
|---|---|
| R11 | `stall.consecutive`, `stall.signal`, `stop_reason="no_progress"` |
| R12 | `experiment.revisions` (int), `experiment.outcome="blocked"` when cap hit |
| R13 | `stop_reason ∈ {gate_clean, max_iterations, no_progress, budget_<name>}` |

These are additive to the existing R3 report contract — no field is renamed or
removed.

## Why spec-only here

The contract (what STOP means, the thresholds, the stop-reason enum) should be
agreed before touching `coordinator.py`. Implementation is a separate Issue that
references this archived change; CI test coverage lands with the implementation,
not the spec.
