# Repository and GitHub Workflow Rules

## Purpose
- This file defines mandatory repository/GitHub workflow rules to prevent local and remote changes from mixing in multi-contributor work.
- Scope is workflow safety only; runtime and product behavior remain governed by `AGENTS.md`, `README.md`, and executable config.
- Keep the workflow simple: isolate work, sync often, and do not mix unrelated changes.

## Mandatory preflight
- Before edits, pull, or branch switch, run `git status --short --branch` and `git fetch --all --prune`.
- If the working tree is dirty, classify files as task-related vs unrelated; do not mix both in one branch or PR.
- If unrelated local edits exist, move task work to a separate branch/worktree before continuing.
- Record the intended base branch explicitly before creating a task branch.
- Do not start active work directly on `main`.

## Isolation rules
- One task equals one branch.
- Prefer `git worktree` for parallel or risky work so uncommitted state stays isolated.
- Use branch names that encode intent, for example `feat/*`, `fix/*`, `docs/*`, `chore/*`.
- Keep commits scoped to one concern; do not combine feature, refactor, docs, and workflow changes unless they are inseparable.
- Do not include unrelated untracked files in commits.

## Local and remote sync rules
- Safe sync order: `fetch` -> verify clean or isolated tree -> update from base branch -> verify -> push branch.
- Do not pull or rebase when unrelated local modifications are present.
- If a branch diverges from remote and also has unrelated local edits, isolate first, then sync.
- Push only task branches; do not push local scratch or backup branches unless that is the explicit goal.
- Do not use destructive git operations on shared branches unless explicitly requested.

## GitHub and todo alignment
- Map each active branch or PR to a GitHub issue when the task is substantial enough to track.
- Keep repository state and GitHub state in sync when task metadata, status, or supporting docs change materially.
- Treat `todo.md` as an in-repo operator surface: update it only when the task actually changes its tracked work, status, or proof.
- Do not create a second private backlog file for the same work.
- If process rules or operator workflow change, document the operator impact briefly in the PR or companion docs.

## PR and merge hygiene
- Re-check `git diff <base>...HEAD` before opening a PR to confirm only intended changes are included.
- If unrelated files appear in the branch, remove them before PR creation.
- Run relevant local verification before opening or merging a PR.
- Keep PRs small and reviewable; split oversized work into follow-up branches.
- Prefer merge flows that preserve reviewability and do not hide conflict resolution.

## Stop conditions
- Stop if branch purpose becomes mixed across multiple independent concerns.
- Stop if pull or rebase would combine unrelated local edits with remote updates.
- Stop if task tracking is unclear enough to create drift between repo state and GitHub state.
- Stop if secrets, auth state, or runtime files appear in staged changes.
- When stopping, report the blocker and the recommended isolation or recovery path.
