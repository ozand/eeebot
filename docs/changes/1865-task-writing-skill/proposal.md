# Task-writing skill

## Problem

Issue #1859 defines DoR and DoD semantics, but their release-owned carrier does not exist. Putting the full contract in `OPERATING.md` would make it resident in every executor prompt.

## Change

Add one release-owned `task-writing` skill. The planner reads its full contract before each plan, while the role prompt holds only an unconditional path. A missing actual read degrades the planner to the ranked queue and records the refusal.

## Acceptance

The skill is outside loop mutation paths, its full text is not resident, every successful planner session records that it read the skill, and a missed read cannot produce a diary plan.

## Non-goals

Do not change DoR/DoD semantics, add a new metric, or duplicate `OPERATING.md` rules.
