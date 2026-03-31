# Active Goal Reprioritization Note

Last updated: 2026-03-31 UTC

## Problem

The self-evolving runtime kept preferring `goal-24dcf2b028609d52` even after the
original improve-fallback issue had already been fixed.

## Root Cause

The host goal registry still had:

- `active_goal_id = goal-24dcf2b028609d52`

so the runtime's `active_first` / `preferred_goal` selection kept returning the stale goal.

## Fix Applied

- `goal-24dcf2b028609d52` was marked `completed`
- `active_goal_id` was switched to `goal-44e50921129bf475`
- one self-evolving cycle was triggered after the change

## Result

- the next self-evolving report selected `goal-44e50921129bf475`
- the runtime no longer preferred the stale improve-fallback goal
- after refreshing the short-lived apply gate, a fresh cycle completed with `PASS`
  on the new active goal and exposed `bounded_apply=on` / `promotion_execute=on`

## Remaining Caveat

The newly active goal is still blocked by promotion/apply prerequisites,
so reprioritization fixed goal selection but not the deeper promotion gate constraints.
