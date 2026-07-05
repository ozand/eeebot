# Data Contracts — spec

_Status: current. Last updated: 2026-06-25._

## Purpose

Data contracts define the canonical, machine-readable schema and provenance types
used by the eeebot/`eeepc` governance, deploy, promotion, provenance, and
reconciliation model. Every structured record that matters for deploy, promote,
reconcile, rollback, audit, or runtime truthfulness has a registered schema
identity and version, so it is identifiable, versioned, owned, and validateable.

## Requirements

### Schema identity and versioning
- R1. Any record used for deploy, promote, reconcile, rollback, audit, or runtime
  truthfulness SHALL carry a registered schema identity (`schema_id`) and version
  (`schema_version`) before it is treated as governance-grade data.
- R2. `schema_id` SHALL be a stable dotted name (e.g. `eeepc.cycle_record`) and
  SHALL NOT be reused for a different meaning. `schema_version` SHALL be semantic
  (`MAJOR.MINOR.PATCH`): major = breaking shape/meaning change, minor = additive
  backward-compatible change, patch = non-breaking clarification.
- R3. Published schema versions SHALL be immutable. Unknown or incompatible
  versions SHALL fail closed.

### Registered types
- R4. The registry SHALL define and conform records to these canonical types:
  - `eeepc.schema_registry` — registry manifest of approved schemas/versions/
    status/owners.
  - `eeepc.identity_primitives` — shared identity rules for stable IDs and tuples.
  - `eeepc.cycle_record` — bounded execution record for a host cycle.
  - `eeepc.evidence_ref` — reference to a content-addressed or URI-backed evidence
    item.
  - `eeepc.promotion_candidate` — host-born change package eligible for promotion
    review.
  - `eeepc.release_artifact` — versioned deployable artifact built from canonical
    source.
  - `eeepc.deployment_fingerprint` — machine-readable identity of what is actually
    installed and running.
  - `eeepc.deploy_decision` — structured rollout/reconcile/rollback/rebuild choice.
  - `eeepc.drift_classification` — closed-set classification of bounded, stale,
    promotable, or unsafe drift.
  - `eeepc.reconciliation_record` — record of repair, quarantine, rollback, replay,
    or rebuild action.
  - `eeepc.rollback_event` — structured rollback trigger, target, and result.
  - `eeepc.change_propagation_event` — record of movement between local, canonical,
    artifact, host, and promotion surfaces.
  - `eeepc.validation_result` — structured outcome of checks, policy evaluations,
    or smoke validations.

### Mapping runtime artifacts to schemas
- R5. A `state/reports/evolution-*.json` cycle log SHALL conform to
  `eeepc.cycle_record`; a `state/promotions/` candidate SHALL conform to
  `eeepc.promotion_candidate`; a release artifact SHALL conform to
  `eeepc.release_artifact`; a validation/smoke result SHALL conform to
  `eeepc.validation_result`.

### Record shape and provenance
- R6. Where relevant, records SHALL use the common baseline fields: `schema_id`,
  `schema_version`, `record_id`, `created_utc`, `owner`, `status`, `references`,
  `content_hash`.
- R7. Provenance-heavy records SHALL additionally carry linkage fields where
  applicable: `origin_cycle_id`, `promotion_candidate_id`, `artifact_id`,
  `deployment_fingerprint_id`, `evidence_ref_id`.
- R8. Each schema SHALL have exactly one accountable owner role; ownership confers
  authority to evolve the definition, not to silently rewrite historical records.

### Authority and validation
- R9. The canonical source SHALL remain the source of truth for registry
  definitions; host-local state MAY emit records but SHALL NOT redefine schema
  meaning.
- R10. Validation SHALL check at least: required-field presence, stable-identity
  presence, provenance-linkage integrity, repo/branch and ownership validity where
  relevant, schema-version compatibility, and content-hash integrity where
  available.

### Deprecation
- R11. Deprecated schemas SHALL remain readable for a defined support window;
  breaking changes SHALL require a version or schema-identity transition; producers
  SHALL stop emitting end-of-life versions; migration SHALL proceed through
  explicit versioning, never silent mutation.

## Scenarios

### Scenario: cycle log conforms to its schema
- Given a self-evolving cycle completes
- When `state/reports/evolution-<ts>-<cycle_id>.json` is written
- Then the record carries `schema_id = eeepc.cycle_record` and a `schema_version`,
  and validation confirms required identity and provenance fields are present.

### Scenario: unknown schema version fails closed
- Given a consumer reads a record with a `schema_version` it does not recognize
- When it validates the record
- Then it rejects the record rather than guessing compatibility.

### Scenario: promotion candidate links to its origin cycle
- Given a promotion candidate is produced from a host cycle
- When it is written under `state/promotions/`
- Then it conforms to `eeepc.promotion_candidate` and its `origin_cycle_id` links
  back to the producing `eeepc.cycle_record`.

## References

- Reference doc: `SCHEMA_REGISTRY.md` was folded into this spec and removed
  2026-07-05 (#613; recoverable from git history).
- Code: `nanobot/runtime/schemas.py` (`CycleReport`, `PromotionCandidate`, …),
  `nanobot/runtime/state.py`, `nanobot/runtime/state_promotion.py`,
  `nanobot/runtime/promotion.py`, `nanobot/runtime/health.py`.
- Related specs: `self-evolving-runtime`, `promotion-and-release`, `host-runtime`.
