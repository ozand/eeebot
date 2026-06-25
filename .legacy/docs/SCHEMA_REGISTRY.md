# Schema Registry

Last updated: 2026-03-28 UTC

## Purpose

This document is the canonical registry of machine-readable schema types used by
the `eeepc` governance, deploy, promotion, provenance, and reconciliation model.

Its job is to make every important structured record:

- identifiable,
- versioned,
- owned,
- validateable.

## Scope

The registry covers schemas for:

- provenance and identity records,
- promotion and replay records,
- release and deployment records,
- evidence and validation records,
- drift and reconciliation records,
- deploy decision records.

It does not cover:

- ad hoc notes,
- free-form logs with no stable governance role,
- purely internal objects that are never persisted or exchanged.

## Registry Principles

- schemas should be machine-readable and validated,
- schema IDs are stable and must not be reused for different meanings,
- published schema versions are immutable,
- unknown or incompatible versions should fail closed,
- host-local state may emit records, but it must not redefine schema meaning,
- canonical source remains the source of truth for registry definitions.

## Naming And Versioning

Suggested naming pattern:

- `schema_id` = stable dotted name, such as `eeepc.cycle_record`
- `schema_version` = semantic version such as `1.0.0`

Versioning rule:

- major = breaking meaning or shape change,
- minor = backward-compatible additive change,
- patch = non-breaking clarification where supported.

## Registry Catalog

### `eeepc.schema_registry`

Purpose:

- registry manifest of approved schemas, versions, status, and owners.

Primary owner:

- maintainers / governance.

### `eeepc.identity_primitives`

Purpose:

- shared identity rules for stable IDs and tuples.

Primary owner:

- maintainers / governance.

### `eeepc.cycle_record`

Purpose:

- bounded execution record for a host cycle.

Primary owner:

- runtime / host agent.

### `eeepc.evidence_ref`

Purpose:

- reference to a content-addressed or URI-backed evidence item.

Primary owner:

- runtime / evidence subsystem.

### `eeepc.promotion_candidate`

Purpose:

- host-born change package eligible for promotion review.

Primary owner:

- promotion pipeline.

### `eeepc.release_artifact`

Purpose:

- versioned deployable artifact built from canonical source.

Primary owner:

- build/release pipeline.

### `eeepc.deployment_fingerprint`

Purpose:

- machine-readable identity for what is actually installed and running.

Primary owner:

- deploy/runtime layer.

### `eeepc.deploy_decision`

Purpose:

- structured record of rollout, reconcile, rollback, or rebuild choice.

Primary owner:

- deploy controller / policy engine.

### `eeepc.drift_classification`

Purpose:

- closed-set classification of bounded, stale, promotable, or unsafe drift.

Primary owner:

- reconciliation subsystem.

### `eeepc.reconciliation_record`

Purpose:

- record of repair, quarantine, rollback, replay, or rebuild action.

Primary owner:

- reconciliation subsystem.

### `eeepc.rollback_event`

Purpose:

- structured description of rollback trigger, target, and result.

Primary owner:

- deploy/runtime layer.

### `eeepc.change_propagation_event`

Purpose:

- record of movement between local, canonical, artifact, host, and promotion
  surfaces.

Primary owner:

- governance / pipeline layer.

### `eeepc.validation_result`

Purpose:

- structured outcome of checks, policy evaluations, or smoke validations.

Primary owner:

- validation pipeline.

## Shared Record Shape Guidance

Where relevant, records should use a common baseline such as:

- `schema_id`
- `schema_version`
- `record_id`
- `created_utc`
- `owner`
- `status`
- `references`
- `content_hash`

Provenance-heavy records should additionally use fields like:

- `origin_cycle_id`
- `promotion_candidate_id`
- `artifact_id`
- `deployment_fingerprint_id`
- `evidence_ref_id`

## Ownership And Stewardship

Each schema should have one accountable owner role.

Ownership means authority to evolve the schema definition, not authority to
silently rewrite historical records.

## Validation Expectations

Validation should check at least:

- required field presence,
- stable identity presence,
- provenance linkage integrity,
- repo/branch and ownership validity when relevant,
- compatibility of schema version,
- content-hash integrity where available.

## Deprecation And Migration

- deprecated schemas should remain readable for a defined support window,
- breaking changes require a version or schema identity transition,
- producers should stop emitting end-of-life schema versions,
- migration should happen through explicit versioning, not silent mutation.

## Practical Rule

If a record matters for deploy, promote, reconcile, rollback, audit, or runtime
truthfulness, it should have a registered schema identity and version before it is
treated as governance-grade data.
