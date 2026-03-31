# Process Note: Lightweight Discovery → Development → Test → Release Loop

## Purpose

This note explains how autonomous or multi-agent work should progress in this
repo without creating a second backlog or process system.

It is a thin execution overlay on top of:

- `todo.md` for active work,
- `docs/userstory/` for scope and intent,
- `done.md` for completed outcomes.

## The Loop

### 1. Discovery

Clarify the bounded problem before changing code.

Discovery should produce:

- a linked user story,
- bounded scope,
- acceptance criteria,
- known blockers or dependencies,
- a clear owner.

If the work is active, it belongs in `todo.md`.

### 2. Development

Implement only the bounded slice.

Development should:

- follow the linked user story,
- stay within scope,
- avoid turning one item into a hidden project,
- keep changes explainable.

### 3. Test

Validate the specific slice, not the whole universe.

Testing should answer:

- did the intended behavior change happen,
- do the focused checks pass,
- are truth/provenance expectations preserved,
- what remains blocked or external.

### 4. Release

Close the loop by making the result visible and archival.

Release means:

- mark the item complete,
- move it out of `todo.md`,
- summarize it in `done.md`,
- preserve the most useful verification context.

## How The Repo Files Fit Together

### `todo.md`

- unfinished work only,
- grouped as meaningful execution outcomes,
- linked to user stories,
- prioritized by WSJF,
- no duplicate mini-trackers.

### `docs/userstory/`

- why the work matters,
- what is in scope,
- what must be true before starting,
- what must be true before calling it done.

### `done.md`

- completed work only,
- completion date,
- short outcome summary,
- enough context to explain what was actually delivered.

## Multi-Agent Coordination Rule

Good multi-agent coordination here is simple:

- one active owner per work item,
- other agents contribute bounded help only,
- handoffs are explicit,
- shared truth lives in files, not in chat memory,
- no second backlog system is allowed.

## Practical Rule Of Thumb

- If a task cannot be described clearly in a user story, it is too broad.
- If it cannot be tested in a focused way, it is too vague.
- If it cannot be archived cleanly in `done.md`, it is probably too fragmented.

## Summary

The operating loop is:

**Discover the bounded problem → implement the slice → verify the result → archive the outcome.**

Keep it lightweight, product-simple, and file-backed.
