# Contributing to eeebot

`eeebot` is a fork of [`HKUDS/nanobot`](https://github.com/HKUDS/nanobot), but
contributions to this repo follow our own process, not upstream's. Read
[`AGENTS.md`](AGENTS.md) and [`CONSTITUTION.md`](CONSTITUTION.md) first — they
are the operational guide and the principles behind it.

## Workflow

- One task = one branch (`feat/*`, `fix/*`, `docs/*`, `chore/*`). Do not work
  directly on `main`.
- Every substantial change starts from a GitHub Issue (see `AGENTS.md` for the
  issue/label contract). Trivial fixes can go straight to a branch + PR.
- PRs target `main`. There is no separate `nightly` branch in this fork.
- Discuss ideas, questions, and proposals via GitHub Issues on this repo.

## Development setup

```bash
git clone https://github.com/ozand/eeebot.git
cd eeebot

pip install -e ".[dev]"          # install with dev dependencies
python -m pytest tests/ -v       # run the test suite
ruff check nanobot/              # lint
```

## Repository hook setup

The versioned `hooks/pre-commit` guard prevents accidental commits on `main` or
this repository's configured default branch. Do not configure
`core.hooksPath hooks`: that path is relative to the working tree, so an older
branch without `hooks/pre-commit` silently has no protection.

Install a branch-independent copy outside the working tree from the repository
root. This uses `origin/main` as the reviewed source and works even when the
current branch predates the hook:

```bash
git_dir="$(git rev-parse --git-dir)"
mkdir -p "$git_dir/repo-hooks"
git show origin/main:hooks/pre-commit > "$git_dir/repo-hooks/pre-commit"
chmod +x "$git_dir/repo-hooks/pre-commit"
git config core.hooksPath "$git_dir/repo-hooks"
```

Verify that the configured path points to an executable hook:

```bash
hook_path="$(git config --get core.hooksPath)" && test -n "$hook_path" && test -x "$hook_path/pre-commit" && printf 'installed: %s\n' "$hook_path/pre-commit"
```

Each existing checkout or worktree must opt in separately. The copied hook does
not update on `git pull`; rerun the installation recipe after an approved
change to `hooks/pre-commit`. This documentation change does not install or
configure any live checkout. Feature branches, feature worktrees, and detached
HEADs remain unaffected. If an operator intentionally needs to commit to the
protected branch, the deliberate escape hatch is `git commit --no-verify`.

## Code style

We care about more than passing lint — code should stay small, calm, and
readable:

- Simple: prefer the smallest change that solves the real problem
- Clear: optimize for the next reader, not for cleverness
- Decoupled: keep boundaries clean, avoid unnecessary new abstractions
- Durable: choose solutions that are easy to maintain, test, and extend

In practice:

- Line length: 100 characters (`ruff`)
- Target: Python 3.11+
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored)
- Async: uses `asyncio` throughout; pytest with `asyncio_mode = "auto"`
- Prefer focused patches over broad rewrites
