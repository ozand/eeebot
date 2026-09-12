# Repository Git hooks

The repository-owned hooks live here so they are versioned and reviewable. Git
only invokes them after a checkout configures this directory as its hooks path.

## Existing checkout setup

From the repository root, run this once:

```bash
git config core.hooksPath hooks
```

The setting is local to that checkout/worktree and is not changed by cloning or
pulling. Each existing checkout or worktree must opt in separately; this PR does
not install or configure the hook in the shared `T:/Code/eeebot` checkout.

New clones can opt in immediately after cloning with the same command. The
hook remains inactive until this setup step is performed.

## Protected branch behavior

`pre-commit` refuses a commit on `main` or the repository's configured default
branch. It provides the worktree path and PR workflow instead. Feature branches,
feature worktrees, and detached HEADs are not blocked.

An intentional operator commit may bypass the guard with `git commit --no-verify`.
That bypass is deliberately preserved; it is an explicit escape hatch, not an
installation mechanism.
