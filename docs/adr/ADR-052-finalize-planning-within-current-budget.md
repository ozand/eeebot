---
title: Finalize planning within the existing 600-second budget
status: accepted
date: 2026-11-06
authors: [delegated delivery]
related: ["#2050", "#2102", "ADR-045"]
tags: [runtime, planning, deadlines]
---

# Status

Provisional acceptance candidate under the owner's delegated technical authority; not implemented or effective. Proposed design revision `571e106cef45537b87903b51df720ce9982d9371` received independent design PASS. This candidate becomes effective only after independent PASS and applicable checks on the exact final status revision followed by authorized merge unchanged. Its PR or issue must retain those receipts. This follow-on amends only ADR-045's OFF-path iteration-only finalization decision; ADR-045's opt-in 6000/9000 contract remains unchanged. No host rollout is authorized here.

# Context

Installed source `6d476b715aa5989d5036e1385f6262d0f2a70f4b` cancels planning after 600 seconds and reserves a tools-disabled turn only at iteration 20. Two observed planner tasks were cancelled near 599 seconds; 31 recorded model responses requested tools. Calls are not reliably joined to those tasks and cancelled in-flight calls may lack terminal rows, so these observations do not establish whether model calls, tools, or other work consumed the time. They do not establish that an earlier final turn will produce a valid plan.

On current main `4d642aba0b8ff64e47c465468a09f6cc89cd5e06`, ADR-045 supplies remaining-time finalization only when its separate 6000/9000 opt-in is enabled. The OFF path has a local 600-second deadline but retains iteration-only finalization.

# Decision

For planner sessions with ADR-045's larger phase opt-in OFF, reuse the existing remaining-time final-turn machinery within `D = min(planner_start + 600s, shared_deadline)`; direct calls without a shared deadline use `planner_start + 600s`. Anchor once before spawn and pass the same deadline through the manager and outer wait; retries, re-entry and cancellation do not extend it. Keep the configured shared wall budget and its independent guard. The 20-iteration ceiling remains an independent earlier final-turn trigger.

Let `R = D - now`, `C = get_call_p99_secs()` (default 243s), and `F = get_final_budget_secs()`. Start an ordinary planner model call only with `R >= 2C` and shared remaining `>= C+F`; bound its timeout to `min(C, R-C, watchdog_remaining, shared_remaining)`. Bound each tool operation to the same local reserve, watchdog and shared deadline; re-evaluate after each operation. At `C <= R < 2C`, request a single tools-disabled final turn only if the unchanged shared `C+F` guard passes. Bound the final call to `min(C, R, watchdog_remaining, shared_remaining)`. At `R < C`, or when the shared guard fails, stop without another call; never borrow time or bypass the guard. Equality is admitted. An iteration-limit final turn obeys the same final-call guard and timeout. If a call or tool is cancelled or times out, cancel and drain cooperatively, admit no subsequent call for that session, and retain the existing bounded no-plan/timed-out fallback without persisting an unvalidated plan; cancellation-resistant code can still finish late.

This replaces only ADR-045's OFF-path iteration-only finalization clause; ADR-045 remains otherwise accepted and immutable, and this proposal does not supersede it until effective acceptance. The executor profile, model, tokens, tools, larger opt-in, rest behavior, plan validation, persistence and host/service settings do not change. Cancellation remains cooperative and a final call is not a guarantee of a valid plan.

# Consequences

### Easier

Planner time is used for a bounded final response instead of ending solely because the iteration ceiling was reached; the existing 600-second and shared-budget guards remain the safety boundary.

### Harder

A final response may be attempted well before twenty iterations. With the defaults `D=600s` and `C=243s`, an ordinary call is admitted only while `R >= 486s`: at most roughly 114 seconds after the planner starts, before other guards. This deliberately trades investigation time for a final-call opportunity. Provider cancellation can still be late or incomplete. The change cannot prove that all existing timed-out sessions would have produced a valid plan.

### What does not change

ADR-045's 6000/9000 opt-in, executor allocation, shared guard, tool profile, model/token settings, persistence path, rest behavior, host settings and rollout authority remain unchanged.

# Alternatives Considered

- **Keep iteration-only finalization:** rejected because the incident shows the planner can reach the 600-second outer timeout before iteration 20.
- **Enable ADR-045's larger budget:** rejected for this minimal design; it changes the resource envelope and needs separate host/service activation evidence.
- **Use a fixed T−120 or fixed short final window:** rejected because it is not tied to the configured call budget `C` and can admit a call without enough time.
- **Bypass the shared `C+F` guard:** rejected because it can consume executor/finalization reserve.

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| OFF path preserves the 600-second deadline while allowing a time-admitted final turn | `tests/test_run_planning_session.py::test_planning_session_uses_600_second_legacy_timeout_by_default` plus new fake-clock final-admission regression | deferred (#2050) |
| Ordinary calls require `R >= 2C`; final calls require `R >= C`; equality and below-boundary cases are explicit | `tests/test_subagent_manager.py::test_planner_current_budget_final_admission_boundaries` | deferred (#2050) |
| Tool steps preserve the final reserve; a completed tool reaching `R=C` transitions to a final call, and timeout/cancellation drains without a later call or unvalidated plan | `tests/test_subagent_manager.py::test_planner_current_budget_tool_and_cancel_guards` | deferred (#2050) |
| ADR-045 opt-in and executor behavior remain unchanged | existing ADR-045 tests in `tests/test_session_clock.py`, `tests/test_subagent_manager.py`, and `tests/test_run_planning_session.py` (these files currently cite ADR-045; the ADR-052 compatibility claim remains deferred until an implementation revision adds a direct citation) | deferred (#2050) |

# References

- Governing incident/design receipt: https://github.com/ozand/eeebot/issues/2050#issuecomment-6674194028
- Installed source incident: https://github.com/ozand/eeebot/issues/2102
- ADR-045: `docs/adr/ADR-045-bound-planner-finalization-by-remaining-time.md`
- Current main: `4d642aba0b8ff64e47c465468a09f6cc89cd5e06`
