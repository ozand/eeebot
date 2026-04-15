# Userstory: eeepc live authority convergence slice

## User Story

As the operator of the `eeepc` self-evolving runtime,
I want the repo and the live host to agree on one clearly defined self-evolving authority surface,
so that deploy proof, cycle proof, approval proof, and promotion/evidence proof all come from the same canonical state tree.

## Scope

In scope:
- define the smallest bounded convergence slice between the repo-side workspace runtime and the live host control-plane state tree
- make the live authority boundary explicit
- choose one concrete convergence direction for implementation
- define verification criteria for one post-convergence host cycle

Out of scope:
- broad runtime redesign
- replacing the host control-plane in one step
- automatic approval renewal
- changing unrelated gateway/runtime features
- solving every historical goal-registry or backlog-quality issue

## Current Situation

As verified on 2026-04-15:
- the repo contains a bounded workspace-state runtime slice
- the live `eeepc` host self-evolving execution authority currently writes to `/var/lib/eeepc-agent/self-evolving-agent/state`
- the deployed gateway workspace path `/home/opencode/.nanobot-eeepc/workspace/state/...` is not currently the verified live self-evolving authority
- a real host cycle already moved from `BLOCK` to `PASS` once the approval gate was restored

This means the current problem is no longer basic operability.
The current problem is state-surface convergence.

## Proposed Bounded Convergence Direction

Choose this direction first:
- keep the live host control-plane state tree as the current execution authority
- adapt the repo-side runtime/docs/reporting surfaces so they can truthfully target or mirror that authority
- do not claim the workspace-state slice is canonical on `eeepc` until one live cycle proves it

Why this is the smallest slice:
- it matches the already verified host reality
- it avoids a risky one-step swap of the live execution plane
- it allows truthful operator summaries immediately
- it creates a narrow proof target for future convergence

## Minimal Implementation Slice

### Slice 1: explicit authority selection/config surface

Add one explicit configuration or adapter boundary that answers:
- where does self-evolving state live for this runtime?
- is the runtime writing workspace-state artifacts, host control-plane artifacts, or a mirrored export?

The key result is not a redesign.
The key result is that the code and docs can no longer silently assume the wrong state tree.

### Slice 2: state-reader unification for operator truth

Update operator-facing status/reporting surfaces so they read from the configured authority surface.
For `eeepc`, that means the live state reader should be able to source truth from:
- `/var/lib/eeepc-agent/self-evolving-agent/state`

At minimum, the unified reader should expose:
- latest report path
- latest cycle status
- approval status
- selected goal ID
- artifact/evidence paths
- whether the source is host-control-plane or workspace-slice

### Slice 3: one-cycle convergence proof

After the authority-reader/config slice is in place, prove one live cycle where the operator-facing summary and the underlying report are derived from the same canonical state tree.

That proof must show:
- the selected authority path
- the report path
- cycle status
- approval state
- artifact or evidence path
- no mismatch between summary surface and underlying report

## Acceptance Criteria

- the repo contains one explicit documented and code-visible authority-selection boundary for self-evolving state
- operator-facing runtime truth can identify which state tree it is reading from
- on `eeepc`, the selected live authority can be set to `/var/lib/eeepc-agent/self-evolving-agent/state`
- one verified host cycle can be summarized from that same authority without mixing in an unverified workspace-state surface
- docs no longer imply that gateway workspace state is the live authority on `eeepc` unless it has been explicitly proven

## Definition of Ready

- the live host source of truth has been verified
- the current mismatch between repo workspace-state surfaces and host control-plane state is documented
- a bounded convergence direction has been chosen
- the implementation slice is small enough to complete without replacing the entire runtime
- host verification commands and evidence expectations are known

## Definition of Done

- an authority-selection/config boundary exists in code or runtime configuration
- an operator-facing reader/status path uses that boundary truthfully
- one host validation shows summary and report derived from the same configured authority tree
- docs reflect the implemented convergence rule
- any remaining deeper convergence work is explicitly left as follow-up, not hidden

## Verification Plan

1. Configure or inspect the runtime to confirm the selected self-evolving authority path.
2. Trigger or inspect one fresh host cycle.
3. Compare:
   - operator-facing summary
   - underlying report JSON
   - authority path used
4. Confirm the following fields all come from the same state tree:
   - cycle status
   - approval status
   - goal ID
   - artifact/evidence path
5. Record the result in a bounded proof note or test output.

## References

- `docs/EEEPC_SELF_EVOLVING_HOST_PROOF_2026-04-15.md`
- `docs/EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md`
- `docs/SOURCE_OF_TRUTH_AND_PROMOTION_POLICY.md`
- `docs/WORKSPACE_RUNTIME_LANE.md`
- `docs/SELF_EVOLVING_RUNTIME_RESTORE_NOTE.md`
- `docs/HOST_WORKSPACE_ARTIFACT_TRIAGE.md`
- `/var/lib/eeepc-agent/self-evolving-agent/state`
- `/home/opencode/.nanobot-eeepc/workspace/state/`
