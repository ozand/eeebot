# Validation Hooks Plan

Last updated: 2026-03-28 UTC

## Purpose

This document defines where executable validation should run and what each hook is
responsible for proving.

It is the bridge between governance prose and enforcement.

## Validation Principles

- canonical source is authoritative,
- host state is observable but provisional,
- every meaningful object should have stable identity and traceable provenance,
- evidence is required before promotion,
- artifact-first deployment is the default,
- drift must be classified before reconciliation acts.

## Validation Surfaces

### Build-time hooks

Run during artifact creation and packaging.

Validate at least:

- source commit linkage,
- artifact metadata completeness,
- included/excluded path rules,
- absence of unapproved host-local overlays,
- declared build inputs.

### Promotion-time hooks

Run when a host-born change becomes a promotion candidate.

Validate at least:

- candidate identity and origin cycle linkage,
- evidence completeness,
- replayability outside the live host,
- target repo and branch validity,
- rollback or rejection path presence.

### Deploy-time hooks

Run before and after rollout.

Pre-deploy validation:

- artifact identity,
- host profile compatibility,
- release channel consistency,
- required rollback target availability.

Post-deploy validation:

- startup stability,
- operator control path,
- truthful capability reporting,
- evidence write path,
- weak-host resource fit.

### Runtime cycle export hooks

Run when an autonomous cycle emits manifests or evidence.

Validate at least:

- cycle record completeness,
- evidence record integrity,
- changed-path summary presence,
- capability snapshot freshness,
- promotion metadata when applicable.

### Reconciliation hooks

Run when drift is detected or periodic reconciliation occurs.

Validate at least:

- drift classification presence,
- whether the chosen action matches the trust level,
- whether canonical truth is protected,
- whether repair, rollback, quarantine, or rebuild is properly justified.

## Hook Categories

Suggested hook families:

- `pre_build`
- `post_build`
- `pre_promotion`
- `post_promotion`
- `pre_deploy`
- `post_deploy`
- `cycle_export`
- `reconciliation`
- `periodic_audit`

## Minimum Validated Record Types

The first validation layer should cover:

- cycle records,
- promotion candidates,
- release artifacts,
- deployment fingerprints,
- evidence references,
- deploy decisions,
- drift classifications.

## First Checks To Implement

Prioritize checks that prevent loss of provenance and ownership confusion:

1. stable identity presence and uniqueness,
2. required provenance links,
3. valid canonical target repo/branch,
4. evidence completeness for promotion,
5. rollback path presence,
6. artifact-to-source linkage,
7. deployment fingerprint completeness,
8. drift classification completeness.

## Failure Handling Policy

- block publish or promotion when identity or provenance is missing,
- reject invalid ownership targets,
- quarantine suspicious runtime exports,
- force rollback or rebuild when unsafe drift is confirmed,
- emit structured rejection reasons.

## Validation Outputs

Every validation result should record at least:

- timestamp,
- hook stage,
- inputs inspected,
- result,
- failure reason if any,
- linked IDs and references.

Validation outputs should themselves be structured records.

## Practical Rule

Validation should run at every trust boundary, not only at deploy time.
If provenance, ownership, replayability, or rollback becomes ambiguous, the hook
should fail closed or escalate for safer handling.
