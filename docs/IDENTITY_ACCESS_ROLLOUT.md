# Identity, Permissions, And GitHub Access Rollout

Last updated: 2026-03-28 UTC

## Purpose

This document defines the staged identity and permission model for getting the
bot to useful autonomous work quickly without giving one actor too much power too
early.

The guiding principle is:

separate local runtime rights, evidence publishing rights, promotion rights, and
human override rights.

## Core Rules

- canonical source belongs under `ozand`,
- host-evolution outputs belong in a separate bot-owned namespace,
- direct canonical `main` mutation is never part of the fast path,
- read broadly, write narrowly,
- deny by default.

## Identity Model

### Owner / Operator

Purpose:

- final authority,
- review and approval,
- recovery and break-glass intervention.

May:

- approve promotions,
- change policy,
- rotate credentials,
- intervene during incident recovery.

### Maintainer

Purpose:

- architect and orchestrator of the base system.

May:

- design and review architecture,
- promote accepted changes,
- manage backlog and policy surfaces.

### Runtime Agent

Purpose:

- day-to-day bounded autonomous work on the host.

May:

- inspect code, docs, tests, runtime state,
- write only to allowlisted local surfaces,
- generate evidence, manifests, and bounded patch candidates,
- run approved actions through the executor lane.

May not:

- access secrets by default,
- mutate canonical source directly,
- change system-wide policy,
- hold broad admin rights.

### Evidence Sync Actor

Purpose:

- publish compact evidence and workspace outputs.

May:

- push to bot-owned evidence/workspace/projects-index repos.

May not:

- push to canonical `ozand` repos,
- publish secrets or raw private state.

### Promotion Actor

Purpose:

- prepare reviewable canonical candidates.

May:

- create review branches,
- open PR-ready promotion candidates,
- attach evidence and validation summaries.

May not:

- push directly to `main`,
- self-merge,
- treat host-local drift as canonical truth.

## Local Permission Model

### Allowed by default

- read code, docs, tests, logs, and bounded runtime state,
- write reports, evidence, manifests, and allowlisted mutation targets,
- create bounded helper outputs and action artifacts.

### Not allowed by default

- secrets and credential stores,
- admin-level OS control,
- unrestricted filesystem writes,
- direct canonical source mutation,
- unrestricted service or network policy changes.

## Namespace Boundaries

### Canonical namespace

- owned by `ozand`,
- contains trusted product/control-plane source,
- accepts changes only through reviewable promotion.

### Host-evolution namespace

- owned by the bot-side namespace such as `mrsmileystoke92`,
- contains evidence, workspace outputs, project seeds, and autonomous project
  scaffolding,
- is never the source of truth for canonical product code.

## GitHub Rollout Stages

### Stage 0 - No GitHub write

Goal:

- local-only operation and truth-building.

Allowed:

- inspect local state,
- simulate export and promotion paths,
- no remote writes.

### Stage 1 - Read-only GitHub access

Goal:

- let the bot inspect remote repo state safely.

Allowed:

- fetch and inspect canonical/upstream state,
- no pushes or PRs.

### Stage 2 - Bot-namespace evidence publishing

Goal:

- give the bot durable external memory and evidence publishing.

Allowed:

- push to host-evidence, host-workspace, and projects-index repos in the bot
  namespace.

Not allowed:

- canonical source writes.

### Stage 3 - Bot-namespace project bootstrap

Goal:

- let the bot create and seed autonomous project repos in its own namespace.

Allowed:

- create repos in the bot-owned namespace,
- publish seed manifests and scaffolding.

### Stage 4 - Canonical promotion via PR-only access

Goal:

- allow host-born value to return to canonical source in a reviewable way.

Allowed:

- create promotion branches,
- open PRs,
- attach evidence.

Not allowed:

- direct push to `main`,
- auto-merge,
- branch protection bypass.

## Practical Recommended Order

1. establish the local runtime identity,
2. enable read-only GitHub access,
3. enable evidence export,
4. enable bot-namespace project creation,
5. enable canonical PR-only promotion.

## Minimum Rights For Fast Usefulness

To get the bot autonomous sooner, it only needs:

- bounded local read/write on runtime and allowlisted workspace surfaces,
- read-only remote repo visibility,
- bot-namespace evidence publishing,
- later project bootstrap rights in the bot namespace.

It does not initially need:

- canonical merge rights,
- broad deploy rights,
- admin/system-level privileges,
- unrestricted GitHub permissions.

## Safe Defaults

- `opencode` remains supervisor/reviewer,
- the autonomous runtime uses a dedicated low-privilege service identity,
- canonical source remains PR-only,
- evidence and project surfaces stay separate from canonical source,
- no single credential combines authoring, merging, and deployment.

## Exit Criteria For Promotion To The Next Stage

Advance stages only when:

- current actions are truthful and evidence-backed,
- permissions are bounded and audited,
- rollback is defined,
- namespace boundaries remain clear,
- the weak host stays healthy under load.

## Immediate Next Step

Status note: this section is planning guidance, not a guarantee that the listed step is still the current live rollout action.

The immediate rollout step should be:

- define the local runtime identity and bounded local permissions,
- then enable read-only GitHub access,
- then enable bot-namespace evidence publishing.
