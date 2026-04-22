# Broken Host Recovery Drill

Status: bounded recovery proof artifact for issue #45. This drill is intentionally read-only and policy-oriented; it does not attempt live host mutation.

## Purpose

Prove that recovery from a broken host state can be decided safely through a documented operator decision trace before any risky repair action is attempted.

## Covered recovery states

This drill classifies only two bounded cases:

1. degraded_but_recoverable
- authority still readable
- rollback/reconcile path still defined
- baseline bootstrap path remains available

2. unrecoverable_without_rebuild
- authority path is not trustworthy enough for bounded repair
- rollback is unavailable or insufficient
- rebuild from baseline is the only safe documented path

## Decision trace

For a broken host state, the operator chooses exactly one path:

1. reconcile
- use when state is degraded but authority/proof surfaces still converge
- source: `docs/DRIFT_BUDGET_AND_RECONCILIATION_POLICY.md`

2. rollback
- use when a prior known-good runtime artifact exists and rollback contract is intact
- source: `docs/EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md`
- source: `docs/RELEASE_ARTIFACT_AND_ROLLBACK_CONTRACT.md`

3. rebuild from baseline
- use when bounded repair or rollback is no longer trustworthy
- source: `docs/SAFE_BOOTSTRAP_FROM_SCRATCH.md`
- source: `docs/BASE_CONFIGURATION_PROFILE.md`

## Read-only proof checklist

- host class and bootstrap baseline are documented
- deploy/rollback contract is documented
- operator approval/review boundaries remain documented
- a clear decision matrix exists for reconcile vs rollback vs rebuild
- the drill can be executed as a proof exercise without changing the live host

## Current bounded conclusion

The recovery gap can be narrowed safely by using a read-only recovery-policy drill first.

This proves the decision spine for broken-host handling without pretending that live repair has already been exercised.

## Explicit exclusions

This drill does NOT include:
- live package installation
- service restarts on the target host
- runtime rewrites on the target host
- live promotion/rollback execution
- broad self-healing mutation

## Source references

- `docs/DEPLOY_DECISION_MATRIX.md`
- `docs/SAFE_BOOTSTRAP_FROM_SCRATCH.md`
- `docs/BASE_CONFIGURATION_PROFILE.md`
- `docs/EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md`
- `docs/EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md`
- `docs/DRIFT_BUDGET_AND_RECONCILIATION_POLICY.md`
- `docs/CHANGE_PROPAGATION_MODEL.md`

## Closure rule for this bounded slice

This proof slice is sufficient when:
- the broken-host decision trace is explicit,
- reconcile / rollback / rebuild-from-baseline are distinguishable,
- the drill remains read-only,
- and operators can execute the classification safely before any mutation.
