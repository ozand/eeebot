# AGENTS.md

How we develop the **eeebot** product. This is the operational guide for
development sessions (AI agents and the operator) working on this codebase.

Principles and guardrails live in [`CONSTITUTION.md`](CONSTITUTION.md) — read it
first. This file is the "how"; the constitution is the "why".

> This guide is about **developing the product**. It is not the operating
> contract for the autonomous runtime that runs on the eeepc host — that product
> behavior lives under [`docs/specs/`](docs/specs/) (start with
> `docs/specs/self-evolving-runtime/spec.md`).

## What this project is

`eeebot` is a bounded self-improving engineering runtime that runs autonomous
cycles on a constrained host (`eeepc`), plus an operator-facing chat-agent
framework forked from `HKUDS/nanobot`. See `docs/ARCHITECTURE.md` and
`docs/specs/` for the full map.

**Naming / compatibility (critical):** the repo/project is `eeebot`, but the
implementation lives in the **`nanobot/` package**. `eeebot/` is a thin
compatibility layer (path-extension + `sys.modules` aliases). Both import names
are intentionally live during the migration window. Two CLI entrypoints ship and
must both be preserved unless a task explicitly retires compatibility:
`nanobot = "nanobot.cli.commands:app"` and `eeebot = "nanobot.cli.eeebot:main"`.
Runtime paths still default to `~/.nanobot` (`~/.eeebot` is fallback); Docker/compose
still use `nanobot` naming. Do **not** do broad mechanical renames — internal
rename work is in progress on parallel branches.

## Source of truth

- **Tasks/status/priority:** GitHub Issues + Project (see "Task tracking" below) —
  the single backlog. No `todo.md`/`backlog.md` second backlog.
- **Current product truth:** `docs/specs/<capability>/spec.md`. Index: `docs/README.md`.
- **Executable truth wins:** trust `pyproject.toml`, `.github/workflows/ci.yml`,
  runtime code, and git state over prose. When docs and code disagree, follow
  running behavior, then fix the doc in the same task.
- **Past failures:** check `lessons/` before deploying (git permissions, systemd
  timers, release-metadata bugs).
- **Workflow safety rules:** `REPO_GITHUB_WORKFLOW_RULES.md`.

## Task tracking (GitHub Issues + labels)

GitHub Issues are the single coordination layer for our work. All interaction
goes through the `gh` CLI. Labels are the metadata store — we do not depend on
a Project v2 board (its fields need extra token scopes and GraphQL; at our
scale labels are enough).

- **Every substantial task is a GitHub Issue.** Status lives **only** in the
  `status:*` labels — never duplicated in the Issue body (no drift):
  `status:discovery` → `status:in-progress` → `status:test` → `status:roll-out`;
  no `status:*` label = backlog; issue closed = done.
- **Issue metadata contract (existing labels only):** one `type:*`
  (`runtime|dashboard|process`), `area:*` as applicable (`deploy|eeepc|
  control-plane|self-improvement|…`), optional `priority:*` and `wsjf:*`, and a
  `story_id` in the body linking to the canonical spec/change (resolved to
  `docs/specs/*` or `docs/changes/*`) when one exists.
- **Claiming (our scale = minimal):** set yourself as `assignee` as a *visible
  signal* and post a claim comment. Assignee is not a lock — do **not** take over an
  Issue already claimed by another agent; escalate via a comment.
- **Dependencies** are expressed via native sub-issues / `blocked by`, not text
  task lists. Only pick a **ready** (unblocked) Issue.
- **Done** = PR/commit reference + acceptance criteria met + issue closed with a
  closing comment + affected `docs/specs/*` updated.
- **Label schema changes are a deliberate act** (human or explicitly tasked
  agent) — day-to-day work only assigns labels that already exist.
- Markdown task lists are allowed **only** as scratch inside a branch or PR body —
  never as a persistent backlog.

## Change workflow (specs + changes)

- **Trivial change** (bugfix, doc fix, small scoped edit): just a branch + PR.
- **Substantial change** (new/changed capability): create a `docs/changes/<id>/`
  folder with `proposal.md` (+ `design.md` if non-obvious). The PR implements it.
  On merge, **archive** it to `docs/changes/archive/<id>/` and update the affected
  `docs/specs/<capability>/spec.md`. See `docs/changes/README.md`.
- The change folder is durable *design* documentation tied to a PR — not a status
  tracker (status is the Issue). The two do not duplicate.

## Working tree and branch safety

- Do not work directly on `main`. One task = one branch (`feat/*`, `fix/*`,
  `docs/*`, `chore/*`); prefer `git worktree` for parallel/risky work.
- Run `git status --short --branch` + `git fetch --all --prune` before edits.
- Classify dirty files as task-related vs unrelated; never mix unrelated edits into
  one branch/PR. Re-check `git diff <base>...HEAD` before opening a PR.
- Assume concurrent rename edits may land nearby; keep edits minimal and task-local;
  do not reintroduce legacy naming in new code unless compatibility requires it.

## Commands

```bash
pip install .[dev]                              # install with dev deps
python -m pytest tests/ -v                      # full Python test suite
python -m pytest tests/<file>.py -k <pattern> -v # focused test
ruff check <path>                               # lint (configured in pyproject.toml)
```

## CI reality

- CI runs **Python tests only**, on Python `3.11`/`3.12`/`3.13`. No system
  packages are installed first — the Matrix channel (and its `libolm-dev`
  build dependency) was removed 2026-07-05 per #602.

## LiteLLM config — single source of truth

All LiteLLM credentials/routing for the eeepc runtime live in
**`/etc/eeepc-agent/litellm.env` only**. Never set `LITELLM_API_KEY` /
`LITELLM_BASE_URL` / `LITELLM_MODEL` elsewhere. Models require a `cl/`, `an/`, or
`un/` gateway prefix. See README "LiteLLM configuration".

## Security and operations

- Never commit secrets, tokens, auth state, or files from local runtime directories.
- Preserve `SECURITY.md` defaults (`allowFrom`, least-privilege execution, path
  protections) unless a task explicitly changes them.

## Memory

`memory/MEMORY.md` (index of current project state) and `memory/HISTORY.md` (append
a one-line `[YYYY-MM-DD HH:MM] <what you did>` entry per change) track development
state. Read `MEMORY.md` before picking up self-evolving work.

---

*The autonomous runtime's own subagent operating directive (the "implement, not
review" loop that runs on the host) is product behavior, not dev process — it lives
in `docs/specs/self-evolving-runtime/spec.md`, not here.*
