# Promotion Gate Spec

Last updated: 2026-03-28 UTC

## Purpose

This document defines the gate that decides whether a host-born change may return
to canonical source.

Promotion is the only approved path for turning provisional host-born change into
durable canonical product or control-plane source.

## Core Rule

Host-born change is real, but provisional.

It becomes canonical only after:

- evidence exists,
- replayability is established,
- the canonical target is valid,
- review or policy approves the promotion.

## Required Promotion Inputs

A promotion candidate should include at least:

- candidate diff or patch bundle,
- changed file list,
- rationale,
- evidence references,
- validation summary,
- resource impact summary when relevant,
- rollback or rejection plan,
- target repo and target branch.

## Required Promotion Metadata

Suggested fields:

- `promotion_candidate_id`
- `candidate_created_utc`
- `origin_cycle_id`
- `origin_host`
- `source_paths`
- `target_repo`
- `target_branch`
- `base_commit`
- `candidate_patch_hash`
- `evidence_refs`
- `validation_summary`
- `resource_impact_summary`
- `risk_level`
- `rollback_plan`
- `review_status`
- `decision`
- `decision_reason`

## Gate Stages

### Gate 0 - Eligibility

Confirm:

- the change came from an allowed mutation surface,
- the candidate is meaningful enough to consider,
- the target canonical repo is correct.

### Gate 1 - Evidence Completeness

Confirm:

- evidence exists,
- metadata is complete,
- rationale and changed paths are traceable.

### Gate 2 - Safety And Recoverability

Confirm:

- rollback or rejection is clear,
- ownership boundaries are preserved,
- no policy-prohibited content is present.

### Gate 3 - Functional Validity

Confirm:

- the change solves or improves the intended problem,
- relevant tests, smoke checks, or runtime validations exist,
- behavior is stable enough to consider canonicalization.

### Gate 4 - Weak-Host Fit

Confirm:

- the change remains affordable on weak hardware,
- it does not create unacceptable memory, CPU, disk, or operational cost,
- it fits the intended host profile.

### Gate 5 - Review Decision

Possible decisions:

- `accept`
- `reject`
- `defer`
- `needs_more_evidence`

## Automatic Rejection Conditions

Promotion should fail if:

- evidence is missing,
- the target canonical repo or branch is invalid,
- the change violates source/evidence ownership rules,
- rollback is unclear,
- the change is not explainable,
- the weak-host cost is too high,
- the candidate is only noisy local drift rather than durable value.

## Promotion Outputs

When accepted, promotion should produce:

- an accepted promotion record,
- a reviewable branch or patch candidate,
- linked evidence references,
- a durable decision trail.

When rejected, promotion should produce:

- a rejection reason,
- missing-evidence or missing-safety notes,
- whether the candidate remains host-local only.

## Target Rules

- canonical promotion targets must remain under the canonical ownership model,
- evidence repos are not valid canonical source targets,
- host-born changes should not auto-land in `main`,
- reviewable promotion branches should be preferred.

## Practical Rule

If a host-born change cannot be replayed and defended outside the live host, it is
not ready to become canonical source.
