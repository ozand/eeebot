# Nanobot Completion Contract

Last updated: 2026-04-16 UTC

## Purpose

This document defines the canonical contract for considering the Nanobot project complete.

It is the authoritative statement of:
- what counts as finished
- which truths are canonical for which purpose
- which proofs must exist
- which items are intentionally out of scope

## Canonical Truth Domains

### 1. Canonical product/source truth

Canonical product/source truth remains in Git-managed repositories under the intended ownership model.

For the current project, that means:
- canonical source remains repo-managed and reviewable
- live host mutation is not canonical product/source truth by itself

### 2. Live eeepc host truth

For live self-evolving runtime behavior on `eeepc`, the current canonical authority root is:
- `/var/lib/eeepc-agent/self-evolving-agent/state`

This is the authoritative live source for:
- cycle status
- active goal on the live host
- approval status on the live host
- report source on the live host
- outbox/index source on the live host
- live artifact proof when present

### 3. Repo-side bounded runtime truth

For repo-side bounded runtime cycles, the canonical workspace-side truth is:
- `<workspace>/state/reports/`
- `<workspace>/state/goals/`
- `<workspace>/state/outbox/latest.json`
- `<workspace>/state/outbox/report.index.json`
- `<workspace>/state/promotions/`

This domain is used for:
- local bounded cycle proofs
- repo-side comparable index proofs
- promotion lifecycle visibility

## Required Read-Path Contract

Nanobot is complete only if `nanobot status` can truthfully report from:
- `workspace_state`
- `host_control_plane`

And can show, when available:
- runtime state source
- runtime state root
- status
- active goal
- approval state
- report source
- artifact paths
- promotion summary
- promotion candidate path
- promotion decision record presence
- promotion accepted record presence

## Required Write-Path Contract

Repo-side bounded runtime must emit:
- detailed cycle report
- outbox latest summary
- host-comparable `outbox/report.index.json`
- promotion latest record when applicable
- decision trail record when applicable
- accepted trail record when applicable

The purpose is comparable proof, not full schema identity with the live host control-plane.

## Required Promotion Contract

Promotion status surfaces must obey this deterministic precedence:
1. `state/promotions/latest.json`
2. fallback from `state/outbox/report.index.json -> promotion.*`

Durable trail visibility must be derived from presence checks for:
- `state/promotions/decisions/<candidate>.json`
- `state/promotions/accepted/<candidate>.json`

## Required Proof Artifacts

The project is not complete unless the repo contains all of the following proof layers:

### Live host proof
- live eeepc authority proof note
- proof of `nanobot status` against the live authority root
- proof of at least one real live PASS cycle with artifact evidence

### Repo-side proof
- repo-side bounded cycle proof note
- repo-side promotion trail proof note
- write-path convergence proof note

### Operational proof
- deploy/verify/rollback runbook
- apply gate runbook

## Completion Decision Rule

Nanobot is complete when all of these are true:
- live authority boundary is implemented and documented
- repo-side bounded runtime emits comparable proof surfaces
- promotion lifecycle is visible and deterministic
- deploy/verification workflow is reproducible
- final proof notes exist
- focused regression tests remain green

## Explicit Non-Goals For Completion

The project does not require the following to be considered complete:
- replacing the live eeepc control-plane executor
- full schema identity between repo-side and live-side artifacts
- automatic promotion into canonical repos
- broad architecture rewrite
- elimination of every historical note or legacy surface

## Final Output Requirement

A final completion summary must exist and must state:
- what is complete
- what is proven
- what remains intentionally out of scope

## References

- `docs/NANOBOT_DONE_CRITERIA.md`
- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_LIVE_VERIFICATION_2026-04-16.md`
- `docs/EEEPC_REPO_SIDE_BOUNDED_CYCLE_PROOF.md`
- `docs/EEEPC_REPO_SIDE_PROMOTION_TRAIL_PROOF.md`
- `docs/EEEPC_WRITE_PATH_CONVERGENCE_PROOF.md`
- `docs/EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md`
