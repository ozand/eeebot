# GitHub Task Workflow

This file is no longer the active backlog.

## Canonical Task Tracker

Active tasks now live in GitHub Issues for `ozand/nanobot`.

GitHub Project board:

- `https://github.com/users/ozand/projects/3/views/3`

Use:

- `gh issue list --repo ozand/nanobot`
- `gh issue view <number> --repo ozand/nanobot`
- `gh issue create --repo ozand/nanobot ...`
- `gh issue comment <number> --repo ozand/nanobot ...`
- `gh issue close <number> --repo ozand/nanobot`

## Required Task Structure

Every active issue should carry:

- Task ID
- WSJF
- Workstream
- Owner
- Status
- link to a user story in `docs/userstory/*` or a linked reference note for runtime hygiene work
- key references
- short notes / blockers

## Project and Prioritization

If the GitHub Project does not have a numeric WSJF field, keep WSJF in two places:

1. in the issue body metadata
2. in a GitHub label such as `wsjf:20.0`

This keeps WSJF queryable/filterable in the Project even without a custom numeric field.

Owner should also be reflected in labels where useful, for example:

- `owner:agent-runtime`
- `owner:agent-product`

## User Stories and Acceptance

`docs/userstory/*` remains the canonical place for:

- scope
- Definition of Ready
- Definition of Done

Do not duplicate full user story text into issues.
Link the relevant document instead.

## New Work Rule

When a new idea appears:

1. decide whether an existing user story already covers it
2. create/update a user story if needed
3. create a GitHub issue with the Task ID and links
4. add/update the GitHub Project card and WSJF/owner labels
5. work from the GitHub issue, not from a local backlog row

When linking documentation, prefer repository URLs once the file is pushed, for example:

- `https://github.com/ozand/nanobot/blob/main/docs/userstory/<FILE>.md`

## Completion Rule

When the issue's DoD is satisfied:

1. close the GitHub issue
2. move/update the GitHub Project card accordingly
3. archive a short completion entry in `done.md` if historical context is worth keeping locally

## Repository Sync Rule

The repository is part of the workflow itself.

- When task metadata changes materially, sync it to GitHub.
- When user stories or supporting docs are added or updated, push them so issue links stay valid.
- When work is taken, progressed, or completed, prefer updating the GitHub issue/project state together with the repository state instead of leaving them out of sync.

## Important Constraints

- Do not reintroduce a second local active backlog in this file.
- Do not create `tasks/todo.md`, `tasks/done.md`, or another parallel task tracker.
- Runtime evidence still belongs in canonical runtime/state surfaces, not in this file.

## Current Migration Result

The previous one-row-per-task backlog has been migrated into GitHub Issues in `ozand/nanobot`.
This file is now only the operator/agent workflow guide for task management.
