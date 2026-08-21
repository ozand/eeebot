![cover-v5-optimized](./images/eeebot.png)

# eeebot

This repository is the canonical `eeebot` project for the eeepc self-improving runtime and its operator-facing control/dashboard workflow.

Important compatibility note:
- the repository/project name is now `eeebot`
- the internal Python package, CLI, and many runtime paths still use `nanobot` for compatibility with the existing deployed system, dashboard, and eeepc host
- the package now exposes both CLI entrypoints during the compatibility window:
  - `nanobot`
  - `eeebot`
- renaming the package/runtime internals should be treated as a separate migration, not mixed into the GitHub repo rename

It is not a plain mirror of the upstream `HKUDS/nanobot` repository.
It contains fork-specific work for:
- bounded self-improving runtime slices
- eeepc live authority integration
- experiment contracts, outcomes, frontier tracking, credits, and revert records
- proof-oriented docs and the host-side operator dashboard (`scripts/eeebot_dashboard.py`)

GitHub repositories:
- Canonical main project repo: https://github.com/ozand/eeebot
- Ops-dashboard repo (canonical for the dormant WSGI dashboard since #617,
  2026-07-05): https://github.com/ozand/eeebot-ops-dashboard
  - The dashboard actually running on eeepc is `scripts/eeebot_dashboard.py`
    (this repo, port 8080). The sibling repo holds the more capable,
    not-yet-deployed replacement, extracted from here with full history.
- Upstream source project: https://github.com/HKUDS/nanobot

## What this fork is for

This fork is focused on making Nanobot operationally useful for a real bounded self-improvement loop on `eeepc`, not just preserving upstream defaults.

Key fork-specific capabilities include:
- live authority reads from eeepc control-plane state
- durable self-evolving cycle reports under `workspace/state/`
- approval-gated bounded apply behavior
- promotion/write-path/read-path convergence notes and proofs
- experiment contracts with `keep` / `discard` / `crash` / `blocked`
- credits ledger and durable subagent/task correlation

The HADI hypothesis/WSJF prioritization surface and operator dashboard now live
in the separate `ozand/eeebot-ops-dashboard` repo (extracted in #617); the
live host dashboard in this repo is `scripts/eeebot_dashboard.py`.

## Canonical docs to start with

Top-level docs index:
- `docs/README.md`

End-to-end system reference (read this first):
- `docs/SYSTEM_OPERATION_REFERENCE.md` — roles, timers, coordinator cycle, artifacts, subagent bridge (cycle-branch → import-smoke → integrate-to-main), models, diagnostics
- `docs/OBSERVABILITY.md` — how to see what the system is doing
- `docs/ARCHITECTURE.md` — high-level map

Core operating docs:
- `docs/EEEBOT_SELF_IMPROVING_RUNTIME_OPERATING_CONTRACT.md`
- `docs/EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE.md`
- `docs/EEEBOT_BUDGET_AND_REWARD_MODEL.md`
- `docs/EEEBOT_EXPERIMENT_AND_OUTCOME_CONTRACT.md`
- `docs/EEEBOT_OPERATOR_WORKFLOW.md`

Capability specs (current product truth) live under `docs/specs/` — see `docs/specs/README.md`. Notably:
- `docs/specs/self-evolving-runtime/spec.md`, `docs/specs/subagent-bridge/spec.md`
- `docs/specs/promotion-and-release/spec.md`, `docs/specs/host-runtime/spec.md`
- `docs/specs/migration/spec.md` (nanobot → eeebot rename guardrails, in progress)

Host runbooks:
- `docs/EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md`
- `docs/EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md`

Tasks/backlog: GitHub Issues + status labels (not a markdown file). See `AGENTS.md`.
Archived/superseded docs were removed 2026-07-05 (#613); recoverable from git history.

## Upstream relationship

We still track upstream `HKUDS/nanobot`, but we do not blindly merge everything.

Merge policy for this fork:
- take safe, high-value upstream fixes selectively
- avoid merges that would break the eeepc 32-bit/runtime constraints
- preserve fork-specific bounded self-improvement behavior and operator surfaces

Examples of upstream updates that are good candidates:
- session durability and corruption repair
- safe subagent/session routing fixes
- narrowly-scoped agent reliability improvements

Examples that require extra scrutiny before merge:
- packaging/layout changes
- provider/model defaults
- heavy WebUI or channel changes
- features that assume environments unavailable on eeepc

## LiteLLM configuration — single source of truth

All LiteLLM credentials and routing settings for the eeepc runtime live in **one place**:

```
/etc/eeepc-agent/litellm.env
```

**Never set `LITELLM_API_KEY`, `LITELLM_BASE_URL`, or `LITELLM_MODEL` anywhere else.**

### File layout

| File | Role |
|------|---------|
| `/etc/eeepc-agent/litellm.env` | **Source of truth** — key, endpoint, model, timeouts |
| `/etc/eeepc-agent/instances/self-evolving-subagent-bridge.env` | Bridge-specific settings only (state dirs, sync flags) |
| `/etc/systemd/system/eeepc-self-evolving-subagent-bridge.service.d/override.conf` | systemd drop-in that injects `litellm.env` into the bridge service |
| `/home/opencode/.nanobot-eeepc/config.template.json` | Gateway wrapper template — reads key from same values |

### Key rotation procedure

```bash
# 1. Edit only litellm.env
sudo nano /etc/eeepc-agent/litellm.env

# 2. Restart the service — no other files need touching
sudo systemctl restart eeepc-self-evolving-subagent-bridge.service

# 3. Verify
curl -s -o /dev/null -w "%{http_code}" \
  "$LITELLM_BASE_URL/models" \
  -H "Authorization: Bearer <new-key>"
# expect: 200
```

Current endpoint/key/model configuration lives only in
`/etc/eeepc-agent/litellm.env` on the host — it is not duplicated here. Models
are referenced with the `cl/`, `an/`, or `un/` gateway prefixes required by the
proxy.

## Current state

This repo should be understood as:
- a maintained operational eeebot repository
- still carrying `nanobot` compatibility internally in code/runtime names
- not a vanilla upstream checkout
- not a marketing landing page

## Guarded self-evolution scripts

The dev-machine `systemd/` units and `scripts/install_user_units.sh` that used
to drive a local repo-side self-improving cycle on this host were removed in
#597/#613 (recoverable from git history; the host-side eeepc runtime does not
depend on them). The guarded self-evolution *scripts* themselves are still
live product code, invoked by `nanobot/runtime/autoevolve.py` and covered by
`tests/test_autoevolve*.py`:
- `scripts/create_candidate_release.py`
- `scripts/health_check_release.py`
- `scripts/guarded_self_evolve.py`
- `scripts/commit_and_push_self_evolution.py`

This path creates a self-mutation request, commits/pushes tracked source
changes, creates a candidate release from the current git commit, applies it
through a release directory/current symlink, runs an automatic health gate,
and writes rollback/failure-learning artifacts if the gate fails. See
`docs/specs/promotion-and-release/spec.md` for the current contract and env
variables.

For current runtime and dashboard state, see the fork docs and the separate dashboard repo.
