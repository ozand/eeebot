---
title: Bound planner finalization by remaining time inside a reserved shared budget
status: proposed
date: 2026-10-28
authors: [ozand, delegated delivery]
related: ["#2050", "#1899", "#1925", "ADR-031"]
tags: [runtime, planning, deadlines, budget]
---

# Status

Proposed design amendment under #2050. Planner target P=6000s is confirmed. This record does not accept a budget, authorize implementation, change a host unit, or authorize host activation or rollout. The source opt-in below is not evidence of service capacity approval. Independent exact-revision review and an authorized technical decision are required before implementation.

# Context

At PR #2071 head `c125c233146e6761c546fd9014b01dde80ab38fc` (base `9ebd3ccebdf734f8a8e9c7b8553e8c96123f7bda`), `bridge.py` waits 600s for planning; `SubagentManager` reserves its final model turn by iteration count. The draft raises shared wall default from 3000s to 3600s and introduces a local deadline, but does not implement confirmed P=6000s.

The bridge shared monotonic deadline is anchored at process start, before planning. At current main `b6a0cc8a52e236b6c80ea36d57491d02e09beaf2`, `session_clock.py` defaults shared wall to 3000s, call p99 budget C to 243s, and final/gate budget F to 300s. The existing guard requires remaining shared time >= configured C+F before a call. C is statistical, not a hard latency guarantee. Planner starts after bridge work, so available time is less than total shared wall. The existing PR branch carries an older unmerged 3600s shared default and 1200s planner wait; the opt-in OFF path must restore main's 3000s/600s behavior, not preserve those unmerged defaults.

The #2050 budget receipt proposes retaining executor allocation E=3000s, already including F=300s, plus planner allowance P=6000s: software bridge budget B=P+E=9000s. No executor-duration or shutdown sample supports reducing E. Additional systemd headroom M=300s (S=9300s) is unmeasured and separately reserved; this ADR does not set S. Current main's planner wait is 600s and shared-wall default is 3000s; a planner-phase opt-in must be OFF by default and leave that behavior unchanged. `NANOBOT_SUBAGENT_WALL_SECS` is an existing total-budget setting, not feature consent, and must retain its legacy semantics for every value. This is not proof that the deployed service envelope can support the proposed value.

# Decision

Propose one dedicated boolean `SELFEVO_PLANNER_PHASE_BUDGET_ENABLED`; no existing setting in the bounded configuration search represents explicit consent to change planner timeout/final-turn behavior. Only exact lowercase `true` enables the feature; absent or exact lowercase `false` leaves it OFF and preserves current main's 600s planner wait, iteration-based final turn, and existing shared wall budget behavior, including any explicit `NANOBOT_SUBAGENT_WALL_SECS` value. Any other value returns a bounded configuration error before provider call or planner spawn and never enables the feature. Do not set this flag in the unit, release scripts, or default configuration. The flag is source behavior selection only, not host approval or evidence of service capacity. When ON, require the existing total-budget setting to parse as a finite value exactly 9000s; absent, malformed, non-finite, or any other value fails closed before provider call/spawn rather than silently raising or weakening the budget. Keep `NANOBOT_SUBAGENT_WALL_SECS` a total-budget setting, never a consent signal. Set `Dshared=t0+B` from the bridge-start monotonic anchor with B=9000s; at planner start `tp`, `Dplanner=min(tp+P, Dshared-E)`. Pre-planner work reduces planner allowance, never E. Preserve P=6000s, E=3000s (including F=300s), and B=9000s; do not count F twice.

Planner finalization is selected by remaining time, not iteration count alone. Let `R=Dplanner-now` and `C=get_call_p99_secs()` (configured value; default 243s). `C` is a local final-call opportunity, not a hard latency bound. Admit the tools-disabled final turn only when `R >= C` and the unchanged shared guard passes (`shared_remaining >= C+F`, where `F=get_final_budget_secs()`, default 300s). Bound its call timeout by the minimum of `C`, `R`, watchdog remaining, and shared remaining; this bounds the call to `Dplanner` but cannot guarantee provider completion, and no F reserve is subtracted from P.

