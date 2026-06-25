# Change Propagation Model

Last updated: 2026-03-28 UTC

## Purpose

This document defines how changes move between:

- local maintainer development,
- canonical Git repositories,
- release artifacts,
- the live weak-host runtime,
- host evidence and promotion surfaces.

The core rule is simple:

the host may evolve, but it is not the canonical source of truth.

## System Surfaces

The project operates across four change surfaces.

### 1. Local development surface

Where maintainers design, implement, test, and review changes to the base system.

### 2. Canonical source surface

Maintainer-owned Git repositories under the canonical namespace.

This is where durable product/control-plane source ultimately belongs.

### 3. Release artifact surface

Versioned deployable builds created from canonical source.

This is the normal delivery mechanism to the weak host.

### 4. Live host evolution surface

The running host where bounded local mutation, evidence generation, and promotion
candidate creation may occur.

## Preferred Steady-State Model

The default propagation model is:

1. local development,
2. review and canonical merge,
3. release artifact build,
4. artifact deployment to host,
5. bounded host evolution,
6. evidence-backed promotion of worthy host-born changes.

This is an artifact-first, promotion-first model.

It intentionally avoids treating direct host patching as the normal development or
delivery path.

## Propagation Classes

### Class A - Maintainer-originated source changes

Examples:

- engine code,
- control-plane code,
- baseline config,
- policy files,
- bootstrap/recovery logic.

Default path:

- local change -> review -> canonical repo -> artifact -> deploy.

### Class B - Host-born provisional changes

Examples:

- bounded self-edits,
- local tool growth,
- host tuning,
- runtime-generated utility logic,
- operator-facing outputs created by the running system.

Default path:

- local host mutation -> evidence -> promotion candidate or local-only retention.

### Class C - Evidence-only outputs

Examples:

- reports,
- manifests,
- summaries,
- drift classifications,
- capability snapshots.

Default path:

- host evidence generation -> evidence sync surface.

### Class D - Emergency recovery changes

Examples:

- live incident hotfix,
- temporary service repair,
- emergency host patch to restore minimal runtime.

Default path:

- emergency patch -> incident evidence -> canonical follow-up -> next artifact
  deploy.

## Artifact-First Deployment Rule

The weak host should normally receive versioned release artifacts rather than raw
source edits.

Why:

- artifacts are easier to reason about,
- rollback is simpler,
- code/state separation is clearer,
- portability to similar hosts is stronger,
- deploy provenance is easier to audit.

## Promotion Rule

Host-born changes remain provisional until promoted.

Promotion requires:

- a candidate diff or patch bundle,
- evidence that explains the change,
- a valid canonical target,
- review or policy acceptance,
- replayability on canonical source.

Host evidence alone is not promotion.

## Host Divergence Model

Divergence between canonical source and the live host is expected.

But divergence must be classified:

- `expected_provisional` - allowed bounded host evolution,
- `promotion_candidate` - useful and worthy of canonical consideration,
- `stale_provisional` - no longer justified and should be resolved,
- `unsafe_divergence` - threatens explainability, recovery, or invariants.

## Reconciliation Rule

When host divergence is meaningful, the system should not blindly overwrite one
side with the other.

The reconciliation order is:

1. classify the divergence,
2. decide whether it is local-only, promotable, stale, or unsafe,
3. export evidence,
4. replay worthy changes against canonical source outside the live host,
5. redeploy a clean artifact if canonicalization is accepted,
6. rebuild from baseline if drift is no longer trustworthy.

## Emergency SSH Exception

Direct SSH patching is acceptable only when:

- the system is in incident mode,
- artifact delivery is unavailable or too slow for recovery,
- restoring minimal trusted runtime is the immediate priority.

Even then:

- scope must stay bounded,
- the patch must be recorded,
- a canonical follow-up is mandatory,
- the host should return to artifact-managed state as soon as possible.

SSH patching is never the preferred steady-state workflow.

## Audit And Traceability

Every significant change should preserve enough metadata to answer:

- where it was born,
- why it was made,
- what it changed,
- whether it was deployed,
- whether it stayed local or was promoted,
- how to recover if it fails.

## Practical Decision Rule

When deciding how to move a change, ask:

- is this a base-system improvement,
- a host-local experiment,
- an evidence-only output,
- or an emergency recovery action?

Then choose the smallest path that preserves:

- canonical ownership,
- evidence,
- replayability,
- rollback,
- portability to similar weak hosts.
