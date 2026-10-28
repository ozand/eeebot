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

Planner finalization is selected by remaining time, not iteration count alone. Let `R=Dplanner-now`, `C=get_call_p99_secs()` (configured value; default 243s), and `F=get_final_budget_secs()` (configured value; default 300s). Admit the final call only when `R >= C+F`, preserving a configured p99-sized call opportunity plus F; this is not a provider-latency guarantee. When admitted, enter the existing tools-disabled final turn and cap its call timeout by the minimum of watchdog remaining, `R-F`, and shared remaining.

A normal planner call is admitted only if the shared guard passes and `R >= 2C+F`: reserve one C-sized opportunity for this normal call, then C+F for final call and existing reserve. Clamp the in-flight normal-call timeout to `min(C, R-(C+F), watchdog_remaining, shared_remaining)`. After any call/tool, re-evaluate remaining time: when another normal call cannot meet `2C+F`, switch to the final turn if C+F remains, otherwise fail without another call. Bound planner tool execution by the time remaining after preserving C+F; tools may consume part of the normal-call budget but never the final window. Check final-transition/admission before rejecting ordinary progress at the shared guard: finalization is admitted only with the complete C+F local window and when its call itself passes the unchanged shared guard. Never bypass that guard.

If configuration or remaining shared time cannot meet both local admission and shared-guard requirements, return existing bounded no-plan/failure without another call. Preserve the guard's strict boundary (it stops only when remaining is less than the margin). In-flight calls/tools remain cancellable and hard-bounded; cancellation-resistant operations may overrun, so no p99 guarantee is claimed. Since `Dplanner <= Dshared-E`, E including F remains reserved before shared deadline. Invalid budgets fail closed; do not borrow E or extend B. Existing validated JSON and harness persistence remain the only route to a plan; timeout/cancellation retains existing no-plan semantics. Executor behavior otherwise stays unchanged. No unit S, host change, model/token/thinking change, telemetry schema, collector, H1/H2, forced-proposal measurement, or #2036 scope change.

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

Rejected: default C is 243s, so 120s is too short; larger configured C makes the mismatch worse.

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
| Shared/planner deadlines use bridge-start clock; P=6000/E=3000 includes F once; pre-planner work reduces planner allowance | `tests/test_session_clock.py::<planner_phase_budget_tests>` | not yet written |
| Final admission requires configured C+F, including C=243 where T−120 fails and C>243; transition occurs before ordinary guard refusal only when final and shared guards hold | `tests/test_subagent_manager.py::<planner_final_admission_tests>` | not yet written |
| Normal call requires 2C+F remaining and is bounded to C; insufficient-at-entry, configured C exceeding available phase, and shared-guard equality/expired boundaries fail closed without bypass or call | `tests/test_subagent_manager.py::<planner_deadline_boundary_tests>` | not yet written |
| Slow in-flight normal call/tool cannot consume final C+F window; timeout clamps to remaining phase time; cancellation/overrun fails safely | `tests/test_subagent_manager.py::<planner_work_preserves_final_window_tests>` | not yet written |
| Final turn has tools disabled; only validated JSON persists; expiry/cancel yields existing no-plan/failure; executor behavior unchanged | `tests/test_run_planning_session.py::<planner_final_persistence_and_executor_compatibility_tests>` | not yet written |
| E includes F once, B=P+E=9000, invalid allocation cannot borrow E or extend B | `tests/test_session_clock.py::<planner_budget_arithmetic_tests>` | not yet written |

# References

- #2050 design-only authorization: https://github.com/ozand/eeebot/issues/2050#issuecomment-6464219856
- #2050 budget receipt: https://github.com/ozand/eeebot/issues/2050#issuecomment-6142430759
- #2050 source-preparation receipt: https://github.com/ozand/eeebot/issues/2050#issuecomment-6074015927
- PR #2071 draft head `c125c233146e6761c546fd9014b01dde80ab38fc`, base `9ebd3ccebdf734f8a8e9c7b8553e8c96123f7bda`; historical branch state, not current main.
- Current main inspected at `b6a0cc8a52e236b6c80ea36d57491d02e09beaf2`: `nanobot/runtime/session_clock.py`, `nanobot/runtime/bridge.py::_run_planning_session`, `nanobot/agent/subagent.py`.
- ADR-031, ADR-035, and shared wall-clock safety contract from #1899.