A normal planner call is admitted only when the shared guard passes and `R >= 2C`: one C-sized opportunity for this call plus C for finalization. Clamp the in-flight call timeout to `min(C, R-C, watchdog_remaining, shared_remaining)` so ordinary work cannot consume the local final-call window. Bound each planner tool operation to `min(C, R-C, watchdog_remaining, shared_remaining)`; after the operation re-evaluate the deadline and enter finalization only if at least C remains. If another ordinary call cannot fit within R-C, switch to the final turn when R>=C and the shared guard passes; otherwise stop with the existing bounded no-plan/failure outcome. Check this final transition before rejecting ordinary progress at the shared guard, but never bypass the shared guard for a final call.

At `Dplanner`, at least E remains before `Dshared`; E=3000s contains the single global F=300s reserve. Do not subtract F from P or add another reserve. If configured C+F exceeds shared time available for a call (including when E<C+F at the planner boundary), fail closed under the shared guard; do not weaken it or invent configuration. Preserve its strict boundary: it stops only when remaining shared time is less than C+F. C is statistical, not a completion guarantee; a provider/tool may overrun despite timeout/cancellation, causing the plan to be lost. Invalid budgets fail closed; do not borrow E or extend B. Existing validated JSON and harness persistence remain the only route to a plan; timeout/cancellation retains existing no-plan semantics. Executor behavior otherwise stays unchanged. No unit S, host change, model/token/thinking change, telemetry schema, collector, H1/H2, forced-proposal measurement, or #2036 scope change.

# Consequences

### What gets easier

The planner receives a bounded allowance and finalization opportunity based on elapsed time while preserving executor and final/gate allocation under one bridge-start monotonic deadline.

### What gets harder

Pre-planner work reduces planner time; the confirmed target requires a larger software envelope than current main defaults. Configured p99 is not a hard upper bound; cancellation-resistant operations may overrun. Host `TimeoutStartSec` adequacy remains unmeasured and separately governed, so activation must remain gated until service capacity is verified.

### What does not change

No executor allocation reduction, extra reserve, shared-guard bypass, model/token/thinking changes, host/unit action, or deployed-behavior claim. Existing plan validation, persistence, and failure semantics remain authoritative.

# Alternatives considered

### Give planner the entire shared deadline

Rejected: it could consume E and F, removing protected executor/gate time.

### Keep fixed 600-second wait and iteration-only final turn

Rejected: neither implements P=6000s nor ensures the final call begins with sufficient time.

### Use fixed T−120 final window

Rejected: default C is 243s, so 120s is too short. The final-call opportunity is configured C; the 300s F reserve belongs only to E after the planner deadline.

### Apply the global C+F guard to the planner-local deadline

Rejected: F belongs to E after `Dplanner`; applying it before `Dplanner` would double-allocate F and reduce the approved P=6000s planner phase. The planner-local guard uses C; the independent shared-deadline guard continues to use C+F.

### Bypass shared p99 guard for finalization

Rejected: finalization is still a model call; bypassing may consume F.

### Reduce E to fit current bridge wall

Rejected: no executor/shutdown data supports reducing E; it already includes F=300s.

### Keep the 3600s branch default while planner opt-in is off

Rejected: that default exists only on the unmerged draft branch and is incompatible with current main's published 3000s budget and 3300s source unit limit. The OFF path must match current main; a new release must not silently activate the larger envelope.

### Set P=6000, E=3000, B=9000 and M=300/S=9300

Retain P/E/B as proposed software values; do not set M/S here. M is unmeasured and unit changes remain separately reserved.

# Test Contract

Proposed only; tests below are not claimed to exist or pass. Before acceptance, map claims to real named tests citing ADR-045 or explicitly defer under repository policy. Use fake monotonic clock/provider fixtures, not live providers.

