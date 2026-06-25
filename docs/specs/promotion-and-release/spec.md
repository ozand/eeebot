# Promotion and Release — spec

_Status: current. Last updated: 2026-06-25._

## Purpose

This capability governs how a change moves from the mutable live host/workspace
into durable product truth: host mutation → evidence → promotion candidate →
canonical source → release artifact → deployed release, plus how rollback,
provenance, and drift are handled. It exists to keep two surfaces separate — the
**canonical Git-managed source** (durable truth) and the **mutable host execution
plane** (real but provisional) — so the system stays recoverable, replayable, and
auditable. Canonical source is authoritative; host state is observable but never
the source of truth until promoted. Detailed walkthroughs live in the legacy
reference docs (see References).

> This is **product/runtime** behavior. How *we* develop this product is in
> `AGENTS.md` / `CONSTITUTION.md`.

## Requirements

### Source-of-truth boundary

- R1. Canonical product/control-plane source SHALL live only in Git-managed repos
  under the `ozand` namespace (canonical: `ozand/eeebot`). Bot-owned host-evolution
  namespaces SHALL NOT be treated as canonical source, and evidence-only repos
  SHALL NOT be valid canonical targets.
- R2. The host SHALL be treated as an execution and evidence surface only. Live
  authority state SHALL be read from the active runtime state root (on the eeepc
  host, `/var/lib/eeepc-agent/self-evolving-agent/state`); a healthy gateway deploy
  SHALL NOT by itself be presented as proof of the live self-evolving authority.
- R3. Host-local mutable state SHALL NOT redefine canonical identity, and direct
  host edits SHALL NOT become canonical until promoted.

### Promotion gate

- R4. A host-born change SHALL be treated as real but **provisional**, becoming
  canonical only after: evidence exists, replayability outside the live host is
  established, the canonical target repo/branch is valid, and review or policy
  approves it.
- R5. A promotion candidate SHALL carry stable identity and provenance metadata at
  minimum: `promotion_candidate_id`, `origin_cycle_id`, `target_repo`,
  `target_branch`, `base_commit`, `candidate_patch_hash`, `source_paths`,
  `evidence_refs`, `validation_summary`, `rollback_plan`, `review_status`,
  `decision`. A candidate missing `evidence_refs` SHALL be incomplete.
- R6. Promotion SHALL pass the staged gate before acceptance: eligibility (allowed
  mutation surface, correct canonical target), evidence completeness, safety and
  recoverability (clear rollback, ownership preserved, no prohibited content),
  functional validity (smoke/test/runtime validation), and weak-host fit
  (affordable CPU/memory/disk). The gate SHALL fail closed.
- R7. A review decision SHALL be exactly one of `accept | reject | defer |
  needs_more_evidence`. Promotion SHALL be auto-rejected when evidence is missing,
  the canonical target is invalid, ownership rules are violated, rollback is
  unclear, the change is unexplainable, weak-host cost is too high, or the candidate
  is only noisy local drift.
- R8. Host-born changes SHALL NOT auto-land on `main`; they SHALL flow through a
  reviewable promotion branch (`promote/eeepc-*`, `promote/host-*`).

### Branches and release channels

- R9. Branch class SHALL be evident from name: canonical (`main`, `nightly`),
  upstream intake (`sync/upstream-*`), promotion (`promote/*`), release
  (`release/*` or immutable tags), emergency (`hotfix/*`, `emergency/*`). The
  autonomous cycle branch SHALL be `selfevo/cycle-<id>`, integrated to `main` only
  after the smoke gate passes.
- R10. Deployable artifacts SHALL be produced only from approved stable branches,
  explicitly-tested candidate branches, or immutable release tags — never from raw
  sync, raw promotion, evidence-only, or arbitrary host-snapshot branches.
