# Userstory: Planner/Executor Model Split

## User Story

As the maintainer of the autonomous runtime,
I want planning/reasoning and execution/tool-calling to be separable,
so that stronger planning can be introduced without forcing the entire runtime onto one model path.

## Scope

This story is a future design track. It should only begin after the simpler single-provider,
per-turn model routing has been proven useful and insufficient.

## Acceptance Criteria

- The responsibilities of planner and executor are explicitly separated.
- The handoff between planner and executor is bounded and observable.
- Safety, provenance, and capability truth remain intact after the split.
- The split does not regress the current minimal routing/fallback path.

## References

- `docs/MODEL_ROUTING_FALLBACK_V1.md`
- `docs/PROJECT_CHARTER.md`
