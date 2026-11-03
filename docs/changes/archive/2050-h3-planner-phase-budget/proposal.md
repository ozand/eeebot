# H3 planner phase budget (#2050)

## Problem

The planner's historical 600s wall wait and iteration-only final turn can expire before a time-based final response is reached. The old H3 draft's 1200s/3600s values predate the confirmed 6000s planner hypothesis and do not preserve the existing executor/final-gate allocation. Raising a default would also risk exceeding the unverified service envelope.

## Change

Implemented planner remaining-time finalization behind the dedicated, exact-value source opt-in `SELFEVO_PLANNER_PHASE_BUDGET_ENABLED=true`. The mode requires explicit finite `NANOBOT_SUBAGENT_WALL_SECS=9000`; that existing setting remains the total wall budget, not feature consent. Unset/false preserves current-main 600s planner behavior, iteration final turn, and legacy total-wall semantics; malformed opt-in or incompatible budget fails closed before planner spawn.

When enabled, bridge-start monotonic allocation is P=6000s planner and E=3000s executor/gate, B=9000s; E contains the single F=300s final/gate reserve. Planner local final-call opportunity C is configured p99; the process-wide shared call guard remains C+F. Calls/tools are deadline bounded, and final plan persistence uses the existing validated harness path. Cancellation is cooperative and may not stop an operation that suppresses cancellation immediately.

Design rationale and alternatives remain in [ADR-045](../../../adr/ADR-045-bound-planner-finalization-by-remaining-time.md); current behavior is recorded in [subagent-bridge spec](../../../specs/subagent-bridge/spec.md).

## Acceptance and verification

- [x] Dedicated opt-in is OFF by default; legacy total-wall configuration does not imply consent.
- [x] Enabled configuration validates a finite exact 9000s total before planner spawn.
- [x] Phase deadline preserves P=6000/E=3000, with F included once in E.
- [x] Time-based final turn uses configured C, preserves the shared C+F guard, and is tools-disabled.
- [x] Deadline/timeout path records no-plan/timed-out and does not persist a partial plan.
- [x] Planner profile and executor compatibility remain covered by focused tests.
- [ ] Independent review and CI for final published candidate.
- [ ] Host/service envelope validation and activation: not authorized or performed; source opt-in is not host approval.

Repository checks on the implementation candidate included focused session-clock/planning-session/subagent-manager tests, planner-choice tests, import hygiene/planner profile/timeout configuration tests, Ruff F811, `lat check`, and diff checks. The published candidate CI is recorded on PR #2071; see its checks for the exact head. No live provider, host, systemd, or 6000-second wait was used.

## Scope and remaining acceptance

This is the repository source/test portion of H3 only. ADR-045 remains Proposed pending its test-on-main acceptance procedure. It does not complete all #2050 acceptance, #2036, forecast/runtime evidence, host capacity approval, or rollout. No unit, release script, default configuration, or host state enables the planner phase budget.
