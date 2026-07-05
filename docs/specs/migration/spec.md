# Migration (nanobot → eeebot) — spec

_Status: current. Last updated: 2026-06-25._

## Purpose

The project/repo is **`eeebot`**; the implementation lives in the
**`nanobot/` package**. As of #619 (2026-07-05) this split is the **decided
final state**, not a migration in flight: public identity and user-facing
surfaces are `eeebot`, the package and all internal imports are `nanobot`
(unified in #598 and enforced by `tests/test_import_hygiene.py`), and the
`eeebot/` compatibility layer is a permanent external-facing shim. No further
rename phases are planned; a full package rename would require a new
`docs/changes/` proposal. The guardrails below remain binding as permanent
rules for the live eeepc host runtime, systemd units, scripts, and durable
state roots.

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
- R4. (Retired 2026-07-05, #617 — the WSGI ops dashboard moved to its own
  canonical repo `ozand/eeebot-ops-dashboard`; its dual-named systemd units left
  this repo with it. The live host dashboard is `scripts/eeebot_dashboard.py`.)

### Rename guardrails (what new code must / must not do)
- R5. Internal code SHALL import `nanobot.*` only (guard:
  `tests/test_import_hygiene.py`); `eeebot` naming SHALL be used for public
  identity and user-facing surfaces. New code SHALL NOT perform broad mechanical
  `nanobot`→`eeebot` renames; edits SHALL stay task-local.
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
- R9. `ozand/eeebot` SHALL be the canonical repository and durable source of
  truth for all eeebot/nanobot product work, with one scoped exception: the
  dormant WSGI ops dashboard, whose canonical home is
  `ozand/eeebot-ops-dashboard` since #617 (2026-07-05, extracted from
  `ops/dashboard/` via subtree split with full history).
- R10. Work on the WSGI ops dashboard SHALL happen in
  `ozand/eeebot-ops-dashboard`; deploying it to the host (replacing the live
  `scripts/eeebot_dashboard.py`) SHALL be proposed via `docs/changes/` here
  first, since host units and deploy scripts live in this repo.

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

### Scenario: dashboard work lands in its canonical repo
- Given new WSGI ops-dashboard code is needed
- When it is written
- Then it is committed under `ozand/eeebot-ops-dashboard` (canonical since
  #617); changes to the live host dashboard (`scripts/eeebot_dashboard.py`)
  land here in `ozand/eeebot`.

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
