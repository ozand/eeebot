# Nanobot Product Goals Master Plan

> For Hermes: execute this plan sequentially without pausing for handoff unless a real blocker appears. After each tranche, verify with focused tests and live proof where applicable.

Goal:
Close the remaining product-level gaps between the current bounded working system and the full `nanobot eeepc` charter goals.

Current verified baseline:
- live eeepc self-evolving cycles can PASS
- approval persistence is now automated on host
- subagent bridge is now producing durable reports and live reports show enabled subagent rollup
- repo-side bounded runtime has comparable read/write/promotion/status proof surfaces
- dashboard exists and is operational on this host

Still-open product goals to execute:
1. full repo-side/runtime vs live host state convergence
2. full write-path convergence
3. full promotion-path convergence
4. accepted improvements into canonical repos
5. research-ingestion pipeline from agents_library
6. autonomous project creation when work outgrows bounded host mutation
7. safe rebuild-from-baseline proof
8. portable weak-host baseline/profile system
9. live Telegram parity
10. measurable efficiency improvement proof over time

Execution order:

## Tranche 1 — State convergence
Objective:
Unify the operator-facing truth model so repo-side and host-control-plane surfaces can be normalized through one explicit contract.

Deliverables:
- convergence spec note
- code/tests to close remaining state-reader mismatches
- proof note showing one same-shape summary for repo-side and live host

Definition of done:
- `nanobot status` and supporting readers produce one coherent canonical summary contract for both sources
- focused tests green
- live host proof rerun if needed

## Tranche 2 — Full write-path convergence
Objective:
Bring repo-side bounded writer outputs closer to the live host control-plane summary/evidence layout.

Deliverables:
- extended comparable outbox/index contract
- artifact/evidence summary parity improvements
- tests and proof note

Definition of done:
- repo-side writes produce a richer host-comparable evidence summary surface
- tests green

## Tranche 3 — Full promotion-path convergence
Objective:
Make promotion summary, trail, and accepted/rejected surfaces consistent across repo-side and live proof paths.

Deliverables:
- promotion path normalization changes
- tests for accepted/rejected visibility
- proof note

Definition of done:
- deterministic promotion state/trail is coherent end-to-end in status and durable files

## Tranche 4 — Canonical repo promotion path
Objective:
Define and implement the minimum safe path by which accepted improvements become canonical repo changes.

Deliverables:
- accepted-improvement export/apply workflow
- guarded repo promotion runbook
- tests or dry-run proof where direct live automation is too risky

Definition of done:
- accepted improvement can be moved into canonical repo through a documented, testable path

## Tranche 5 — Research ingestion pipeline
Objective:
Create a bounded pipeline for ingesting patterns from `agents_library`, ranking them, and tracking adoption/rejection.

Deliverables:
- research ledger or index surface
- mapping from research item to bounded experiment/backlog entry
- proof note and tests

Definition of done:
- repeatable research-to-backlog/adoption pipeline exists

## Tranche 6 — Autonomous project creation
Objective:
Allow the system to create/register side projects when bounded host mutation is no longer the right fit.

Deliverables:
- project seed manifest format
- project registry
- bootstrap flow proof

Definition of done:
- one bounded side-project bootstrap can be created and recorded safely

## Tranche 7 — Safe rebuild from baseline
Objective:
Prove the system can be rebuilt from canonical source + evidence after drift/failure.

Deliverables:
- rebuild runbook
- baseline restore proof
- explicit checkpoints

Definition of done:
- reproducible rebuild procedure is documented and tested in bounded form

## Tranche 8 — Portable weak-host baseline/profile system
Objective:
Extract host-specific assumptions into baseline + overlays suitable for similar weak hosts.

Deliverables:
- baseline profile
- eeepc overlay
- validation checklist

Definition of done:
- weak-host deployment assumptions are explicit and portable

## Tranche 9 — Live Telegram parity
Objective:
Close the real operator-path gap for Telegram.

Deliverables:
- live Telegram probe execution
- parity fixes if needed
- proof note

Definition of done:
- minimal Telegram operator sequence succeeds repeatably

## Tranche 10 — Measurable efficiency proof
Objective:
Show that the system becomes more effective over time rather than merely functioning.

Deliverables:
- metric definitions
- collection/reporting path
- proof note over multiple cycles

Definition of done:
- measurable efficiency evidence exists across repeated cycles

Immediate next step:
Start Tranche 1 by auditing the current remaining mismatch surfaces between repo-side state and live host state, then implement the smallest missing normalization slice first.
