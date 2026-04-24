# AGENTS.md

## Scope
- This file is the root guidance for OpenCode sessions in this repo.
- Keep changes small, focused, and compatible with the current runtime.

## Source of truth
- Trust executable sources first: `pyproject.toml`, `.github/workflows/ci.yml`, `bridge/package.json`, runtime code, and current git state.
- Use `docs/README.md` as the docs index, especially for migration context and maintainer reading order.
- For active rename execution context, prioritize migration docs linked from `docs/README.md` over stale assumptions in older notes.
- Repository/GitHub operating rules live in `REPO_GITHUB_WORKFLOW_RULES.md`.

## Repo identity and migration guardrails
- This is the canonical `eeebot` repository: `ozand/eeebot`. Treat it as the default GitHub target and durable source of truth for eeebot/nanobot product work.
- Do not create new durable product code only in sibling repos such as `ozand/eeebot-ops-dashboard`. If a sibling repo is used as temporary staging, create a canonical `ozand/eeebot` tracking issue immediately and migrate or mirror the code back here before considering the work durable.
- Dashboard/operator-control work belongs in this canonical repo once production-worthy; see `docs/EEEBOT_CANONICAL_REPOSITORY_AND_DASHBOARD_CONSOLIDATION.md`.
- Internals are being migrated from `nanobot` naming to `eeebot`.
- Internal rename work is actively in progress on parallel branches; avoid broad mechanical renames or cross-cutting refactors unless the task is explicitly migration-scoped.
- If your task touches files under rename churn, keep edits minimal and task-local; do not reintroduce legacy naming in newly added code unless compatibility requires it.
- Packaging/CLI compatibility is currently dual-entrypoint; when touching packaging or CLI behavior, preserve both current scripts from `pyproject.toml` unless the task explicitly retires compatibility:
  - `nanobot = "nanobot.cli.commands:app"`
  - `eeebot = "nanobot.cli.eeebot:main"`
- The WhatsApp bridge is the separate Node/TypeScript package in `bridge/`.

## Working tree and branch safety
- Do not do active work directly on `main`.
- Start from fresh `origin/main`, create a task branch, and keep unrelated local edits isolated in a separate branch/worktree.
- Preferred workflow for parallel/risky work: task branch + git worktree.
- Before pulling, check for local modifications so remote changes are not mixed with unrelated local edits.
- Assume concurrent rename edits may be landing nearby; rebase or merge frequently and avoid cleanup changes outside task scope.
- Do not block or undo in-progress rename work by restoring legacy names purely for consistency; prefer incremental convergence toward `eeebot` naming.

## Runtime and path gotchas
- Runtime compatibility paths still default to `~/.nanobot` in current code.
- `~/.eeebot` is only used as a fallback when it exists and `~/.nanobot` does not.
- Docker and compose still use `nanobot` naming and commands in the current compatibility window.

## Verified commands
- Install dev dependencies: `pip install .[dev]`
- Run full Python tests: `python -m pytest tests/ -v`
- Run a focused test: `python -m pytest tests/<file>.py -k <pattern> -v`
- Ruff is configured in `pyproject.toml`; use targeted checks when needed: `ruff check <path>`
- Bridge build/typecheck: in `bridge/`, run `npm run build`
- Bridge runtime requires Node `>=20`

## CI reality
- CI currently runs Python tests only, on Python `3.11`, `3.12`, and `3.13`.
- CI installs `libolm-dev` and `build-essential` before tests; keep that in mind for env-sensitive failures.
- There is no verified JS bridge CI job in this repo, so bridge changes should be manually validated with `npm run build`.

## Security and operations
- Never commit secrets, tokens, auth state, or files from local runtime directories.
- Preserve security defaults from `SECURITY.md`: `allowFrom` protections, least-privilege execution, path protections, and localhost-only bridge assumptions unless the task explicitly changes them.
- For recurring tasks/reminders, preserve the template guidance in `nanobot/templates/AGENTS.md`: prefer built-in cron tooling and use `HEARTBEAT.md` for periodic tasks rather than ad hoc reminder notes.

## Change style
- Prefer minimal, focused patches over broad rewrites.
- Keep refactors scoped to task intent; avoid opportunistic rename churn.
- When docs and code disagree, follow running behavior/config first, then update docs deliberately in the same task when appropriate.
