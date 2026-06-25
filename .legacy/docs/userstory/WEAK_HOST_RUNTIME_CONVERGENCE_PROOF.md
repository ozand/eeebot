# Weak Host Runtime Convergence Proof

Status: bounded proof artifact for issue #9. This is a proof-only slice; it does not broaden host mutation or rollout automation.

Checked at: 2026-04-22 16:02:33 UTC

## Scope

This document proves the current weak-host runtime is converged enough for operator truth surfaces to match the underlying authority/control artifacts.

## Verified surfaces

1. Direct host reachability
- source: `control/eeepc_reachability.json`
- expectation: reachable is true and freshness is recent

2. Control-plane summary
- source: `/api/system`
- expectation: execution/approval state is readable and matches current local control artifacts

3. Active execution registry
- source: `control/active_execution.json`
- expectation: the dashboard can point to one concrete active execution state, even if stale-detection remains visible

4. Producer-side runtime authority
- source: `workspace/state/control_plane/current_summary.json`
- expectation: cycle/result/report/runtime-source are present

5. Latest report
- source: producer summary `report_path`
- expectation: report status matches the current producer-side runtime truth

## Current bounded conclusion

The deployed weak-host runtime does not currently show a host-crash or reachability-collapse pattern.
The remaining gap is primarily operator-truth reconciliation around stale/live execution semantics, not raw host liveness.

This means the smallest safe stabilization slice is proof/convergence oriented, not broad host mutation.

## Evidence pointers

- `control/eeepc_reachability.json`
- `control/active_execution.json`
- `workspace/state/control_plane/current_summary.json`
- latest producer report referenced by `current_summary.json`
- `/api/system`
- `/api/summary`

## Closure rule for this slice

This proof is sufficient for the bounded issue slice when:
- reachability is true,
- control-plane API is readable,
- producer summary exists,
- latest report path exists,
- and the operator can compare those sources without hidden authority ambiguity.
