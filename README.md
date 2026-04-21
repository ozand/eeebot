# eeebot

This repository is the canonical owned eeebot project for the eeepc self-improving runtime and its operator-facing control/dashboard workflow.

Important compatibility note:
- the repository/project name is now `eeebot`
- the internal Python package, CLI, and many runtime paths still use `nanobot` for compatibility with the existing deployed system, dashboard, and eeepc host
- the package now exposes both CLI entrypoints during the compatibility window:
  - `nanobot`
  - `eeebot`
- renaming the package/runtime internals should be treated as a separate migration, not mixed into the GitHub repo rename

It is not a plain mirror of `HKUDS/nanobot`.
It contains fork-specific work for:
- bounded self-improving runtime slices
- eeepc live authority integration
- HADI + WSJF hypothesis/prioritization surfaces
- experiment contracts, outcomes, frontier tracking, credits, and revert records
- local operator dashboard integration and proof-oriented docs

GitHub repositories:
- Main project repo: https://github.com/ozand/eeebot
- Dashboard: https://github.com/ozand/eeebot-ops-dashboard
- Upstream source project: https://github.com/HKUDS/nanobot

## What this fork is for

This fork is focused on making Nanobot operationally useful for a real bounded self-improvement loop on `eeepc`, not just preserving upstream defaults.

Key fork-specific capabilities include:
- live authority reads from eeepc control-plane state
- durable self-evolving cycle reports under `workspace/state/`
- approval-gated bounded apply behavior
- promotion/write-path/read-path convergence notes and proofs
- HADI hypothesis backlog with explicit WSJF surface
- experiment contracts with `keep` / `discard` / `crash` / `blocked`
- credits ledger and durable subagent/task correlation

## Canonical docs to start with

Recommended reading for this fork:
- `docs/NANOBOT_COMPLETION_CONTRACT.md`
- `docs/NANOBOT_FINAL_COMPLETION_SUMMARY.md`
- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_USAGE.md`
- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_LIVE_VERIFICATION_2026-04-16.md`
- `docs/EEEPC_AGENT_RUNTIME_INSTRUCTIONS.md`

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

## Current state

This repo should be understood as:
- a maintained operational eeebot repository
- still carrying `nanobot` compatibility internally in code/runtime names
- not a vanilla upstream checkout
- not a marketing landing page

For current runtime and dashboard state, see the fork docs and the separate dashboard repo.
