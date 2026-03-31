# Initial Validator Roadmap

Last updated: 2026-03-28 UTC

## Purpose

This document defines the staged plan for turning the current governance specs
into executable validators.

The goal is not to validate everything at once.
The goal is to protect the highest-value invariants first.

## Initial Scope

The first validator wave should focus on:

- cycle records,
- promotion candidates,
- release artifacts,
- deployment fingerprints,
- evidence references,
- drift classifications,
- deploy decisions.

## Phase 0 - Rule Inventory

- map governance statements to machine-checkable rules,
- identify hard failures vs warnings,
- align each validator to a schema and a source policy document.

## Phase 1 - Core Identity And Provenance Validators

Implement the highest-value checks first:

- stable ID presence,
- candidate -> origin cycle linkage,
- artifact -> source commit linkage,
- deployment fingerprint -> artifact linkage,
- evidence reference completeness,
- target repo/branch validity.

Acceptance:

- malformed provenance or ownership records are rejected.

## Phase 2 - Promotion Gate Validators

Add checks for:

- evidence existence,
- replayability indicators,
- rollback or rejection plan presence,
- ownership boundary preservation,
- no forbidden canonical target.

Acceptance:

- no promotion can pass without evidence and valid target metadata.

## Phase 3 - Build And Deploy Validators

Add checks for:

- artifact metadata completeness,
- reproducible build input declaration,
- no unapproved overlays in release artifacts,
- post-deploy startup and control-path health,
- weak-host fit checks where measurable.

Acceptance:

- invalid artifacts or clearly unsafe deployments are blocked.

## Phase 4 - Runtime Export Validators

Add checks for:

- cycle manifest completeness,
- evidence bundle integrity,
- changed-path summaries,
- capability snapshot freshness,
- promotion metadata presence when emitted.

Acceptance:

- runtime exports become structurally auditable rather than free-form.

## Phase 5 - Reconciliation Validators

Add checks for:

- drift classification presence,
- stale vs unsafe drift distinction,
- repair vs rebuild decision support,
- quarantine or rollback trigger records.

Acceptance:

- reconciliation decisions are explainable and linked to evidence.

## Phase 6 - Hardening And Automation

- turn selected warnings into hard failures,
- integrate validators into CI and deploy flows,
- add regression fixtures,
- extend coverage to more schema types.

## Recommended First Checks

The minimum viable validator set should start with:

1. missing or duplicate stable IDs,
2. broken provenance links,
3. invalid target repo/branch ownership,
4. missing evidence refs for promotion candidates,
5. missing rollback plan,
6. artifact without canonical source commit,
7. deployment fingerprint without artifact ID,
8. drift record without classification.

## Governance Ownership

Validators should be owned by the policy/control-plane side, not by mutable host
state.

The policy docs define what should be true.
Validators are the enforcement path for those truths.

## Practical Rule

Start with the checks that prevent loss of provenance, ownership confusion, and
unsafe promotion.
Only after those are solid should the validator layer expand into richer runtime
quality checks.
