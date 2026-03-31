# Drift Budget And Reconciliation Policy

Last updated: 2026-03-28 UTC

## Purpose

This document defines how much host drift is acceptable, how drift should be
classified, and when the system must repair, reconcile, quarantine, or rebuild.

The project accepts bounded drift.
It does not accept unbounded, non-explainable drift.

## Definitions

### Drift

Any meaningful difference between:

- canonical source and deployed release,
- deployed release and live host state,
- expected baseline and actual runtime behavior,
- approved policy and actual mutable behavior.

### Reconciliation

The process of explaining and resolving drift without confusing host-local state
with canonical source.

### Drift budget

The bounded amount of divergence the project is willing to tolerate before action
is required.

## Drift Domains

Drift may occur in:

- code,
- config,
- prompts,
- runtime state,
- dependency/environment assumptions,
- evidence/export surfaces,
- policy behavior.

Not all domains carry the same risk.

## Drift Classes

### `expected_provisional`

Bounded local evolution that is still explainable and recoverable.

### `promotion_candidate`

Useful local change with evidence that may be worth replaying into canonical
source.

### `stale_provisional`

Previously tolerated drift that has not been promoted, renewed, or retired within
the expected review window.

### `unsafe_divergence`

Drift that threatens:

- bootstrap,
- recovery,
- truthfulness,
- canonical ownership,
- policy boundaries,
- explainability.

## Budget Principles

- some host drift is expected,
- drift must remain attributable,
- drift must remain bounded,
- drift must remain recoverable,
- drift must not silently redefine the base system.

If any of those stop being true, the budget has been exceeded.

## What Must Stay Stable

The following should not drift casually:

- canonical source ownership,
- release tree identity,
- bootstrap assumptions,
- recovery model,
- policy-defined high-risk boundaries,
- promotion target rules.

## Detection And Reporting

Drift should be detected through:

- deployment fingerprints,
- evidence manifests,
- changed-path summaries,
- capability snapshots,
- runtime validation checks,
- periodic reconciliation reviews.

Drift reporting should capture:

- what drifted,
- when it drifted,
- why it drifted,
- how risky it is,
- what action is required.

## Reconciliation Workflow

1. detect drift,
2. classify its domain and severity,
3. decide whether it is acceptable, promotable, stale, or unsafe,
4. record evidence,
5. choose one outcome:
   - keep local,
   - promote,
   - quarantine,
   - rollback,
   - rebuild from baseline.

## Repair vs Rebuild Rule

Prefer repair when:

- the drift is local,
- the cause is understood,
- invariants still hold,
- rollback or forward-fix is straightforward.

Prefer rebuild when:

- local state is no longer trustworthy,
- drift has compounded across critical surfaces,
- policy and runtime behavior no longer align,
- continuing repair would preserve harmful architectural ambiguity.

## Stale Drift Rule

Provisional drift should not live forever.

If a host-local mutation is not:

- promoted,
- intentionally retained,
- or explicitly retired,

it should eventually be classified as stale and resolved.

## Unsafe Drift Rule

When drift becomes unsafe:

- stop treating the affected state as reliable,
- preserve evidence,
- quarantine or discard the harmful mutation,
- restore a trusted release or rebuild from baseline.

## Direct Host Patch Interaction

Direct host patching may reduce immediate symptoms, but it does not erase drift.

Any SSH rescue patch must still be reconciled into one of these outcomes:

- canonicalized,
- retired,
- replaced by rebuilt baseline.

## Practical Rule

If a difference exists but nobody can explain whether it is intentional, bounded,
and recoverable, it should be treated as suspicious drift until proven otherwise.