- R11. Allowed merge directions SHALL be explicit: `upstream → sync/upstream-*`;
  `host-born → promote/* → reviewed canonical branch`; `canonical → release
  artifact → host deployment`. Direct evidence-repo→`main` and upstream→stable
  overwrites SHALL NOT be performed.

### Release artifacts, deployment, rollback

- R12. The normal deployment unit SHALL be a versioned release artifact built from
  canonical source — not an ad hoc host patch. Artifacts SHALL NOT include secrets,
  credential stores, raw inbox data, full volatile state trees, or unapproved
  host-local overlays, and SHALL keep release payload separate from runtime state
  and evidence.
- R13. Each artifact SHALL carry traceable metadata including `artifact_id`,
  `artifact_version`, `release_channel`, `source_repo`, `source_commit`,
  `build_timestamp_utc`, `target_host_profile`, `included_paths`/`excluded_paths`,
  `deploy_strategy`, `rollback_strategy`, and `previous_known_good_artifact_id`. An
  artifact without `source_commit` SHALL be invalid.
- R14. Each deploy SHALL emit a deployment fingerprint (`engine_sha`,
  `control_plane_sha`, `policy_hash`, `artifact_id`, `artifact_version`,
  `release_channel`) describing what is actually running. A fingerprint without
  `artifact_id` SHALL be invalid.
- R15. Deployment SHALL be guarded: a candidate release is created
  (`releases/<id>` + `current` symlink), gated by a post-deploy health check, and
  promoted only on pass. Post-deploy validation SHALL cover process start/stability,
  operator control path, truthful capability reporting, evidence write path, and
  weak-host resource fit.
- R16. Rollback SHALL be available after every deployment, SHALL preserve evidence
  explaining why it occurred, and SHALL NOT depend on damaged mutable state. The
  recovery order SHALL be: (1) previous known-good artifact, (2) baseline-compatible
  configuration, (3) justified evidence-backed state replay, (4) rebuild from
  canonical baseline. Rollback SHALL be triggered on startup failure, post-deploy
  health-check failure, capability regression, unacceptable resource impact, invalid
  artifact metadata, host-profile incompatibility, or operator request — and on gate
  failure a rollback/failure-learning artifact SHALL be written.

### Provenance, drift, validation, decision

- R17. Identity primitives (`cycle_id`, `promotion_candidate_id`, `artifact_id`,
  `deployment_fingerprint_id`, `evidence_ref_id`) SHALL be immutable once assigned
  and SHALL NOT be reused or silently rewritten. Every promotion candidate SHALL
  reference one `origin_cycle_id`; every artifact one canonical `source_commit`;
  every deployment fingerprint one `artifact_id`. Ambiguous provenance SHALL fail
  closed; placeholder provenance (`unknown`, `local-build`) SHALL be treated as
  degraded, not normal-good.
- R18. Host drift SHALL be bounded and attributable, never unbounded or
  unexplainable. Drift SHALL be classified as `expected_provisional`,
  `promotion_candidate`, `stale_provisional`, or `unsafe_divergence` before
  reconciliation acts, and the chosen action SHALL match the trust level. Canonical
  ownership, release-tree identity, bootstrap assumptions, recovery model, and
  promotion-target rules SHALL NOT drift casually.
- R19. Stale provisional drift SHALL be promoted, intentionally retained, or
  retired; unconfirmed differences SHALL be treated as suspicious until classified.
  On `unsafe_divergence` the system SHALL stop trusting the affected state,
  preserve evidence, quarantine/discard the mutation, and restore a trusted release
  or rebuild from baseline.
- R20. Validation hooks SHALL run at every trust boundary (`pre_build`/`post_build`,
  `pre_promotion`/`post_promotion`, `pre_deploy`/`post_deploy`, `cycle_export`,
  `reconciliation`, `periodic_audit`), each emitting a structured record (timestamp,
  stage, inputs, result, failure reason, linked IDs). On missing identity/provenance,
  invalid ownership target, or confirmed unsafe drift, the hook SHALL block, force
  rollback/rebuild, or escalate.
