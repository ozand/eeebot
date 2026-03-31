# Userstory: Smarter Continuation Policy

## User Story

As an operator relying on the bot for longer autonomous work,
I want the runtime to continue, pause, or hand off more intelligently than a simple hard iteration stop,
so that useful work does not end prematurely while bounded execution is preserved.

## Scope

This story covers improving the current hard-stop behavior around iteration budgets.
It includes continuation, pause/resume, and clearer bounded-stop semantics, but not unlimited execution.

## Acceptance Criteria

- The runtime distinguishes hard failure from budget pause/continuation.
- User-visible status explains why work stopped and what continuation path is available.
- The policy remains bounded by time, budget, or explicit continuation rules.
- The implementation is compatible with the current `agent_max_tool_iterations` and subagent budget model.

## Current V1 Slice

- when the main loop stops on the iteration ceiling, the runtime stores the unfinished prompt
- `/continue` resumes from the stored unfinished turn within the same session

## References

- `docs/MODEL_ROUTING_FALLBACK_V1.md`
- `todo.md`
