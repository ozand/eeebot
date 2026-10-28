
# Consequences

### What gets easier

A slow planner can spend its bounded planner allowance on useful work and receive a finalization opportunity based on elapsed time, while the executor and existing final/gate reserve retain an explicit allocation. The shared monotonic deadline remains the outer authority.

### What gets harder

Planning may end early when pre-planner work consumes its allocated phase, and the 6000-second target requires a larger software bridge envelope than current source defaults. The configured p99 margin is a start guard, not a hard latency guarantee; overrunning or cancellation-resistant calls still require bounded cancellation and fail-closed activation compatibility. Host `TimeoutStartSec` adequacy remains unmeasured and separately governed.

### What does not change

No executor allocation reduction, no extra 300-second reserve, no model/token/thinking changes, no host unit edits, no host actions, and no claim of deployed behavior. Existing planner validation/persistence and no-plan semantics remain authoritative.

# Alternatives considered

### Give the planner the entire shared bridge deadline

Rejected: planner calls could consume E and the existing F reserve, leaving no protected executor/gate window. It also contradicts the source’s shared deadline safety contract.

### Keep a 600-second fixed planner wait and iteration-only final turn

Rejected: this does not implement the confirmed 6000-second hypothesis, and an iteration-based final turn may arrive after the wall-clock guard has already stopped progress.

### Allow planner finalization to bypass the shared p99 safety guard

Rejected: a final turn is still a model call, and bypassing the guard can consume time reserved for finalization/gates. No evidence supports weakening that guard.

### Reduce the 3000-second executor allocation to fit the planner inside the current bridge total

Rejected: no executor-duration or shutdown-time measurement supports a smaller E. It would spend the existing 300-second reserve or leave executor completion unsupported.

### Use P=6000, E=3000, B=9000 with M=300 systemd headroom

Retain P/E/B as the software proposal; reject setting M/S here. The 300-second unit headroom is explicitly unmeasured and its unit change is a reserved host/operator action, not source budget authority.

# Test Contract

Proposed only; tests are not yet written or claimed to exist. On any later acceptance, map every claim to existing/new main tests citing ADR-045 or explicitly defer under repository ADR policy.

| Decision claim | Planned test | Currently |
|---|---|---|
| Phase deadlines derive from a shared bridge-start monotonic anchor and preserve E, including reduced planner allowance after pre-planner work | `tests/test_session_clock.py::<planner_phase_budget_tests>` | not yet written |
| Planner final turn is selected by remaining local time and disables tools without depending on iteration cap alone | `tests/test_subagent_manager.py::<planner_time_finalization_tests>` | not yet written |
| Planner model calls and tools cannot exceed the minimum applicable watchdog/planner/shared deadline; expired budget starts no call and never bypasses shared safety margin | `tests/test_subagent_manager.py::<planner_deadline_enforcement_tests>` | not yet written |
| Expiry/cancellation remains no-plan/failure and only validated plan JSON reaches existing harness persistence; executor behavior is unchanged | `tests/test_run_planning_session.py::<planner_timeout_and_executor_compatibility_tests>` | not yet written |
| Budget arithmetic uses P=6000, E=3000 including F=300, and B=9000 without assigning a unit S value | `tests/test_session_clock.py::<planner_budget_allocation_tests>` | not yet written |

# References

- #2050 H3 design-only coordination: https://github.com/ozand/eeebot/issues/2050#issuecomment-6464219856
- #2050 budget decision receipt: https://github.com/ozand/eeebot/issues/2050#issuecomment-6142430759
- #2050 original H3 source-preparation receipt: https://github.com/ozand/eeebot/issues/2050#issuecomment-6074015927
- Existing draft PR #2071, head `c125c233146e6761c546fd9014b01dde80ab38fc`, base `9ebd3ccebdf734f8a8e9c7b8553e8c96123f7bda` (historical source, not a current-main review).
- `nanobot/runtime/session_clock.py`; `nanobot/runtime/bridge.py::_run_planning_session`; `nanobot/agent/subagent.py` deadline and final-turn loop.
- ADR-031 and ADR-035; existing shared wall safety contract from #1899.
