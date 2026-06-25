# Source Of Truth And Promotion Policy

Last updated: 2026-04-15 UTC

## Why This Exists

`eeepc` now has two real development surfaces:

1. the stable product/runtime/control-plane code we own and deploy,
2. the mutable live host workspace/state that the running agent can evolve.

These must not be treated as the same thing.

If they are mixed together, the project becomes unrecoverable, non-repeatable, and hard to reason about.

## Core Rule

The host is an execution and evidence surface.
It is not the canonical source of truth for product code.

Canonical source of truth must remain in Git-managed local repositories.

## Current Live Authority Boundary On `eeepc`

There are two different kinds of truth that must be kept separate:

1. canonical product/source truth
2. live host execution truth

For `eeepc` as of 2026-04-15:

- canonical product/source truth remains in Git-managed repositories such as `ozand/eeebot`
- live self-evolving execution truth on the host currently lives under `/var/lib/eeepc-agent/self-evolving-agent/state`
- the deployed gateway workspace path under `/home/opencode/.nanobot-eeepc/workspace/state/...` must not be assumed to be the active self-evolving authority unless explicitly verified

Practical implication:

- a healthy gateway deploy does not by itself prove that the repo's workspace-state runtime slice is the authority used by the live host self-evolving loop
- live `BLOCK`/`PASS`, approval status, bounded apply results, and artifact backups must be read from the host control-plane state tree until convergence is completed

## Ownership Invariant

For this project, canonical source repositories must belong to the `ozand` GitHub namespace.

The bot-owned namespace must never be treated as the canonical owner of product source.

## The Two-Code-Domain Model

### Domain A - Canonical source repos

These are the repos we intentionally develop, version, review, and deploy.

Ownership rule:

- canonical source belongs under `ozand`
- bot-owned host-evolution namespaces must not become canonical source by accident

#### A1. `nanobot` engine repo

Path:

- `T:\Code\eeepc`

Owns:

- chat/gateway runtime
- agent loop
- providers
- channels
- config schema
- upstream/forked engine behavior

Examples:

- `repo_research/nanobot/nanobot/agent/loop.py`
- `repo_research/nanobot/nanobot/agent/improve_bridge.py`
- `repo_research/nanobot/nanobot/config/schema.py`

Canonical ownership strategy:

- use `ozand/eeebot` as the canonical owned fork,
- keep `HKUDS/nanobot` as `upstream` fetch source only,
- never treat upstream as the canonical push target.

Remote policy:

- `origin` = `ozand/eeebot`
- `upstream` = `HKUDS/nanobot`
- `remote.pushDefault = origin`

Branch policy:

- `main` tracks `origin/main`
- upstream intake uses a dedicated sync branch such as `sync/upstream-main`
- host-born promotions use reviewable branches such as `promote/eeepc-*`

#### A2. `eeepc` control-plane repo

Path:

- `T:\Code\servers_team\Project\servers\eeepc`

Owns:

- host-native runtime policy
- self-evolving execution plane
- bridge files
- deployment scripts
- systemd definitions
- runbooks and operational contracts

Examples:

- `Project/servers/eeepc/runtime/self_evolving_agent/app/*`
- `Project/servers/eeepc/runtime/systemd/*`
- `Project/servers/eeepc/scripts/*`
- `Project/servers/eeepc/docs/*`

### Domain B - Mutable host evolution plane

This is the live machine's changing state and bounded mutable workspace.

Examples:

- `/var/lib/eeepc-agent/self-evolving-agent/state/*`
- `/home/opencode/.nanobot-eeepc/sim_telegram/*`
- allowlisted target workspace files mutated by bounded apply
- host-local reports, reflections, outbox artifacts, and ledgers

This domain is operational state, not canonical source.

Current `eeepc` live authority note:

- the authoritative self-evolving runtime state currently observed on the live host is `/var/lib/eeepc-agent/self-evolving-agent/state`
- this includes reports, approvals, outbox artifacts, backups, and related cycle evidence
- repo-side workspace-state artifacts are still an implementation slice and must not be described as the live host authority unless the host is explicitly emitting them

GitHub namespace rule:

- host evolution repos may live under a bot-owned namespace,
- but canonical source promotion targets must remain `ozand` repositories.

## What Can Evolve On The Host

The host may autonomously change:

- bounded allowlisted workspace targets,
- prompts,
- selected config files,
- evidence/report outputs,
- local documentation inside the mutable target workspace,
- operational notes and generated artifacts.

