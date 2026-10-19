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

### Task provenance and completion receipts

For Pi-assisted repository work, make the receipt describe what actually ran; a
session's current directory or a model-written path is not proof of repository
identity or authorization. Record the observed cwd, Git top-level, branch, full
starting and ending HEAD SHAs, task/owner/writer, selected generator and import
or source provenance, single-writer scope, and admitted baseline-to-final path
status. State exact completion evidence (for example, focused checks, Lat
validation, independent review, or CI) and distinguish passed, failed, skipped,
and not-run work.

Keep receipts outside the product tree in an explicitly approved evidence
location. Default to path names/status, not content: never read, copy, publish,
or hash credentials, auth/session state, private files, caches, or whole trees.
Only inspect or hash content for explicitly admitted non-private paths. Record
which paths were admitted and why; unknown paths remain uninspected. Do not
claim a guard blocked writes unless its exact tool coverage and result are
verified.

The best-effort Pi protected-root guard implementation is maintained in
[`ozand/pi-config`](https://github.com/ozand/pi-config). The implementation and
subsequent evidence-journal repair are merged and independently reviewed; the
exact merged repair is pinned at
[`ca9dd0c575d67577479a92433a91f7fc6700c1c6` README](https://github.com/ozand/pi-config/blob/ca9dd0c575d67577479a92433a91f7fc6700c1c6/README.md#protected-root-guard-not-activated-by-this-change)
and [ADR-004](https://github.com/ozand/pi-config/blob/ca9dd0c575d67577479a92433a91f7fc6700c1c6/docs/adr/ADR-004-protect-registered-product-roots-at-the-pi-tool-boundary.md).
It covers only supported direct Pi `write`/`edit` calls and is not a sandbox.
The guard has not thereby been installed or activated. Canary/readiness work is
tracked separately in [pi-config#41](https://github.com/ozand/pi-config/issues/41);
canary completion and production approval are not claimed here. Check current
upstream state before relying on protection. Operator-provided environment
registration is a launch trust assumption, not cryptographic identity; cwd,
branch, and model-supplied fields alone do not authorize writes.

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
