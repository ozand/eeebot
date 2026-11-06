# Current-budget planner finalization

## Problem

The installed planner can reach its 600-second limit before the iteration-only final turn. The available aggregate evidence does not establish where the elapsed time went.

## Proposed change

Apply ADR-052's remaining-time final-turn policy inside the existing 600-second planner and shared wall budgets. Preserve the separate ADR-045 6000/9000 opt-in, executor, safety guards, model and tool profile. This proposal is not an implementation or rollout authorization.

## Acceptance

- ADR-052 receives independent exact-revision review and becomes effective before implementation.
- Focused fake-clock tests establish its admission, timeout, cancellation and compatibility boundaries; validated plans use the existing persistence path.
- Runtime specs and current-state `lat.md/` are updated with the implemented behavior in the implementation PR. Host rollout remains separately governed.
