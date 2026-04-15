# eeepc Write-Path Convergence Proof

Last updated: 2026-04-16 UTC

## Goal

Record the bounded proof that the repo-side runtime now emits a comparable write-side summary/index contract alongside the already validated live eeepc host authority proof.

This is not a claim of full write-path unification.
It is a claim that the repo-side bounded runtime now produces a compact summary/index surface that can be compared to the live host control-plane proof fields without ad-hoc interpretation.

## Repo-Side Comparable Surface

Repo-side bounded runtime now writes:
- `state/outbox/report.index.json`

The new index includes:
- `status`
- `source`
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

## Live eeepc Comparable Surface

Live eeepc host authority proof already uses:
- `/var/lib/eeepc-agent/self-evolving-agent/state/outbox/report.index.json`
- `/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-*.json`
- `/var/lib/eeepc-agent/self-evolving-agent/state/goals/registry.json`

Live authority root:
- `/var/lib/eeepc-agent/self-evolving-agent/state`

## Comparable Fields Proven

The following proof fields can now be compared across:
- repo-side bounded runtime summary/index, and
- live eeepc host authority summary/index

### 1. Status

Repo-side:
- `report.index.json.status`

Live eeepc:
- `outbox/report.index.json.status`
- also reflected by `nanobot status` through the host authority root

### 2. Report source

Repo-side:
- `report.index.json.source`

Live eeepc:
- `outbox/report.index.json.source`
- surfaced by `nanobot status` as `Report source`

### 3. Goal id

Repo-side:
- `report.index.json.goal.goal_id`

Live eeepc:
- `outbox/report.index.json.goal.goal_id`
- surfaced by `nanobot status` as `Active goal`

### 4. Follow-through / artifact paths

Repo-side:
- `report.index.json.goal.follow_through.artifact_paths`

Live eeepc:
- `outbox/report.index.json.goal.follow_through.artifact_paths`
- surfaced by `nanobot status` as `Artifacts` when present

### 5. Approval summary

Repo-side:
- `report.index.json.capability_gate.approval`

Live eeepc:
- `outbox/report.index.json.capability_gate.approval`
- surfaced by `nanobot status` as `Approval gate` and `Gate state`

### 6. Promotion pointer summary

Repo-side:
- `report.index.json.promotion.*`

Live eeepc:
- not yet fully converged to the same repo-side promotion pointer model
- still treated as follow-up convergence work

## What Is Proven Now

The repo-side runtime and the live eeepc host authority now share a bounded, comparable summary/index layer for:
- status
- report source
- goal id
- follow-through artifact paths
- approval summary

In addition, the repo-side runtime now exposes promotion pointer fields from that same summary/index surface, and the repo-side status reader now surfaces:
- `Promotion summary`
- `Promotion candidate path`

Authoritative precedence for those repo-side promotion fields is now explicit:
1. `state/promotions/latest.json`
2. fallback from `state/outbox/report.index.json -> promotion.*`

The repo-side status reader now also exposes durable trail visibility for the active candidate:
- `Promotion decision record`
- `Promotion accepted record`

Those are derived from presence checks for:
- `state/promotions/decisions/<candidate>.json`
- `state/promotions/accepted/<candidate>.json`

This means the promotion pointer is no longer only buried in JSON; it is also visible in the operator-facing runtime status output, and stale outbox promotion blocks no longer override the authoritative promotion latest record.

## What Is Not Yet Proven

Still not claimed:
- full schema identity between repo-side and live host control-plane outputs
- shared promotion lifecycle files across both runtimes
- identical goal registry schemas
- identical detailed report payloads
- automatic canonical promotion flow from live host to repo

## Bounded Conclusion

Write-path convergence has advanced from:
- repo-only workspace-style artifacts

to:
- repo-side bounded runtime with a host-comparable `report.index.json` summary contract

That is sufficient to support the next bounded implementation step without ambiguity.

## References

- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_LIVE_VERIFICATION_2026-04-16.md`
- `docs/EEEPC_WRITE_PATH_PROMOTION_CONVERGENCE_NOTE.md`
- `docs/EEEPC_RUNTIME_STATE_AUTHORITY_USAGE.md`
- `docs/userstory/EEEPC_WRITE_PATH_PROMOTION_CONVERGENCE_SLICE.md`
- `docs/plans/2026-04-16-eeepc-write-path-promotion-convergence.md`
- `nanobot/runtime/coordinator.py`
- `nanobot/runtime/state.py`
- `nanobot/runtime/promotion.py`
