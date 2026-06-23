# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`eeebot` is a bounded **self-improving engineering runtime** that runs autonomous cycles on a constrained host (`eeepc`), plus an operator-facing chat-agent framework forked from `HKUDS/nanobot`. It is not a plain conversational bot: the core loop observes runtime state, selects a bounded task, runs an experiment, and writes durable proof to a canonical state root before any change is promoted. See `docs/ARCHITECTURE.md` and `docs/SYSTEM_OPERATION_REFERENCE.md`.

## Critical naming/compatibility fact

The project/repo is `eeebot`, but the **implementation lives in the `nanobot/` package**. `eeebot/` is a thin compatibility layer: `eeebot/__init__.py` extends its `__path__` onto `nanobot`'s and registers `sys.modules` aliases for many subpackages (`eeebot.agent.*`, `eeebot.bus.*`, `eeebot.channels.*`, `eeebot.config.*`, `eeebot.cron.*`, `eeebot.heartbeat.*`, `eeebot.providers.*`, `eeebot.utils.*`) so those resolve to the **exact same module objects** as their `nanobot` counterparts. The `eeebot.runtime.*` modules (`state`, `coordinator`, `promotion`, `health`) are instead thin re-export shims (`from nanobot.runtime.state import *`) — a *distinct* module object that re-exports nanobot's symbols, so they're functionally equivalent at the symbol level. Both import names are intentionally live during the migration window.

- Two CLI entrypoints are both shipped (`pyproject.toml` `[project.scripts]`): `nanobot = nanobot.cli.commands:app` and `eeebot = nanobot.cli.eeebot:main`. Preserve **both** when touching packaging/CLI unless the task explicitly retires compatibility.
- New code should use `eeebot` naming where practical, but do **not** do broad mechanical renames — internal rename work is in progress on parallel branches (see `AGENTS.md` and `docs/EEEBOT_INTERNAL_RENAME_MIGRATION_PLAN.md`). Keep edits task-local.
- Runtime paths still default to `~/.nanobot` (`~/.eeebot` is only a fallback). Docker/compose still use `nanobot` naming.

## Commands

```bash
pip install .[dev]                              # install with dev deps (pytest, ruff, matrix-nio)
python -m pytest tests/ -v                      # full Python test suite
python -m pytest tests/test_runtime_coordinator.py -k <pattern> -v   # focused test
python3 -m pytest tests/ -x -q                  # fast smoke (used by autonomous subagents, ~60s)
ruff check <path>                               # lint (line-length 100, rules E/F/I/N/W, E501 ignored)
```

Bridge (separate Node/TypeScript package in `bridge/`, requires Node >=20):
```bash
cd bridge && npm run build                      # tsc build/typecheck — bridge has NO CI, validate manually
```

CI (`.github/workflows/ci.yml`) runs **Python tests only** on Python 3.11/3.12/3.13, and installs `libolm-dev` + `build-essential` first (relevant for env-sensitive matrix/e2e failures).

## Architecture

### Self-improving runtime (the primary subject of most work)

The cycle is `Observe → Specify → Execute → Evaluate → Persist`, driven by `nanobot/runtime/coordinator.py` (`run_self_evolving_cycle`). Entry points: `app/main.py` (systemd service entry) and the `eeebot cycle-health` CLI command.

Key `nanobot/runtime/` modules:
- `coordinator.py` — the cycle runner: goal rotation, HADI hypothesis backlog, WSJF prioritization, experiment contracts (`keep`/`discard`/`blocked`/`crash`), credits ledger, budgets (`DEFAULT_EXPERIMENT_BUDGET` with a hard ceiling).
- `state.py` / `state_subagents.py` / `state_promotion.py` — read/aggregate durable runtime state; compute cycle health, material progress, subagent telemetry.
- `subagent_materializer.py` + `bounded_subagent_executor.py` — turn coordinator-emitted subagent requests into bounded executions.
- `promotion.py` / `github_ops.py` / `autoevolve.py` — promotion readiness packets, guarded git commit/push, candidate releases.
- `scorer.py`, `health.py`, `lessons.py`, `local_ci.py`.

