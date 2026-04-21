# eeebot Full Migration Execution Plan

Status: planning / migration-governance document. Not a live execution state surface.

> For Hermes: execute this as a staged compatibility migration. Do not do a single-shot internal rename.

Goal:
Complete the migration from public/internal `nanobot` identity toward `eeebot` while preserving eeepc runtime compatibility, dashboard continuity, and rollback safety.

Current completed phases:
- public GitHub repo rename to `eeebot`
- dashboard repo rename to `eeebot-ops-dashboard`
- public README/metadata alignment
- `eeebot` CLI alias
- `EEEBOT_*` environment variable aliases with `NANOBOT_*` fallback
- `~/.eeebot/...` path compatibility aliases
- machine-readable inventory and rename matrix

Remaining migration phases:

## Phase 3 — Service/script alias layer
Objective:
Add `eeebot`-named wrappers and systemd aliases without removing `nanobot`-named compatibility surfaces.

Tasks:
1. Add `eeebot-ops-dashboard-web.service` and `eeebot-ops-dashboard-collector.service` aliases.
2. Update installer script to install both old and new units.
3. Add `run_eeebot_web.sh` / `run_eeebot_collector.sh` aliases if needed, delegating to existing scripts.
4. Verify both unit names can be enabled/run.

Acceptance:
- old service names still work
- new service names also work
- no change to runtime paths or collected state roots

## Phase 4 — Public/help/docs cleanup
Objective:
Update user-visible help and docs from `nanobot` to `eeebot` where low risk.

Tasks:
1. Update CONTRIBUTING/SECURITY/public docs wording.
2. Update CLI help text where public-facing only.
3. Preserve archived proof docs unless they need explicit clarifying notes.

Acceptance:
- user-facing help/docs say eeebot
- compatibility-critical examples remain clear

## Phase 5 — Optional config/service env aliases
Objective:
Add `EEEBOT_*` aliases for dashboard/service env names where safe.

Tasks:
1. Add dashboard-side `EEEBOT_*` aliases with `NANOBOT_*` fallback.
2. Keep existing systemd environment files valid.

Acceptance:
- both env naming schemes work

## Phase 6 — Dual import/package support (hard)
Objective:
Introduce `eeebot` import/package aliasing before any hard package rename.

Tasks:
1. Add import alias package or wrapper if feasible.
2. Add tests proving both import styles work.
3. Do not rename package directory yet.

Acceptance:
- existing imports keep working
- new eeebot import surface exists

## Phase 7 — Runtime/service cutover (very hard)
Objective:
Only after all prior compatibility layers exist, evaluate changing canonical service/runtime names.

Non-goal for current tranche:
- do not rename `workspace/state` or eeepc authority root
- do not bulk-rewrite archived docs/control artifacts
- do not rename the `nanobot/` package directory in this tranche

Current execution tranche:
- implement Phase 3 service/script alias layer
- start Phase 4 public/help/docs cleanup where obvious and low risk

## Phase 8 — Provider/config bridge hardening (current)
Objective:
Move the remaining low-risk internal `config`/`provider` import surfaces onto `eeebot.*` compatibility bridges before attempting deeper agent/runtime rewrites.

Tasks:
1. Add explicit `eeebot.providers` compatibility shims for the highest-value provider modules used by config/runtime selection.
2. Migrate safe `nanobot.config` imports to `eeebot.config.*` where no circular-import risk appears.
3. Add tests for `eeebot.config.loader`, `eeebot.config.schema`, and provider-registry access.

Acceptance:
- mixed `nanobot`/`eeebot` imports stay green
- no duplicate module identity issues
- no runtime/service path changes required
