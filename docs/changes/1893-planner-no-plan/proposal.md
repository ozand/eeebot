# Planner no-plan outcome

## Problem

Live planners with a successfully pre-read task contract ended without JSON: one exhausted 20 tool-call iterations and returned the generic no-final text; another stopped on an identical-call loop after eight iterations. Bridge classified both as malformed JSON, obscuring the actual stop.

## Change

Keep the ADR-031 20-tick budget and ranked-queue fallback. Interpret known terminal `SubagentManager` telemetry before JSON parsing: explicit `stop_reason` or the current no-final fallback records `planning_session.outcome=no_plan` with a precise reason. Only responses that claim to be final but fail JSON parsing remain `malformed`.

## Acceptance

Tests cover both real-shaped terminal forms and preserve task-writing read evidence; no diary plan is written for a no-plan outcome. Host verification must show a valid plan or an explicitly classified no-plan/provider blocker rather than misleading malformed success.

## Non-goals

No extra LLM turn, changed tool access, larger planner budget, or assignment-to-selection implementation.