| Decision claim | Planned test | Currently |
|---|---|---|
| Dedicated boolean absent/false preserves legacy behavior including wall setting 9000; malformed boolean errors before provider/spawn; enabled mode validates exact compatible total budget | `tests/test_session_clock.py::test_planner_phase_opt_in_validation_and_deadline_partition` | passing |
| Default remains 3000s and phase math preserves P=6000/E=3000 including F only once; pre-planner elapsed reduces planner deadline | `tests/test_session_clock.py::test_default_bridge_wall_budget_is_3000_seconds`, `tests/test_session_clock.py::test_planner_phase_opt_in_validation_and_deadline_partition` | passing |
| Enabled bridge wiring passes planner deadline 6000 with B=9000/E=3000; legacy path retains 600s | `tests/test_run_planning_session.py::test_planning_session_uses_opted_in_6000_second_phase_deadline`, `tests/test_run_planning_session.py::test_planning_session_uses_600_second_legacy_timeout_by_default` | passing |
| Final admission needs local C and shared C+F; with B=9000, E=3000, Dplanner=6000 and C=243, a final turn at 5757 is bounded to 243s and leaves E intact | `tests/test_subagent_manager.py::test_planner_final_turn_starts_at_local_call_reserve_without_tools` | passing |
| Shared reserve incompatibility, malformed/non-finite total budgets, and boolean errors fail closed without changing legacy budget parsing; local C and shared C+F guards retain separate boundaries | `tests/test_session_clock.py::test_planner_phase_opt_in_validation_and_deadline_partition`, `tests/test_session_clock.py::test_planner_wall_guard_uses_local_call_c_while_shared_guard_stays_c_plus_f` | passing |
| Final admission has a C-sized timeout at Dplanner-C; normal work cannot consume that local window; a planner entering at Dplanner makes no provider call; executor profile remains unchanged | `tests/test_subagent_manager.py::test_planner_final_turn_starts_at_local_call_reserve_without_tools`, `tests/test_subagent_manager.py::test_opted_in_planner_entry_too_late_stops_without_provider_call`, `tests/test_subagent_manager.py::test_executor_declared_and_registered_tool_names_match` | passing; no separate cancellation-resistant tool test |
| Only validated plan JSON reaches existing harness diary; opted-in deadline expiry cancels and records timed_out without diary persistence; valid plan still uses the existing writer | `tests/test_run_planning_session.py::test_opted_in_planner_deadline_timeout_records_no_plan_without_diary`, `tests/test_run_planning_session.py::test_planning_session_uses_opted_in_6000_second_phase_deadline`, `tests/test_run_planning_session.py::test_happy_path_writes_the_diary_and_journals_success` | passing

The explicit-off test includes `SELFEVO_PLANNER_PHASE_BUDGET_ENABLED=false` with `NANOBOT_SUBAGENT_WALL_SECS=9000` and verifies legacy planner behavior while that shared wall value remains honored. A normal-call/tool timeout and cancellation-resistant-operation adversarial test, plus an opted-in timeout/cancellation bridge-harness integration test, remain required before the corresponding ADR claims can be marked fully verified.

# References

- #2050 design-only authorization: https://github.com/ozand/eeebot/issues/2050#issuecomment-6464219856
- #2050 budget receipt: https://github.com/ozand/eeebot/issues/2050#issuecomment-6142430759
- #2050 source-preparation receipt: https://github.com/ozand/eeebot/issues/2050#issuecomment-6074015927
- PR #2071 draft head `c125c233146e6761c546fd9014b01dde80ab38fc`, base `9ebd3ccebdf734f8a8e9c7b8553e8c96123f7bda`; historical branch state, not current main.
- Current main inspected at `b6a0cc8a52e236b6c80ea36d57491d02e09beaf2`: `nanobot/runtime/session_clock.py`, `nanobot/runtime/bridge.py::_run_planning_session`, `nanobot/agent/subagent.py`.
- ADR-031, ADR-035, and shared wall-clock safety contract from #1899.

The explicit-off test must include `SELFEVO_PLANNER_PHASE_BUDGET_ENABLED=false` with `NANOBOT_SUBAGENT_WALL_SECS=9000` and prove planner behavior remains legacy while the configured shared wall is still honored.
