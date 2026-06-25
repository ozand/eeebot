# Self-Evolving Runtime Restore Note

Last updated: 2026-03-31 UTC

## Observed Problem

The live bot behaved like an operator chat that waited for explicit user tasks,
even though the project goal is a bounded self-evolving runtime.

## Root Cause

The autonomous service path was not fully dead, but it was effectively stuck:

- `nanobot-gateway-eeepc.service` was active,
- `eeepc-self-evolving-agent-health.timer` was active,
- the health service was running every 15 minutes,
- but the self-evolving cycle kept replaying a stale operator-originated task,
- and bounded apply remained blocked because the approval gate file was missing.

## Smallest Practical Restore Step

1. clear/archive the stale `state/inbox/task.json`,
2. write a short-lived `state/approvals/apply.ok`,
3. trigger `eeepc-self-evolving-agent-health.service` once,
4. verify that the next report is produced from the autonomous backlog path instead of the stale inbox task.

## What Changed

- stale inbox task was archived and removed,
- a 60-minute approval gate was written,
- the self-evolving health service was started manually,
- the next run finished with `PASS`.

## Remaining Caveat

The autonomous backlog still prefers an older active goal until the goal registry is reprioritized,
so the runtime is restored but not yet fully redirected to a newer self-improvement objective mix.
