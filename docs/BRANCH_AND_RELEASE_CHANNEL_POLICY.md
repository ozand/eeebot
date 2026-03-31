# Branch And Release Channel Policy

Last updated: 2026-03-28 UTC

## Purpose

This document defines branch intent, release channels, and merge directions for
the `eeepc` workstream.

Its goal is to prevent confusion between:

- canonical development,
- upstream intake,
- host-born promotion,
- deployable releases,
- emergency recovery work.

## Branch Taxonomy

### Canonical branches

- `main` - stable canonical source,
- `nightly` - experimental integration branch when used.

### Upstream sync branches

- `sync/upstream-*` - intake from upstream sources only.

These are not direct feature-development branches.

### Promotion branches

- `promote/eeepc-*`
- `promote/host-*`

These exist for reviewable host-born changes returning toward canonical source.

### Release branches or tags

- `release/*` or immutable release tags.

These identify approved deployable states.

### Emergency branches

- `hotfix/*`
- `emergency/*`

These are reserved for urgent recovery or high-priority incident work.

## Branch Ownership Rules

- canonical source branches belong under maintainer-owned canonical repos,
- upstream sync branches are for intake, not for silent product ownership,
- host-evidence repos are not canonical product branches,
- host-born changes must not land directly in `main` without promotion review.

## Release Channels

### Stable channel

Backed by:

- approved canonical branches or tags,
- validated release artifacts,
- rollback-ready deployment records.

### Candidate channel

Backed by:

- pre-release validation states,
- reviewable branch heads,
- not-yet-final deployment candidates.

### Sync channel

Backed by:

- upstream intake branches,
- merge exploration,
- compatibility work.

### Promotion channel

Backed by:

- host-born candidates,
- evidence-linked review branches,
- replay or cherry-pick candidates.

### Emergency channel

Backed by:

- urgent recovery patches,
- temporary stabilization work,
- follow-up canonicalization requirements.

## Merge Direction Rules

Allowed directions should be explicit.

Examples:

- upstream -> `sync/upstream-*`
- maintainer work -> review branch -> canonical branch
- host-born candidate -> `promote/*` -> reviewed canonical branch
- canonical branch -> release artifact
- release artifact -> host deployment

Avoid:

- host evidence repo -> canonical `main` direct merge,
- upstream -> canonical stable direct overwrite,
- emergency branch becoming long-lived default development surface.

## Which Branches Produce Deployable Artifacts

Deployable artifacts should come only from:

- approved stable branches,
- approved candidate branches when explicitly testing,
- immutable release tags.

The following should not be normal deployment sources:

- raw sync branches,
- raw promotion branches,
- evidence-only branches,
- arbitrary host snapshots.

## Promotion Requirements By Branch Type

For promotion branches:

- evidence linkage is required,
- target canonical repo/branch must be explicit,
- replayability should be verified,
- conflict handling should happen outside the live host,
- merge should not skip review unless policy explicitly allows it for low-risk cases.

## Emergency Branch Rules

Emergency branches are valid only when:

- availability or recoverability is at risk,
- waiting for the normal path is too costly,
- the change is bounded and explainable.

After emergency use:

- normalize the patch into canonical history,
- rebuild a normal artifact,
- retire the emergency branch.

## Branch Hygiene

Branches should reveal intent in their names.

Keep branch classes distinct so it is always clear whether a branch is for:

- stable development,
- upstream sync,
- promotion,
- release preparation,
- emergency recovery.

Stale promotion and emergency branches should not linger indefinitely.

## Practical Rule

If a branch name does not make its role obvious, it likely weakens governance.

If a branch could accidentally become both an evidence lane and a canonical source
lane, the policy is too ambiguous and should be tightened.
