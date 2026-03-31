# User Story Guidelines

## Purpose

This directory holds user stories for active or upcoming work.

User stories are not meant to duplicate the backlog.
They exist to answer:

- why the work matters,
- what bounded scope is intended,
- what must be true before starting,
- what must be true before calling it done.

## Required Structure

Every new user story should contain:

1. `User Story`
2. `Scope`
3. `Acceptance Criteria`
4. `Definition of Ready`
5. `Definition of Done`
6. `References`

## Definition of Ready

Use this section to describe what must already be true before implementation
starts.

Typical ready checks:

- the problem and operator value are clear,
- the target workstream/task exists in `todo.md`,
- the implementation boundary is small enough to complete coherently,
- dependencies or blockers are known,
- the owner is clear,
- relevant docs/code references are linked,
- success can be checked with focused validation.

## Definition of Done

Use this section to describe what must be true before the task/package can be
considered complete.

Typical done checks:

- the bounded scope is implemented,
- focused tests or host/runtime checks pass,
- truth/provenance implications are handled,
- docs/backlog notes are updated,
- remaining blockers are explicitly recorded,
- the result is small, explainable, and does not require hidden assumptions.

## Suggested Template

```md
# Userstory: <title>

## User Story

As a <role>,
I want <capability>,
so that <outcome>.

## Scope

- in scope
- out of scope

## Acceptance Criteria

- ...

## Definition of Ready

- ...

## Definition of Done

- ...

## References

- `todo.md`
- `docs/...`
```
