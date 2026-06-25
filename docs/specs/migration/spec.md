# Migration (nanobot → eeebot) — spec

_Status: current. Last updated: 2026-06-25._

## Purpose

The project/repo is **`eeebot`**, but the implementation still lives in the
**`nanobot/` package**. The rename from `nanobot` to `eeebot` is a *staged
compatibility migration*, not a cosmetic search/replace — its job is to move the
public identity and the highest-value user-facing surfaces to `eeebot` while
keeping the live 32-bit eeepc host runtime, dashboard collectors, systemd units,
scripts, and durable state roots working unchanged. This spec describes what is
**true now** about that in-progress migration and the guardrails new code must
follow during the window. Public identity and the compatibility-alias layer are
complete; hard internal/runtime renames are intentionally deferred.

## Requirements

### Compatibility surfaces (true now)
- R1. `eeebot` SHALL be the public identity (`ozand/eeebot`); the runtime
  implementation SHALL remain the `nanobot/` Python package, with `eeebot/` as a
  thin compatibility layer (`__path__` extension + `sys.modules` aliases for
  `eeebot.*` subpackages, plus re-export shims for `eeebot.runtime.*`).
- R2. Both CLI entrypoints SHALL ship and SHALL be preserved unless a task
  explicitly retires compatibility: `nanobot = "nanobot.cli.commands:app"` and
  `eeebot = "nanobot.cli.eeebot:main"`. Both `import nanobot` and `import eeebot`
  (incl. `python -m eeebot`) SHALL work.
- R3. Runtime paths SHALL default to `~/.nanobot`, with `~/.eeebot` as a fallback;
  `NANOBOT_*` environment variables SHALL remain canonical, with `EEEBOT_*` as
  optional aliases. Docker/compose SHALL continue to use `nanobot` naming.
- R4. Both old (`nanobot-ops-dashboard-*`) and new (`eeebot-ops-dashboard-*`)
  dashboard systemd unit names SHALL install and run.

### Rename guardrails (what new code must / must not do)
- R5. New code SHOULD use `eeebot` naming where practical, but SHALL NOT perform
  broad mechanical `nanobot`→`eeebot` renames; edits SHALL stay task-local
  (internal rename is staged on parallel branches).
- R6. New code SHALL NOT rename the `nanobot/` package directory, bulk-rewrite
  import paths to `eeebot.*`, or rename systemd units/scripts without alias shims.
- R7. New code SHALL NOT rename or bulk-rewrite durable runtime-state paths,
  control artifacts, or historical proof artifacts (`workspace/state/**`,
  dashboard `control/**`); any such rename SHALL go through explicit migration
  tooling that preserves reader backward compatibility.
- R8. New compatibility names SHALL be added as aliases first (CLI, env, service,
  import) and SHALL only replace `nanobot` names after dual-name support and a
  rollback path are proven; the live eeepc authority root SHALL NOT be moved
  during a rename.

### Canonical repository
- R9. `ozand/eeebot` SHALL be the canonical repository and durable source of truth
  for all eeebot/nanobot product work, including the operator dashboard / ops
  control plane.
- R10. New durable product code SHALL NOT live only in
  `ozand/eeebot-ops-dashboard` (treated as staging/mirror only); dashboard work
  SHALL consolidate into `ozand/eeebot` (subtree under `ops/dashboard/`, package
  / service / env names kept compatible during the slice). If a staging repo is
  used, a canonical tracking issue SHALL be opened in `ozand/eeebot`.

## Scenarios

### Scenario: both import names resolve to the same runtime
- Given a clean install of the package
- When `import nanobot` and `import eeebot.cli.commands` are run
- Then both succeed and resolve to the same underlying `nanobot` module objects.

### Scenario: new code stays task-local
- Given a task touches a file that still uses `nanobot` naming
- When the change is made
- Then only the symbols the task needs are touched — no opportunistic
  package/import/unit rename is introduced.

### Scenario: durable state is not renamed
- Given a candidate change would rename a `workspace/state/**` or `control/**`
  artifact path
- When the change is evaluated against this spec
- Then it is rejected unless it routes through explicit migration tooling that
  preserves reader backward compatibility.

### Scenario: dashboard work lands in the canonical repo
- Given new operator-dashboard code is needed
- When it is written
- Then it is committed under `ozand/eeebot` (or staged with a canonical tracking
  issue), never left solely in `ozand/eeebot-ops-dashboard`.

## References

- Inventory data (kept as-is, not folded): `docs/EEEBOT_INTERNAL_RENAME_INVENTORY.json`.
- Folded source docs (moved to `.legacy/docs/`):
  `EEEBOT_INTERNAL_RENAME_MIGRATION_PLAN.md`, `EEEBOT_PHASE2_RENAME_MATRIX.md`,
  `EEEBOT_MIGRATION_STATUS_AND_PROOF.md`, `EEEBOT_DUAL_IMPORT_SUPPORT_PROOF.md`,
  `EEEBOT_CANONICAL_REPOSITORY_AND_DASHBOARD_CONSOLIDATION.md`.
- Naming/compatibility guardrails: `AGENTS.md` "Naming / compatibility (critical)"
  section; `CLAUDE.md` "Critical naming/compatibility fact".
- Code: `eeebot/__init__.py` (path extension + `sys.modules` aliases),
  `eeebot/__main__.py`, `nanobot/cli/eeebot.py`, `pyproject.toml`
  `[project.scripts]`, `nanobot/config/paths.py`.
