# eeepc Repo-Side Promotion Trail Proof

Last updated: 2026-04-16 UTC

## Goal

Record one coherent repo-side proof of the promotion lifecycle as it is now surfaced through:
- runtime status
- comparable outbox index
- promotion latest record
- decision trail files
- accepted trail files

This is a repo-side bounded proof.
It is not a claim that the live eeepc host control-plane uses the same promotion storage layout.

## Repo-Side Promotion Surfaces

The repo-side bounded runtime and promotion workflow now expose the following durable surfaces:

### Cycle/report surfaces
- `state/reports/evolution-*.json`
- `state/outbox/latest.json`
- `state/outbox/report.index.json`

### Promotion surfaces
- `state/promotions/latest.json`
- `state/promotions/<promotion_candidate_id>.json`
- `state/promotions/decisions/<promotion_candidate_id>.json`
- `state/promotions/accepted/<promotion_candidate_id>.json`
- `state/promotions/patches/<promotion_candidate_id>.json`

## Status Surface Fields Now Available

`nanobot status` can now surface the following promotion-related fields for repo-side bounded runtime state:
- `Promotion candidate`
- `Promotion review`
- `Promotion decision`
- `Promotion summary`
- `Promotion candidate path`
- `Promotion decision record`
- `Promotion accepted record`

## Authoritative Merge Rule

For repo-side promotion summary fields, the deterministic precedence is:
1. `state/promotions/latest.json`
2. fallback from `state/outbox/report.index.json -> promotion.*`

For durable trail visibility, the reader checks filesystem presence directly for:
- `state/promotions/decisions/<candidate>.json`
- `state/promotions/accepted/<candidate>.json`

This means:
- stale promotion blocks in `report.index.json` must not override `promotions/latest.json`
- decision/accepted visibility is based on durable files, not inferred summary text

## Bounded Proof Example

A coherent repo-side promotion trail now looks like this:

1. A bounded cycle completes and writes:
- `state/reports/evolution-*.json`
- `state/outbox/latest.json`
- `state/outbox/report.index.json`

2. If the cycle produces a promotion candidate, it also writes:
- `state/promotions/<candidate>.json`
- `state/promotions/latest.json`

3. When review occurs, it writes:
- `state/promotions/decisions/<candidate>.json`

4. When accepted, it also writes:
- `state/promotions/accepted/<candidate>.json`
- `state/promotions/patches/<candidate>.json`

5. `nanobot status` can then expose, from the repo-side runtime state:
- which promotion candidate is current
- review/decision state
- a compact summary line
- the candidate path
- whether a decision record exists
- whether an accepted record exists

## What Is Proven By This Slice

The repo-side promotion lifecycle is now operator-visible across both:
- summary/index surfaces
- durable trail files

Specifically, the project now has a proofable operator-facing chain for:
- candidate creation
- candidate summary visibility
- authoritative latest promotion record visibility
- decision trail presence
- accepted trail presence

## What Is Not Yet Claimed

This proof does not claim:
- that the live eeepc host control-plane stores promotion trail in the same file layout
- that canonical source promotion is automated end-to-end
- that repo-side and live-side promotion schemas are identical
- that promotion execution into canonical repos is complete

## Why This Matters

Before this slice, promotion state existed but was not fully surfaced as a coherent operator-facing trail.

After this slice:
- promotion summary is visible in status
- promotion precedence is deterministic
- decision/accepted trail presence is visible in status
- docs explicitly describe how to interpret these surfaces

That is sufficient for a bounded repo-side promotion proof.

## References

- `docs/EEEPC_WRITE_PATH_PROMOTION_CONVERGENCE_NOTE.md`
- `docs/EEEPC_WRITE_PATH_CONVERGENCE_PROOF.md`
- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_USAGE.md`
- `docs/PROMOTION_GATE_SPEC.md`
- `nanobot/runtime/promotion.py`
- `nanobot/runtime/state.py`
- `nanobot/runtime/coordinator.py`
