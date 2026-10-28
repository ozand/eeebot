---
title: Bound planner finalization by remaining time inside a reserved shared budget
status: proposed
date: 2026-10-28
authors: [ozand, delegated delivery]
related: ["#2050", "#1899", "#1925", "ADR-031"]
tags: [runtime, planning, deadlines, budget]
---

# Status

Proposed design only under #2050. Planner target P=6000s is confirmed. This record does not accept a budget, authorize implementation, change a host unit, or authorize activation or rollout. Independent exact-revision review and an authorized technical decision are required before implementation.

# Context

At PR #2071 head `c125c233146e6761c546fd9014b01dde80ab38fc` (base `9ebd3ccebdf734f8a8e9c7b8553e8c96123f7bda`), `bridge.py` waits 600s for planning; `SubagentManager` reserves its final model turn by iteration count. The draft raises shared wall default from 3000s to 3600s and introduces a local deadline, but does not implement confirmed P=6000s.

The bridge shared monotonic deadline is anchored at process start, before planning. At current main `b6a0cc8a52e236b6c80ea36d57491d02e09beaf2`, `session_clock.py` defaults shared wall to 3000s, call p99 budget C to 243s, and final/gate budget F to 300s. The existing guard requires remaining shared time >= configured C+F before a call. C is statistical, not a hard latency guarantee. Planner starts after bridge work, so available time is less than total shared wall.

The #2050 budget receipt proposes retaining executor allocation E=3000s, already including F=300s, plus planner allowance P=6000s: software bridge budget B=P+E=9000s. No executor-duration or shutdown sample supports reducing E. Additional systemd headroom M=300s (S=9300s) is unmeasured and separately reserved; this ADR does not set S. B is proposed capacity, not verified host capacity.

# Decision

Propose phase partitioning from bridge-start monotonic anchor `t0`: `Dshared=t0+B`; at planner start `tp`, `Dplanner=min(tp+P, Dshared-E)`. Pre-planner work reduces planner allowance, never E. Preserve P=6000s, E=3000s (including F=300s), B=9000s; do not count F twice.

Planner finalization is selected by remaining time, not iteration count alone. Let `R=Dplanner-now` and `C=get_call_p99_secs()` (configured value; default 243s). `C` is a local final-call opportunity, not a hard latency bound. Admit the tools-disabled final turn only when `R >= C` and the unchanged shared guard passes (`shared_remaining >= C+F`, where `F=get_final_budget_secs()`, default 300s). Bound its call timeout by the minimum of `C`, `R`, watchdog remaining, and shared remaining; this bounds the call to `Dplanner` but cannot guarantee provider completion, and no F reserve is subtracted from P.

A normal planner call is admitted only when the shared guard passes and `R >= 2C`: one C-sized opportunity for this call plus C for finalization. Clamp the in-flight call timeout to `min(C, R-C, watchdog_remaining, shared_remaining)` so ordinary work cannot consume the local final-call window. Bound each planner tool operation by the time remaining after preserving local C; re-evaluate at every call/tool boundary. If another ordinary call cannot fit within R-C, switch to the final turn when R>=C and the shared guard passes; otherwise stop with the existing bounded no-plan/failure outcome. Check this final transition before rejecting ordinary progress at the shared guard, but never bypass the shared guard for a final call.

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

### Bypass shared p99 guard for finalization

Rejected: finalization is still a model call; bypassing may consume F.

### Reduce E to fit current bridge wall

Rejected: no executor/shutdown data supports reducing E; it already includes F=300s.

### Set P=6000, E=3000, B=9000 and M=300/S=9300

Retain P/E/B as proposed software values; do not set M/S here. M is unmeasured and unit changes remain separately reserved.

# Test Contract

Proposed only; tests below are not claimed to exist or pass. Before acceptance, map claims to real named tests citing ADR-045 or explicitly defer under repository policy. Use fake monotonic clock/provider fixtures, not live providers.

| Decision claim | Planned test | Currently |
|---|---|---|
| Shared/planner deadlines use bridge-start clock; P=6000/E=3000 includes F only once in E; pre-planner work reduces planner allowance | `tests/test_session_clock.py::<planner_phase_budget_tests>` | not yet written |
| Final admission needs local C and shared C+F; at t0=0, B=9000, E=3000, Dplanner=6000, a final call starting at 5757 with C=243 finishes by 6000 and leaves E intact | `tests/test_subagent_manager.py::<planner_final_admission_tests>` | not yet written |
| Old T−120 behavior fails for C=243; configurable C is used; if shared remaining < C+F (including E<C+F at phase boundary), fail closed without bypass | `tests/test_subagent_manager.py::<planner_deadline_boundary_tests>` | not yet written |
| Normal call/tool timeout preserves local C window; slow work cannot consume it; cancellation/overrun may lose plan without claiming p99 as hard bound | `tests/test_subagent_manager.py::<planner_work_preserves_final_window_tests>` | not yet written |
| Final turn has tools disabled; only validated JSON persists; expiry/cancel yields existing no-plan/failure; executor behavior unchanged | `tests/test_run_planning_session.py::<planner_final_persistence_and_executor_compatibility_tests>` | not yet written |
| E includes F once, B=P+E=9000, invalid allocation cannot borrow E or extend B | `tests/test_session_clock.py::<planner_budget_arithmetic_tests>` | not yet written |

# References

- #2050 design-only authorization: https://github.com/ozand/eeebot/issues/2050#issuecomment-6464219856
- #2050 budget receipt: https://github.com/ozand/eeebot/issues/2050#issuecomment-6142430759
- #2050 source-preparation receipt: https://github.com/ozand/eeebot/issues/2050#issuecomment-6074015927
- PR #2071 draft head `c125c233146e6761c546fd9014b01dde80ab38fc`, base `9ebd3ccebdf734f8a8e9c7b8553e8c96123f7bda`; historical branch state, not current main.
- Current main inspected at `b6a0cc8a52e236b6c80ea36d57491d02e09beaf2`: `nanobot/runtime/session_clock.py`, `nanobot/runtime/bridge.py::_run_planning_session`, `nanobot/agent/subagent.py`.
- ADR-031, ADR-035, and shared wall-clock safety contract from #1899.
