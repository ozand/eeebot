# Goal Registry Hygiene Note

Last updated: 2026-03-31 UTC

## Goal

Reduce stale-goal noise in the host goal registry without introducing a second goal system.

## Safe Cleanup Applied

- kept the current `active_goal_id` on `goal-44e50921129bf475`
- preserved historical goal records for audit
- marked stale or smoke-test goals as `archived=true`
- kept the already-fixed improve-fallback goal in the registry but archived it

## Verified Result

- the registry still points to `goal-44e50921129bf475` as the active goal
- stale goals remain readable for audit but no longer represent live planning candidates
- the next self-evolving report still selected `goal-44e50921129bf475`

## Non-Goals

- no new goal system
- no deletion of historical evidence
- no automatic approval renewal
