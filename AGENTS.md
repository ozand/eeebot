# AGENTS.md

For agent routing and responsibility/capability/authority boundaries, read [`AGENT_ROUTING.md`](AGENT_ROUTING.md). This policy guides development sessions and does not itself grant authority.

How we develop the **eeebot** product. This is the operational guide for development sessions and the operator; read [`CONSTITUTION.md`](CONSTITUTION.md) first for principles and guardrails.

## Instruction precedence

Follow applicable higher-priority system and developer instructions, then applicable owner and workspace instructions. Within this repository, `CONSTITUTION.md` states principles and guardrails; `AGENT_ROUTING.md` and `REPO_GITHUB_WORKFLOW_RULES.md` govern their respective authority and workflow boundaries. This guide and its linked workflow provide operational detail. If applicable instructions conflict and cannot be reconciled, stop the affected action and seek clarification; do not choose the more permissive instruction.

> This guide is about **developing the product**. It is not the operating contract for the autonomous runtime that runs on the eeepc host — that product behavior lives under [`docs/specs/`](docs/specs/) (start with [`docs/specs/self-evolving-runtime/spec.md`](docs/specs/self-evolving-runtime/spec.md)).

## What this project is

`eeebot` is a bounded self-improving engineering runtime that runs autonomous cycles on a constrained host (`eeepc`), plus an operator-facing chat-agent framework forked from `HKUDS/nanobot`. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/specs/`](docs/specs/) for the full map.

**Naming / compatibility (final state, decided in #619):** the repo/project is `eeebot`, the implementation lives in the **`nanobot/` package** — permanently. `eeebot/` is a thin external-compatibility layer (path-extension + `sys.modules` aliases); no further rename phases are planned. Internal code imports **`nanobot.*` only** (enforced by `tests/test_import_hygiene.py` since #598); `eeebot` naming is for public identity and user-facing surfaces. Two CLI entrypoints ship and must both be preserved unless a task explicitly retires compatibility: `nanobot = "nanobot.cli.commands:app"` and `eeebot = "nanobot.cli.eeebot:main"`. Runtime paths default to `~/.nanobot` (`~/.eeebot` is fallback); Docker/compose use `nanobot` naming. Do **not** do broad mechanical renames. See [`docs/specs/migration/spec.md`](docs/specs/migration/spec.md).

## Source of truth

- **Tasks/status/priority:** GitHub Issues and their existing labels are the single backlog and status source (see [task tracking](docs/agents/development-workflow.md#task-tracking-github-issues--labels)); priority metadata uses existing labels. No `todo.md`/`backlog.md` second backlog.
- **Current product truth:** `docs/specs/<capability>/spec.md`. Index: [`docs/README.md`](docs/README.md).
- **Executable truth wins:** trust `pyproject.toml`, `.github/workflows/ci.yml`, runtime code, and git state over prose. When docs and code disagree, follow running behavior, then fix the doc in the same task.
- **Past failures:** check `lessons/` before deploying (git permissions, systemd timers, release-metadata bugs).
- **Workflow safety rules:** read [`REPO_GITHUB_WORKFLOW_RULES.md`](REPO_GITHUB_WORKFLOW_RULES.md) before repository/Git workflow operations.

## Development workflow

Read [`docs/agents/development-workflow.md`](docs/agents/development-workflow.md) before task tracking, change workflow, repository/branch operations, verification, LiteLLM configuration, or memory instructions. It contains the detailed procedures and applicable commands.

## Security and operations

- Never commit secrets, tokens, auth state, or files from local runtime directories.
- Preserve [`SECURITY.md`](SECURITY.md) defaults (`allowFrom`, least-privilege execution, path protections) unless a task explicitly changes them.

---

*The autonomous runtime's own subagent operating directive (the "implement, not review" loop that runs on the host) is product behavior, not dev process — it lives in [`docs/specs/self-evolving-runtime/spec.md`](docs/specs/self-evolving-runtime/spec.md).*
