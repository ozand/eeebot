# eeepc Repo-Side Bounded Cycle Proof

Last updated: 2026-04-16 UTC

## Goal

Record one coherent repo-side proof for a bounded runtime cycle using the current workspace-side runtime artifacts.

This proof is meant to complement the live eeepc authority proof, not replace it.

## Repo-Side Cycle Surfaces

A bounded repo-side cycle now emits:
- `state/reports/evolution-*.json`
- `state/goals/active.json`
- `state/outbox/latest.json`
- `state/outbox/report.index.json`
- `state/promotions/latest.json` when a promotion candidate exists
- decision/accepted trail files when review has happened

## Minimal Comparable Proof Fields

A repo-side bounded cycle can now be interpreted through the following fields:
- status
- report source
- goal id
- approval summary
- next step hint
- follow-through status
- artifact path list
- promotion summary
- promotion candidate path
- decision-trail presence

## How To Read A Repo-Side Cycle

### 1. Report

Primary detailed artifact:
- `state/reports/evolution-*.json`

Use this for:
- cycle identifiers
- timestamps
- result status
- evidence reference
- execution response or execution error

### 2. Goal state

Goal surface:
- `state/goals/active.json`

Use this for:
- active goal id in the workspace-side bounded runtime

### 3. Latest outbox summary

Summary surface:
- `state/outbox/latest.json`

Use this for:
- approval gate summary
- next hint
- latest report summary pointer

### 4. Comparable index

Comparable bridge surface:
- `state/outbox/report.index.json`

Use this for:
- status
- source report path
- goal id
- follow-through summary
- artifact paths
- approval summary
- promotion pointer summary

This is the closest repo-side equivalent to the live eeepc outbox/index proof surface.

### 5. Promotion surfaces

When present, use:
- `state/promotions/latest.json`
- `state/promotions/<candidate>.json`
- `state/promotions/decisions/<candidate>.json`
- `state/promotions/accepted/<candidate>.json`

These are used by the status reader with deterministic precedence for promotion summary fields.

## Status Surface Backed By Those Files

`nanobot status` can now present a coherent repo-side bounded cycle summary including:
- `Runtime state source: workspace_state`
- `Runtime state root: <workspace>/state`
- `Active goal`
- `Cycle`
- `Cycle started`
- `Cycle ended`
- `Evidence`
- `Approval gate`
- `Gate state`
- `Next`
- `Report source`
- `Goal source`
- `Outbox source`
- `Promotion summary`
- `Promotion candidate path`
- `Promotion decision record`
- `Promotion accepted record`

## What This Proves

This repo-side bounded runtime now has a proofable and operator-readable cycle surface that is no longer just a collection of isolated files.

It now has:
- a detailed cycle report
- a compact latest summary
- a host-comparable report index
- promotion lifecycle visibility
- deterministic precedence for promotion summary fields

## What It Does Not Prove

This proof does not claim:
- that repo-side bounded runtime is the live eeepc authority
- that repo-side goal schema equals host control-plane goal schema
- that repo-side reports are identical to host control-plane reports
- that canonical source promotion is automated

## Why This Completes The Repo-Side Proof Layer

With this note plus the promotion trail proof note, the project now has both:
- repo-side bounded cycle proof
- repo-side promotion trail proof

Together they form the repo-side counterpart to the live eeepc host authority proof already validated earlier.

## References

- `docs/EEEPC_REPO_SIDE_PROMOTION_TRAIL_PROOF.md`
- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_LIVE_VERIFICATION_2026-04-16.md`
- `docs/EEEPC_WRITE_PATH_PROMOTION_CONVERGENCE_NOTE.md`
- `docs/EEEPC_WRITE_PATH_CONVERGENCE_PROOF.md`
- `nanobot/runtime/coordinator.py`
- `nanobot/runtime/state.py`
- `nanobot/runtime/promotion.py`
