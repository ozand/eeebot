# eeepc Runtime State Authority Usage

Last updated: 2026-04-15 UTC

## Purpose

This note explains how to query runtime truth through the explicit authority boundary added to `nanobot status`.

The goal is to avoid silently mixing:
- repo-side workspace-state artifacts, and
- live host control-plane state.

For repo-side promotion summaries specifically, `nanobot status` now uses a deterministic precedence rule:
1. `state/promotions/latest.json`
2. fallback from `state/outbox/report.index.json -> promotion.*`

For durable promotion trail visibility, `nanobot status` also reports whether these records exist for the active candidate:
- `state/promotions/decisions/<candidate>.json`
- `state/promotions/accepted/<candidate>.json`

## New Status Command Flags

`nanobot status` now supports:
- `--runtime-state-source`
- `--runtime-state-root`

Supported source values in the current bounded slice:
- `workspace_state`
- `host_control_plane`

## Default Behavior

If no authority flags are passed:
- `nanobot status` reads from the default workspace-state path
- source is reported as `workspace_state`
- root is reported as `<workspace>/state`

Example:

```bash
nanobot status
```

Expected authority lines include:

```text
Runtime state source: workspace_state
Runtime state root: /path/to/workspace/state
```

## Querying Live eeepc Host Truth

To query the current live `eeepc` self-evolving authority surface, point `status` at the host control-plane state root:

```bash
nanobot status \
  --runtime-state-source host_control_plane \
  --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state
```

Expected authority lines include:

```text
Runtime state source: host_control_plane
Runtime state root: /var/lib/eeepc-agent/self-evolving-agent/state
```

## What This Proves

When the host-control-plane root contains a fresh cycle report, the bounded reader can now surface from the same authority tree:
- runtime status
- active goal
- approval state
- report path
- artifact paths when present
- promotion summary when a comparable promotion surface is available
- promotion candidate path when a comparable promotion surface is available

This is the minimum needed to make operator summaries truthful about which state tree they come from.

## Current Scope

This bounded slice currently normalizes enough host-control-plane report shape to surface:
- `goal.goal_id`
- `process_reflection.status`
- `capability_gate.approval`
- `follow_through.artifact_paths`

It does not yet attempt full runtime unification.

## Important Boundary

This feature does not mean the repo workspace-state runtime has become the live `eeepc` authority.
It means the CLI can now identify and read the intended authority surface explicitly.

As of the current verified host state:
- live `eeepc` self-evolving authority = `/var/lib/eeepc-agent/self-evolving-agent/state`
- repo-side workspace-state runtime remains a separate implementation slice

## Operator Rule

Use:
- `workspace_state` for repo-local or workspace-slice checks
- `host_control_plane` for live `eeepc` self-evolving truth

Do not claim a live proof unless the status output and the underlying report come from the same chosen authority root.

## Related References

- `docs/EEEPC_SELF_EVOLVING_HOST_PROOF_2026-04-15.md`
- `docs/EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md`
- `docs/SOURCE_OF_TRUTH_AND_PROMOTION_POLICY.md`
- `docs/WORKSPACE_RUNTIME_LANE.md`
- `docs/userstory/EEEPC_LIVE_AUTHORITY_CONVERGENCE_SLICE.md`
- `docs/plans/2026-04-15-eeepc-live-authority-convergence.md`
