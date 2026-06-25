# Spec delta — self-evolving-runtime: cycle stop-guards

This is the **delta** to apply into `docs/specs/self-evolving-runtime/spec.md`
when this change lands. It adds R11–R13 and three scenarios; it changes nothing
existing.

## Add under `## Requirements`, after the "Roles" subsection

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

## Add under `## Scenarios`

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

## Add to `## References`

- External reference (design input, not a dependency):
  [`ksimback/looper`](https://github.com/ksimback/looper) `loop.yaml`
  (`loop_control.no_progress`, `gates.*.max_revisions`, `stop_conditions`).