The host may also generate:

- new proposals,
- promotion candidates,
- patch/evidence bundles,
- local recovery changes.

But these are not source-of-truth source code changes until promoted.

## Promotion Rule

Any host-born change that should become part of the product must follow a promotion path.

Promotion path:

1. bounded host change lands,
2. evidence is recorded,
3. a promotion artifact is created,
4. operator or policy review accepts it,
5. accepted change is committed back into the correct Git repo,
6. future deployments carry it forward.

Promotion target rule:

- promoted source changes must target canonical repositories owned under `ozand`,
- never a bot-owned host-evolution repo.

Without promotion, the change remains a host-local mutation.

## Live Reporting Rule

When describing live `eeepc` runtime behavior, operator summaries must identify which state tree they are derived from.

For the current host:

- use `/var/lib/eeepc-agent/self-evolving-agent/state` for live self-evolving truth
- do not present `/home/opencode/.nanobot-eeepc/workspace/state/...` as canonical live proof unless those artifacts are actually present and being used by the running host loop

## Deployment Rule

Never treat manual host patching as the canonical development path.

Expected path:

1. edit locally,
2. commit locally,
3. deploy release artifact to host,
4. keep state outside the release tree,
5. use host evidence to decide what to promote back.

## Git Sync Policy For Autonomous Cycles

Each autonomous cycle should end with Git-sync-style evidence, but not necessarily a direct code push into the canonical product repo.

There are two allowed outputs:

### Output 1 - Evidence sync

Always allowed.

The cycle exports compact versioned evidence such as:

- latest report summary,
- report index,
- changed paths,
- capability gate snapshot,
- backups/snapshots manifest,
- promotion candidate metadata.

This can be pushed to a project-side GitHub repository branch or archive path dedicated to host evidence.

### Output 2 - Source promotion sync

Only for accepted changes that should become canonical source.

This should not be an automatic blind push into main product code.
It should produce a reviewable artifact or commit candidate.

## Recommended Repo Layout On GitHub

### Repo 1 - `nanobot`

Own upstream/fork engine code.

### Repo 2 - `eeepc-control-plane`

Own self-evolving runtime, host deployment glue, policies, docs, and bridge contracts.

### Repo 3 or branch namespace - `eeepc-host-evidence`

Own compact exported host-cycle evidence and promotion candidates.

This can be:

- a separate repo,
- or a dedicated branch/folder policy inside `eeepc-control-plane`.

## What Should Be Versioned Every Cycle

At minimum, cycle-end sync should export:

- cycle timestamp
- goal id / lane / source
- result status
- follow-through status
- changed paths
- artifact paths
- capability gate snapshot
- latest report summary
- latest report index pointer
- promotion candidate metadata if present

## What Should Not Be Blindly Pushed Every Cycle

- full volatile state trees
- secrets or env files
- raw inbox task files
- unreviewed host-local source mutations into canonical source repos
- large noisy ledgers with no compaction boundary

## Immediate Implementation Direction

### Step 1 - Evidence export contract

Add a cycle-end export manifest under the eeepc runtime state and map it to a Git-safe export directory.

Suggested state file:

- `state/outbox/cycle_export_manifest.json`

Suggested exported repo path shape:

- `exports/host-cycles/YYYY/MM/DD/<cycle-id>/manifest.json`

### Step 2 - Promotion candidate contract

Add explicit promotion candidate files.

Suggested state paths:

- `state/promotions/candidates.jsonl`
- `state/promotions/events.jsonl`

### Step 3 - Git sync adapter

Introduce a dedicated sync script that:

- stages only approved export surfaces,
- writes a deterministic commit message,
- pushes to a non-destructive branch,
- never pushes secrets,
- never mutates product source repos directly without promotion rules.

## Recovery Principle

If the host self-damages, we should be able to restore it from:

1. canonical source repo commits,
2. release artifact version,
3. exported host evidence/promotion history,
4. state backups/snapshots.

This is the main reason the source/evidence/promotion split must exist.

## Current Project Decision

For `eeepc`, the official model is now:

- local Git repos are canonical,
- host changes are real but provisional,
- every important host cycle should export evidence,
- only promoted changes become canonical source.

Namespace clarification:

- `ozand` owns canonical product/control-plane source,
- `mrsmileystoke92` owns host-evolution planes only.
