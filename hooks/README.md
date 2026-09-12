# Repository Git hooks

The repository-owned hooks live here so they are versioned and reviewable. The
`hooks/` directory is the source of truth; the installed copy below is the
branch-independent runtime location.

## Why `core.hooksPath hooks` is not enough

`core.hooksPath hooks` points into the current working tree. The setting can
survive a pull, but the hook file exists only on branches that contain
`hooks/pre-commit`. If an older branch predates this directory, Git finds no
hook and skips it silently: a configured path is not the same thing as a
working guard.

## Existing checkout setup

Install the hook outside the working tree so changing branches cannot remove
it. From the repository root, run this once. `git rev-parse --git-dir` resolves
the right Git directory for a normal checkout or a linked worktree; in a normal
checkout this is the `.git/repo-hooks` location:

```bash
git_dir="$(git rev-parse --git-dir)"
mkdir -p "$git_dir/repo-hooks"
git show origin/main:hooks/pre-commit > "$git_dir/repo-hooks/pre-commit"
chmod +x "$git_dir/repo-hooks/pre-commit"
git config core.hooksPath "$git_dir/repo-hooks"
```

The source is read from `origin/main`, so this also works while the current
branch predates `hooks/pre-commit`. If `origin/main` is not available locally,
fetch it first or substitute another reviewed ref containing the versioned
hook.

Verify both configuration and the executable file; do not treat a successful
`git config` command as proof that Git can invoke the hook:

```bash
hook_path="$(git config --get core.hooksPath)" && test -n "$hook_path" && test -x "$hook_path/pre-commit" && printf 'installed: %s\n' "$hook_path/pre-commit"
```

Each existing checkout or worktree must opt in separately. This documentation
change does not install or configure any live checkout. New clones must run the
same installation and verification steps after cloning.

The copied hook does not update on `git pull`: pulling updates the versioned
source under `hooks/`, not the file outside the working tree. Re-run the
installation recipe after an approved hook change to refresh the copied guard.

## Protected branch behavior

`pre-commit` refuses a commit on `main` or the repository's configured default
branch. It provides the worktree path and PR workflow instead. Feature branches,
feature worktrees, and detached HEADs are not blocked.

An intentional operator commit may bypass the guard with `git commit --no-verify`.
That bypass is deliberately preserved; it is an explicit escape hatch, not an
installation mechanism.