- R21. Deploy/recovery decisions SHALL choose the smallest intervention preserving
  canonical ownership, evidence, replayability, rollback, and portability — in
  priority order: artifact rollout when canonical and deployable; reconcile/replay
  when bounded and explainable; bounded SSH patch only in incident mode (recorded,
  with mandatory canonical follow-up); rollback or rebuild when trust is lost. Every
  major decision SHALL record a decision trail (actor, chosen action, rejected
  alternatives, triggering evidence, drift class, IDs, rollback path, incident flag,
  follow-up). SSH patching SHALL NOT be the steady-state workflow.

### Evidence export

- R22. Each autonomous cycle SHALL emit a compact, Git-safe evidence record (cycle
  timestamp, goal/lane/source, result status, changed paths, artifact paths,
  capability snapshot, latest report summary/index pointer, promotion candidate
  metadata when present). Volatile state trees, secrets, raw inbox files, and
  unreviewed host-local source mutations SHALL NOT be blindly pushed into canonical
  source repos.

## Scenarios

### Scenario: host-born change is promoted, not auto-merged
- Given a bounded host mutation has landed with recorded evidence
- When it is packaged as a promotion candidate
- Then it gets a `promotion_candidate_id` linked to its `origin_cycle_id`, targets a
  `promote/eeepc-*` branch (never direct `main`), and is accepted only after the
  staged gate (eligibility → evidence → safety → functional → weak-host fit) passes.

### Scenario: promotion candidate missing evidence is rejected
- Given a candidate with no `evidence_refs` or an invalid canonical target
- When the promotion gate evaluates it
- Then it fails closed with a structured rejection reason and remains host-local
  only.

### Scenario: guarded release fails health check and rolls back
- Given a candidate release built from canonical source and symlinked at `current`
- When the post-deploy health check fails
- Then the `current` symlink is restored to the previous known-good release, a
  rollback/failure-learning artifact is written, and the release state is not
  promoted.

### Scenario: unsafe drift forces baseline recovery
- Given host divergence classified as `unsafe_divergence`
- When reconciliation runs
- Then the affected state stops being treated as reliable, evidence is preserved,
  the harmful mutation is quarantined/discarded, and a trusted artifact is restored
  or the host is rebuilt from baseline.

### Scenario: emergency SSH patch requires canonical follow-up
- Given incident mode where artifact delivery is too slow to restore runtime
- When a bounded SSH patch is applied
- Then the patch is recorded, a mandatory canonical follow-up is created, and the
  host returns to artifact-managed state as soon as possible.

## References

- Source/legacy docs (folded into this spec, archived under `.legacy/docs/`):
  `SOURCE_OF_TRUTH_AND_PROMOTION_POLICY.md`, `PROMOTION_GATE_SPEC.md`,
  `RELEASE_ARTIFACT_AND_ROLLBACK_CONTRACT.md`, `BRANCH_AND_RELEASE_CHANNEL_POLICY.md`,
  `CHANGE_PROPAGATION_MODEL.md`, `VERSION_AND_PROVENANCE_MODEL.md`,
  `DRIFT_BUDGET_AND_RECONCILIATION_POLICY.md`, `VALIDATION_HOOKS_PLAN.md`,
  `DEPLOY_DECISION_MATRIX.md`.
- Code: `nanobot/runtime/promotion.py` (readiness packets), `state_promotion.py`,
  `autoevolve.py` (`create_candidate_release`, `apply_candidate_release`,
  `health_check_release`, `rollback_release`, `derive_selfevo_branch_name`),
  `github_ops.py`, `health.py`, `local_ci.py`.
- Scripts: `scripts/guarded_self_evolve.py`, `scripts/create_candidate_release.py`,
  `scripts/health_check_release.py`, `scripts/commit_and_push_self_evolution.py`.
- Related specs: `self-evolving-runtime`, `host-runtime`, `observability`.
