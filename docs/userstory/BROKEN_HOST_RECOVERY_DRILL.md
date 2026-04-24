# Broken Host Recovery Drill

Status: bounded recovery proof artifact for issue #45. This drill is intentionally proof-oriented and read-only; it does not mutate the live host.

Date: 2026-04-22 UTC

## Bottom line

The smallest safe recovery-drill slice is not live repair. It is a read-only proof that a broken host can be classified into the correct recovery posture and that the documented recovery path resolves to one of:
- host reconciliation,
- rollback,
- or rebuild from baseline.

If the host is no longer trustworthy, the drill must stop at documented recovery selection and evidence capture.

## Purpose

Prove that the project can recover from broken-host states without relying on risky host mutation.

This slice exists to show that:
- recovery decisions are documented,
- the system knows when repair is safe and when rebuild is required,
- canonical source plus baseline configuration are enough to re-establish a trusted starting point,
- no live host mutation is needed to validate the recovery model.

## Covered recovery surfaces

This drill covers only the recovery decision path and its proof surfaces:
- drift classification,
- rollback eligibility,
- rebuild-vs-repair selection,
- canonical source / evidence separation,
- baseline restart assumptions.

## Explicit exclusions

This drill does NOT include:
- package installation on the target host,
- service restart or migration,
- SSH rescue patching,
- writes to live authority roots,
- promotion of host-born changes,
- any other live host mutation.

## Minimum proof set

1. Broken-host classification is defined
- source: `docs/DRIFT_BUDGET_AND_RECONCILIATION_POLICY.md`
- source: `docs/DEPLOY_DECISION_MATRIX.md`

2. Recovery posture is defined
- source: `docs/DEPLOY_DECISION_MATRIX.md`
- source: `docs/MAINTAINER_OPERATING_MODEL.md`

3. Baseline restart, verification, and rollback path are defined
- source: `docs/SAFE_BOOTSTRAP_FROM_SCRATCH.md`
- source: `docs/BASE_CONFIGURATION_PROFILE.md`
- source: `docs/EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md`
- source: `docs/EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md`

4. Recovery remains evidence-backed and non-canonical host state is not trusted
- source: `docs/CHANGE_PROPAGATION_MODEL.md`
- source: `docs/PROJECT_CHARTER.md`

## Broken-host states this slice must classify

The proof only needs to distinguish these two cases:

1. `degraded_but_recoverable`
- invariants still hold,
- rollback or forward repair is straightforward,
- repair does not deepen drift.

2. `unrecoverable_without_rebuild`
- local state is no longer trustworthy,
- drift spans multiple critical surfaces,
- continuing repair would preserve ambiguity,
- the safe next step is rebuild from baseline.

## Current bounded conclusion

For issue #45, the smallest safe proof slice is a recovery-policy audit with a read-only decision trace.

That is enough to prove:
- the broken host can be classified,
- the operator can choose rollback versus rebuild based on documented criteria,
- the recovery path is explicit even when live host state is damaged.

It is not enough to claim a live repair or host restoration has been executed.

## Closure rule for this bounded slice

This drill is sufficient when all of the following are true:
- the recovery path is documented,
- the proof remains read-only,
- rollback / repair / rebuild decision criteria are explicit,
- the rebuild-from-baseline fallback is clearly named,
- operators can distinguish proof readiness from mutation readiness.
