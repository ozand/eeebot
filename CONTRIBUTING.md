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