**State contract (the durable proof):** every cycle MUST write evidence under the state root:
- `state/reports/evolution-<timestamp>-<cycle_id>.json` — definitive log (top-level `result_status` = `PASS|BLOCK|CRASH`; `experiment.outcome` = `keep|discard|blocked|crash`; plus `changed_files`, `promotion.readiness`).
- `state/goals/current.json` — snapshot of active goal/cycle/tasks.
- `state/promotions/` — promotion candidates awaiting human/policy review.

The state root is environment-driven (`NANOBOT_RUNTIME_STATE_SOURCE`, `NANOBOT_RUNTIME_ROOT`, `NANOBOT_WORKSPACE`); on the eeepc host it defaults to `/var/lib/eeepc-agent/self-evolving-agent/state`.

### Chat-agent framework (upstream nanobot heritage)

- `nanobot/agent/` — `loop.py` (core processing engine), `subagent.py` (`SubagentManager`), `context.py`, `memory.py`, `skills.py`, `tools/`.
- `nanobot/channels/` — pluggable chat channels (telegram, slack, discord, matrix, mochat, whatsapp, feishu, dingtalk, qq, wecom, email) via `registry.py`/`manager.py`.
- `nanobot/providers/`, `nanobot/bus/`, `nanobot/session/`, `nanobot/cron/`, `nanobot/heartbeat/`, `nanobot/security/`.
- CLI commands (`nanobot/cli/commands.py`): `onboard`, `gateway`, `agent`, `cycle-health`, `status`.
- The WhatsApp bridge (`bridge/`) is a Node/Baileys process the Python runtime talks to.

### Orchestration scripts & systemd

`scripts/` holds the operational glue driven by user systemd timers in `systemd/`. The default managed autonomous path is the **guarded self-evolution loop**: `guarded_self_evolve.py` → `create_candidate_release.py` → release dir + `current` symlink → `health_check_release.py` gate → `commit_and_push_self_evolution.py`, writing rollback/failure-learning artifacts on gate failure. The subagent execution path is `scripts/eeepc_self_evolving_subagent_bridge.py` (reads `state/subagents/requests/`, spawns bounded subagents). Install local timers with `scripts/install_user_units.sh`.

## LiteLLM config — single source of truth

All LiteLLM credentials/routing for the eeepc runtime live in **`/etc/eeepc-agent/litellm.env` only**. Never set `LITELLM_API_KEY` / `LITELLM_BASE_URL` / `LITELLM_MODEL` elsewhere. Models require a `cl/` or `an/` gateway prefix. Avoid `cl/gpt-5.5` and `cl/claude-opus-*` for routine cycles (cost). See README "LiteLLM configuration" for rotation steps and current endpoint.

## Workflow rules (enforced — see `AGENTS.md`, `REPO_GITHUB_WORKFLOW_RULES.md`)

- **Do not work directly on `main`.** One task = one branch (`feat/*`, `fix/*`, `docs/*`, `chore/*`); prefer `git worktree` for parallel/risky work. Run `git status --short --branch` + `git fetch --all --prune` before edits; never mix unrelated local edits into a task branch/PR.
- `ozand/eeebot` is the canonical repo and durable source of truth. Do not leave durable product code only in sibling repos (e.g. `ozand/eeebot-ops-dashboard` — staging only).
- Prefer **complete logical changes** over micro-incremental edits, but keep refactors scoped to task intent; avoid opportunistic rename churn.
- When docs and code disagree, follow running behavior/config first, then update docs deliberately in the same task.
- Before deploying changes, check `lessons/` for past operational failures (git permissions, systemd timers, release-metadata bugs) to avoid loop stagnation.

## Memory / context for autonomous work

`memory/MEMORY.md` (index of current project state and what to work on next) and `memory/HISTORY.md` (append a one-line `[YYYY-MM-DD HH:MM] <what you did>` entry per change) drive the autonomous subagent loop described in `AGENTS.md`. Read `MEMORY.md` before picking up self-evolving work.
