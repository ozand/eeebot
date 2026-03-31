# Version And Provenance Model

Last updated: 2026-03-28 UTC

## Purpose

This document defines the machine-checkable identity and provenance model for:

- canonical source revisions,
- host cycles,
- promotion candidates,
- release artifacts,
- deployment fingerprints,
- evidence records.

The goal is to make every meaningful runtime and promotion object traceable.

## Core Rule

Every deployable or promotable object should be explainable through stable
identity, explicit provenance links, and enough metadata to recover the path from
canonical source to live host behavior.

Ambiguous provenance should fail closed.

## Core Entities

### Canonical source revision

The immutable source revision from maintainer-owned canonical repositories.

Suggested fields:

- `source_repo`
- `source_branch`
- `source_commit`
- `source_commit_timestamp_utc`
- `source_tree_hash`

### Host cycle

A bounded autonomous execution interval on the live host.

Suggested fields:

- `cycle_id`
- `cycle_type`
- `cycle_started_utc`
- `cycle_ended_utc`
- `origin_host`
- `goal_id`
- `lane`
- `result_status`

### Promotion candidate

A host-born change package eligible for review and possible canonicalization.

Suggested fields:

- `promotion_candidate_id`
- `origin_cycle_id`
- `candidate_created_utc`
- `target_repo`
- `target_branch`
- `base_commit`
- `candidate_patch_hash`
- `source_paths`
- `evidence_refs`
- `validation_summary`
- `resource_impact_summary`
- `rollback_plan`
- `review_status`
- `decision`

### Release artifact

A versioned deployable bundle built from canonical source and approved inputs.

Suggested fields:

- `artifact_id`
- `artifact_version`
- `artifact_kind`
- `source_repo`
- `source_commit`
- `build_timestamp_utc`
- `build_recipe_hash`
- `release_channel`
- `target_host_profile`
- `compatibility_constraints`
- `artifact_hash`

### Deployment fingerprint

The identity tuple describing what is actually installed and running on a host.

Suggested fields:

- `deployment_fingerprint_id`
- `artifact_id`
- `artifact_version`
- `engine_sha`
- `control_plane_sha`
- `policy_hash`
- `release_channel`
- `target_host_profile`
- `installed_at_utc`
- `runtime_state_hash`

### Evidence record

A proof object linking observations, validations, or outputs to a cycle,
candidate, deploy, or rollback event.

Suggested fields:

- `evidence_ref_id`
- `evidence_type`
- `uri`
- `content_hash`
- `produced_by_cycle_id`
- `related_candidate_id`
- `captured_at_utc`
- `format_version`

## Identity Primitives

The following identifiers should be treated as stable primitives:

- `cycle_id`
- `promotion_candidate_id`
- `artifact_id`
- `deployment_fingerprint_id`
- `evidence_ref_id`

Once assigned, these identities should not be reused or silently rewritten.

## Tuple-Based Identity

For stronger provenance, the project should prefer explicit identity tuples rather
than only opaque names.

### Artifact identity tuple

Suggested fields:

- `artifact_id`
- `source_repo`
- `source_commit`
- `build_recipe_hash`
- `artifact_hash`
- `target_host_profile`
- `release_channel`

### Deployment identity tuple

Suggested fields:

- `artifact_id`
- `artifact_version`
- `engine_sha`
- `control_plane_sha`
- `policy_hash`
- `target_host_profile`
- `runtime_state_hash`

### Promotion identity tuple

Suggested fields:

- `promotion_candidate_id`
- `origin_cycle_id`
- `base_commit`
- `candidate_patch_hash`
- `target_repo`
- `target_branch`

## Required Relationships

The following relationships should hold:

- each `promotion_candidate` should reference one `origin_cycle_id`,
- each `release_artifact` should reference one canonical `source_commit`,
- each `deployment_fingerprint` should reference one `artifact_id`,
- each `evidence_ref` should be attributable to the producing cycle or event,
- each deployable artifact should be explainable without relying on mutable host
  state as the source of truth.

## Invariants

### Identity invariants

- `cycle_id` is immutable once assigned,
- `promotion_candidate_id` is immutable once assigned,
- `artifact_id` is immutable once published,
- `deployment_fingerprint_id` is immutable once recorded,
- `evidence_ref_id` is immutable once recorded.

### Provenance invariants

- every deployable artifact must point to a specific canonical `source_commit`,
- every promotion candidate must point to a specific `origin_cycle_id`,
- every deployment fingerprint must point to a specific artifact identity,
- evidence should be content-verifiable whenever practical.

During early bounded rollout work, placeholder provenance may temporarily exist,
but it should be treated as degraded provenance rather than normal-good state.

### Completeness invariants

- a promotion candidate without `evidence_refs` is incomplete,
- an artifact without `source_commit` is invalid,
- a deployment fingerprint without `artifact_id` is invalid,
- a cycle without outcome metadata is incomplete for governance.

### Boundary invariants

- host-local mutable state must not redefine canonical identity,
- evidence repos must not be treated as canonical source,
- release artifacts must not absorb unapproved host-local overlays,
- direct host edits do not become canonical until promoted.

## Policy Statements

- canonical source is the durable source of truth,
- host state is observable and meaningful, but provisional,
- every autonomous cycle should emit a compact provenance record,
- every promotion candidate must be evidence-backed,
- every deployment should emit a deployment fingerprint,
- rollback should be possible using recorded identity and provenance.

## Validation Rules

A validator for this model should check at least:

- identity uniqueness,
- required field presence,
- hash or content-link integrity where available,
- repo/branch validity,
- source-to-artifact linkage,
- candidate-to-cycle linkage,
- deployment-to-artifact linkage,
- evidence completeness.

## Practical Rule

If an object matters for deploy, promote, reconcile, rollback, or audit, it should
have a stable identity and a provable path back to the source and cycle that gave
birth to it.
