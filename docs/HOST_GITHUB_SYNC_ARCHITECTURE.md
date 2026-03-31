# Host GitHub Sync Architecture

Last updated: 2026-03-27 UTC

## Purpose

This document defines how `eeepc` should synchronize host-evolution outputs to a GitHub namespace that is separate from the canonical local development repositories.

## Separation Of Responsibilities

### Canonical repos

- `T:\Code\eeepc`
- `Project/servers/eeepc`

These remain the source of truth for product and control-plane code.

### Host evolution namespace

The host should push only to a separate GitHub account/namespace used for:

- cycle-end evidence exports,
- mutable host workspace snapshots,
- promotion candidates,
- autonomous project repos created by the bot itself.

The host evolution namespace is not allowed to silently overwrite canonical product source.

Current namespace decision:

- host evolution namespace = `mrsmileystoke92`
- canonical source namespace = `ozand`

Non-overlap rule:

- no canonical source repository may live under `mrsmileystoke92`
- no host-evolution plane should become the canonical source-of-truth repo

## Required Host Components

### 1. `gh` CLI

The host must have GitHub CLI installed and reachable as `gh`.

### 2. Dedicated auth material

The host must authenticate to the separate GitHub account only.

Expected auth surfaces:

- `gh auth login --with-token`
- optional dedicated git credential helper for that namespace

### 3. Export-first workflow

Each cycle should first produce a compact export manifest:

- `state/outbox/cycle_export_manifest.json`

Then a sync adapter can stage and push only approved export surfaces.

## Recommended GitHub Layout

### Repo A - `eeepc-host-evidence`

Contains compact per-cycle manifests and report summaries.

Suggested path layout:

- `exports/host-cycles/YYYY/MM/DD/<cycle-id>/manifest.json`

Retention recommendation:

- keep full per-cycle manifests for a limited rolling window,
- keep long-lived daily/weekly aggregates forever,
- keep all degraded/failure/promotion-linked cycles indefinitely.

### Repo B - `eeepc-host-workspace`

Contains bounded exported mutable workspace snapshots only when explicitly allowed.

Suggested contents:

- `exports/workspace-cycles/YYYY/MM/DD/<cycle-id>/manifest.json`
- `exports/workspace-cycles/YYYY/MM/DD/<cycle-id>/files/<relpath>`
- `exports/workspace-cycles/YYYY/MM/DD/<cycle-id>/patches/<relpath>.patch`
- `exports/latest_workspace.json`

Safety rules for this repo:

- export only allowlisted applied files,
- never export secrets or env files,
- never export full raw state trees,
- never mix compact evidence payloads into this repo.

### Repo C - `eeepc-projects-index`

Contains metadata for project repos autonomously created by the host.

Suggested contents:

- `registry/projects.json`
- `projects/<project-id>/project.json`
- `projects/<project-id>/seed-history.jsonl`
- `exports/project-seeds/YYYY/MM/DD/<seed-id>/manifest.json`
- `exports/latest_projects.json`

This repo should contain control metadata only.
It should not contain the full code of autonomous projects.

### Repo D+ - autonomous project repos

Created by the bot when justified by product work.

Recommended trigger conditions:

- repeated goal lineage beyond bounded host mutation scope,
- operator-explicit project bootstrap request,
- promotion pressure that no longer fits allowlisted host mutation,
- adopted recommendation that needs an independent lifecycle.

Runtime source of truth:

- these trigger rules should be treated as runtime policy,
- the canonical machine-readable definition belongs in `runtime/self_evolving_agent/config/policy.yaml` under `project_seed_manifest_v1`.

## Safety Rules

- Never push secrets or env files.
- Never push the full volatile state tree.
- Never push host-born source changes directly into canonical repos.
- Promotion into canonical repos must remain reviewable.
- Never treat `mrsmileystoke92` repos as canonical source repos.
- Promotion targets for product source must point to `ozand`-owned repos only.

## Immediate Implementation Steps

1. install `gh` on host,
2. authenticate `gh` to the separate namespace,
3. deploy cycle export bridge,
4. add Git sync adapter that pushes only evidence surfaces,
5. add repo bootstrap path for autonomous project creation later.

## Growth And Retention Guidance

### Evidence repo

- good for compact manifests, summaries, and promotion pointers,
- not good as a forever-growing copy of every full payload.

Recommended practice:

- compact per-cycle manifests by default,
- aggregate into daily and weekly summaries,
- retain raw per-cycle detail only for recent windows or significant events.

Implemented MVP direction:

- keep full per-cycle exports in a short rolling window,
- generate daily aggregate summaries under `exports/host-days/YYYY/MM/DD.json`,
- pin degraded/failure-like cycles beyond the normal rolling window,
- avoid changing the existing `exports/latest.json` contract.

### Workspace repo

- intended for evolving code/docs/artifacts produced by host autonomy,
- should store bounded file copies and patches,
- should be separate from evidence so repository history remains legible.

### Autonomous project repos

- create only when work becomes a genuine project rather than a single bounded host mutation,
- register those repos in a projects-index repo instead of hiding them in host evidence.

### Projects index repo

- use this as the control metadata plane for project creation,
- emit a seed manifest first,
- create the repo second,
- bootstrap initial files third,
- keep every transition append-only and reviewable.

## Current Blocking Items

- host `gh` is not installed yet,
- separate GitHub credentials must be confirmed,
- token should be rotated if it was exposed in chat history.
