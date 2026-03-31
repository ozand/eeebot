# Deploy Decision Matrix

Last updated: 2026-03-28 UTC

## Purpose

This document defines how maintainers and policy logic should choose among:

- normal artifact rollout,
- candidate rollout,
- host reconciliation,
- promotion replay,
- emergency SSH patching,
- rollback,
- full rebuild from baseline.

The guiding rule is:

choose the smallest intervention that preserves canonical ownership, evidence,
replayability, rollback, and portability.

## Decision Inputs

Important inputs include:

- deployment fingerprint,
- release artifact identity and channel,
- drift classification,
- health-check status,
- weak-host resource impact,
- rollback availability,
- baseline compatibility,
- incident severity,
- replayability of host-born changes,
- evidence completeness.

## Decision Hierarchy

The preferred order is:

1. safety and recovery first,
2. rollback before repair when rollback is clearly safer,
3. repair before rebuild when invariants still hold,
4. promote only when evidence-backed,
5. use SSH patching only in incident mode,
6. rebuild from baseline when trust is lost.

## State Categories

### State trust

- `trusted`
- `explainable_drift`
- `stale_provisional`
- `unsafe_or_untrusted`

### Change origin

- `canonical_source`
- `host_born`
- `evidence_only`
- `emergency_recovery`

### Recovery posture

- `normal_operation`
- `degraded_but_recoverable`
- `incident_mode`
- `unrecoverable_without_rebuild`

## Action Matrix

### Normal artifact rollout

Use when:

- the change is canonicalized,
- the artifact is valid,
- the target host profile is compatible,
- rollback is available,
- weak-host budgets remain acceptable.

Avoid when:

- the artifact is missing required provenance,
- the host state is unsafe and needs deeper triage first.

### Candidate rollout

Use when:

- a change is not yet final but is reviewable and artifactizable,
- evidence exists,
- rollout is needed for bounded validation on the target host.

Avoid when:

- the candidate depends on hidden local assumptions,
- provenance or rollback is unclear.

### Host reconciliation

Use when:

- drift is bounded and explainable,
- invariants still hold,
- repair can happen without pretending the drift is canonical.

Avoid when:

- repair would preserve harmful ambiguity,
- trust in the local state is already broken.

### Promotion replay

Use when:

- a host-born change is useful,
- evidence is complete,
- the change can be replayed against canonical source outside the live host.

Avoid when:

- the change cannot be reproduced,
- the change is only noisy local drift.

### Emergency SSH patch

Use only when:

- incident mode is active,
- artifact delivery is unavailable or too slow,
- restoring minimal trusted runtime is the priority.

Constraints:

- keep scope bounded,
- record the patch,
- generate follow-up canonical work,
- return to artifact-managed state quickly.

### Rollback

Use when:

- post-deploy validation fails,
- capability regression appears,
- resource impact is unacceptable,
- artifact metadata or compatibility is invalid,
- the operator requests reversal.

Preferred rollback order:

1. previous known-good artifact,
2. baseline-compatible configuration,
3. justified evidence-backed state replay,
4. rebuild if rollback is not enough.

### Full rebuild from baseline

Use when:

- local state is no longer trustworthy,
- drift spans multiple critical surfaces,
- bootstrap or recovery assumptions are broken,
- repeated repair would only preserve confusion.

## Decision Record

Every major decision should record at least:

- timestamp,
- actor,
- chosen action,
- rejected alternatives,
- triggering evidence,
- drift classification,
- relevant artifact or candidate IDs,
- rollback path,
- incident flag,
- required follow-up.

## Guardrails

- artifact-first deployment is the default path,
- host-local mutation is never the durable source of truth,
- SSH patching is emergency-only,
- unexplained drift is suspicious until classified,
- rollback should remain viable after every deployment,
- rebuild is preferred when repair would preserve ambiguity.

## Practical Rule

If the change is canonical and deployable, roll out an artifact.
If it is bounded and explainable, reconcile or replay it.
If it is incident-critical, use a bounded SSH patch with mandatory follow-up.
If trust is lost, rollback or rebuild from baseline.
