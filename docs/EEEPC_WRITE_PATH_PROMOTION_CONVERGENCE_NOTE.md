# eeepc Write-Path and Promotion Convergence Note

Last updated: 2026-04-16 UTC

## Purpose

This note records the bounded write-side convergence contract added after live read-path validation.

The goal is not full runtime unification.
The goal is to make repo-side bounded runtime writes produce a summary/index surface that is comparable to the live eeepc host control-plane proof shape.

## What Changed

The repo-side bounded runtime now writes:
- `state/outbox/latest.json`
- `state/outbox/report.index.json`

The new `report.index.json` is the write-side compatibility bridge.

## Why This Exists

Live `eeepc` host truth already exposes operator-proof fields through:
- `reports/evolution-*.json`
- `goals/registry.json`
- `outbox/report.index.json`

Before this slice, the repo-side runtime wrote only workspace-style artifacts and promotion files, but did not emit a host-comparable outbox index.

That made read-path convergence stronger than write-path convergence.

## Repo-Side Comparable Index Contract

The repo-side runtime now emits a compact summary at:
- `state/outbox/report.index.json`

The bounded comparable fields are:
- `ok`
- `source`
- `status`
- `goal.goal_id`
- `goal.follow_through.status`
- `goal.follow_through.blocked_next_step`
- `goal.follow_through.artifact_paths`
- `goal.follow_through.action_summary`
- `capability_gate.approval`
- `promotion.promotion_candidate_id`
- `promotion.candidate_path`
- `promotion.review_status`
- `promotion.decision`

The repo-side status/reader surfaces now also normalize and display:
- `Promotion summary: <candidate> | <review_status> | <decision>`
- `Promotion candidate path: ...`
- `Promotion decision record: present|missing`
- `Promotion accepted record: present|missing`

Those values are derived from the authoritative repo-side promotion surface precedence:
1. `state/promotions/latest.json`
2. fallback from `state/outbox/report.index.json -> promotion.*`

Durable trail visibility is derived separately from filesystem presence checks for the active candidate:
- `state/promotions/decisions/<candidate>.json`
- `state/promotions/accepted/<candidate>.json`

Deterministic merge rule:
- if `state/promotions/latest.json` exists, promotion status/decision/candidate fields must come from it
- `state/outbox/report.index.json -> promotion.*` is fallback only when `promotions/latest.json` is absent
- stale outbox promotion pointers must not override an existing `promotions/latest.json`

## What This Makes Comparable

The repo-side runtime can now emit a summary/index contract that can be compared more directly to the live eeepc host control-plane outbox/index surface.

Comparable proof fields now include:
- cycle status
- report source path
- goal id
- follow-through summary
- artifact path list
- approval summary
- promotion pointer summary

## What Is Intentionally Still Different

This slice does not claim that the repo-side index is structurally identical to the live eeepc control-plane JSON.

Differences remain in:
- full report schema
- goal registry schema
- promotion decision trail layout
- live control-plane execution metadata
- host-specific health/reflection rollups

That is acceptable for this bounded slice.
The success condition is a comparable summary contract, not full schema identity.

## Operator Rule

For live eeepc proof:
- use `nanobot status --runtime-state-source host_control_plane --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state`

For repo-side bounded cycle proof:
- inspect the generated workspace report
- inspect `state/outbox/report.index.json`
- inspect promotion files under `state/promotions/` when present

## Practical Meaning

The repo now has:
1. live read-path authority validation
2. repo-side write-path comparable summary output
3. promotion pointers discoverable from the repo-side outbox index

This reduces the gap between:
- what the live host proves, and
- what the repo-side bounded runtime emits

without replacing the live eeepc control-plane.

## References

- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_LIVE_VERIFICATION_2026-04-16.md`
- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_USAGE.md`
- `docs/userstory/EEEPC_WRITE_PATH_PROMOTION_CONVERGENCE_SLICE.md`
- `docs/plans/2026-04-16-eeepc-write-path-promotion-convergence.md`
- `nanobot/runtime/coordinator.py`
- `nanobot/runtime/promotion.py`
