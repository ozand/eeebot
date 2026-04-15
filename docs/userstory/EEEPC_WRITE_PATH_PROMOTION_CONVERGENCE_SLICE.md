# Userstory: eeepc write-path and promotion convergence slice

## User Story

As the operator of the `eeepc` self-evolving runtime,
I want the repo-side bounded runtime to emit evidence and promotion artifacts that align with the live host control-plane state tree,
so that read-path truth and write-path truth no longer diverge across two different artifact models.

## Scope

In scope:
- define the smallest write-path convergence slice after live read-path validation
- align repo-side emitted artifact names and payload expectations with the live eeepc control-plane where practical
- identify one bounded export/promotion bridge instead of attempting full runtime replacement
- define one verification target for a single post-change cycle

Out of scope:
- replacing the host control-plane executor
- broad self-evolving runtime redesign
- automatic promotion into canonical repos
- changing unrelated gateway/chat behavior
- building the full GitHub sync bridge in one step

## Current Situation

As of 2026-04-16:
- live read-path truth is now operationally validated through `nanobot status`
- live `eeepc` authority is `/var/lib/eeepc-agent/self-evolving-agent/state`
- repo-side bounded runtime still writes workspace-style artifacts under `workspace/state/...`
- repo-side promotion review workflow writes `state/promotions/...`
- live eeepc host truth currently exposes key cycle summary/proof through:
  - `reports/evolution-*.json`
  - `goals/registry.json`
  - `outbox/report.index.json`
  - approval files and host backups

This means read-path convergence is proven, but write-path convergence is not.

## Problem To Solve

The repo now knows how to read the live host authority tree, but the repo’s own bounded write surfaces still describe a different canonical artifact model.

Without a bounded convergence slice:
- local/runtime tests talk in one artifact shape
- live eeepc writes another shape
- promotion/evidence documentation remains split between the two

## Proposed Bounded Direction

Do not replace the host write path.
Instead, add one bounded bridge layer that lets repo-side runtime outputs be exported or mirrored in a host-control-plane-compatible summary shape.

The direction for this slice is:
- keep live eeepc control-plane writes authoritative
- add a repo-side export/index contract that mirrors the minimum live proof surfaces
- keep promotion review artifacts durable, but make the summary/index surface compatible with live operator truth

## Minimal Implementation Slice

### Slice 1: host-compatible cycle summary export

After a repo-side bounded cycle completes, emit a compact summary artifact compatible with the live control-plane proof fields.

At minimum this summary should include:
- status
- source report path
- goal id
- follow-through status
- artifact paths
- approval summary when available
- process reflection summary when available

### Slice 2: stable outbox index contract

Add or align one stable outbox index file so the same status reader pattern can work across both:
- workspace-state slice
- host-control-plane slice

The purpose is not identical trees.
The purpose is one minimal comparable index contract.

### Slice 3: promotion pointer compatibility

When the repo-side runtime creates promotion candidates, ensure the cycle summary/index can point to them in a way that operator-facing status and future sync/export code can follow without custom branching.

## Acceptance Criteria

- the repo contains one explicit write-side convergence contract for cycle summary/index output
- workspace-state writes include a comparable outbox/index summary surface
- promotion candidates can be discovered from that summary surface when present
- docs explain how write-path truth relates to the live eeepc control-plane truth
- one focused verification can compare a repo-side bounded cycle summary against the live host proof schema without hand interpretation

## Definition of Ready

- live read-path convergence has been validated
- the live eeepc proof fields are documented
- the repo-side current write surfaces are identified
- the bounded slice is small enough to implement without touching the live executor

## Definition of Done

- a bounded export/index contract is implemented or explicitly planned at file level
- affected write surfaces and tests are identified precisely
- docs clearly describe the new convergence rule
- remaining larger sync/promotion automation work is explicitly left for follow-up

## References

- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_LIVE_VERIFICATION_2026-04-16.md`
- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_USAGE.md`
- `docs/EEEPC_SELF_EVOLVING_HOST_PROOF_2026-04-15.md`
- `docs/PROMOTION_GATE_SPEC.md`
- `docs/HOST_GITHUB_SYNC_ARCHITECTURE.md`
- `docs/userstory/EEEPC_LIVE_AUTHORITY_CONVERGENCE_SLICE.md`
- `nanobot/runtime/coordinator.py`
- `nanobot/runtime/promotion.py`
- `nanobot/runtime/state.py`
