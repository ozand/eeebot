# Release Artifact And Rollback Contract

Last updated: 2026-03-28 UTC

## Purpose

This document defines the contract for building, deploying, validating, and
rolling back release artifacts for `eeepc` on weak hosts.

The normal deployment unit is a release artifact, not an ad hoc host patch.

## Core Rule

The live host should normally consume versioned release artifacts built from
canonical source.

Host-local mutation may still exist, but it is not the deployment source of truth.

## Definitions

### Release artifact

A versioned deployable bundle built from canonical source and approved inputs.

### Rollback artifact

The last known-good release bundle, or an explicitly designated fallback bundle.

### Deployment fingerprint

The identity tuple that describes what is actually running on the host.

Suggested fields:

- `engine_sha`
- `control_plane_sha`
- `policy_hash`
- `artifact_id`
- `artifact_version`
- `release_channel`

## Artifact Boundary Rules

Artifacts must be built from canonical source only.

Artifacts should not include:

- secrets,
- credential stores,
- raw inbox data,
- full volatile state trees,
- unrelated host-local experiments,
- unapproved mutable host overlays.

## Required Artifact Metadata

Each artifact should be traceable with at least:

- `artifact_id`
- `artifact_version`
- `release_channel`
- `build_timestamp_utc`
- `source_repo`
- `source_branch`
- `source_commit`
- `target_host_profile`
- `compatibility_constraints`
- `included_paths`
- `excluded_paths`
- `deploy_strategy`
- `rollback_strategy`
- `previous_known_good_artifact_id`
- `evidence_refs` if relevant
- `promotion_refs` if relevant

Weak-host rollout note:

- placeholder provenance such as `unknown`, `local-build`, or overly generic
  recipe identifiers should be treated as weaker-than-desired rollout evidence,
- acceptable for early bounded candidate work,
- but should be tightened before stronger or wider rollout automation is trusted.

## Build Rules

- artifacts should be reproducible from canonical source and declared inputs,
- build output must point to a specific canonical commit,
- required metadata must exist before publish,
- unapproved host-local files should never leak into the artifact.

For bounded host rollout work, the candidate tree should also pass a minimum
runtime completeness check so obviously incomplete source overlays do not reach
the host gateway path.

## Deployment Rules

- deploy artifacts to the host as the normal path,
- keep release payload separate from runtime state and evidence,
- preserve idempotent deployment where practical,
- emit deployment evidence for each install or upgrade.

## Post-Deploy Validation

After deployment, the system should validate at least:

- process start and stability,
- operator control path,
- truthful capability reporting,
- evidence write path,
- compatibility with weak-host resource expectations.

Failed validation should block promotion of the release state and may trigger
rollback.

## Rollback Contract

Rollback should:

- restore the last known-good artifact or baseline,
- avoid dependence on damaged mutable state,
- preserve evidence explaining why rollback occurred,
- return the host to an explainable and supportable state.

## Rollback Triggers

Rollback should be considered when there is:

- startup failure,
- post-deploy health-check failure,
- capability regression,
- unacceptable weak-host resource impact,
- invalid artifact metadata,
- host-profile incompatibility,
- operator-requested rollback.

## Recovery Hierarchy

Preferred order:

1. rollback to previous known-good artifact,
2. restore baseline-compatible configuration,
3. replay only justified evidence-backed local state,
4. rebuild from canonical source if rollback is insufficient.

## Practical Rule

If a host needs the new code but cannot safely take the new artifact, the right
response is usually to improve the artifact and deployment contract, not to drift
into routine live patching.
