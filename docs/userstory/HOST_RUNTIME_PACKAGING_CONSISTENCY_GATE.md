# Userstory: Host Runtime Packaging Consistency Gate

## User Story

As the maintainer of the `eeepc` host runtime,
I want candidate runtime updates to pass through a bounded packaging consistency gate,
so that weak-host rollout remains explainable, rollback-safe, and does not drift away from the artifact/runtime truth we intend to deploy.

## Scope

This story covers one bounded package:

- candidate runtime/source integrity,
- rollout provenance quality,
- degraded provenance warnings,
- rollback-safe gate behavior.

It does not cover:

- full identity hardening,
- broad host rebuild automation,
- wider platform compatibility beyond the gate,
- unrelated runtime-lane UX or Telegram feature work.

## Acceptance Criteria

- Candidate build records include explicit provenance such as `source_commit` and `build_recipe_hash` when available.
- Degraded provenance is surfaced as degraded/weak rather than silently treated as normal-good state.
- Deploy-facing records and docs make it clear when rollout metadata is too weak to trust for stronger automation.
- Focused validation proves the new gate behavior without requiring broad host mutation.
- The package stays narrow and does not introduce a second deployment system.

## Definition of Ready

- The active backlog still places host runtime stability above lower-WSJF work.
- Existing rollout provenance and runtime-candidate checks are already in place.
- The next slice can stay within deploy-envelope/governance files without widening host permissions.
- Focused validation can be run without full host rebuild automation.

## Definition of Done

- Candidate rollout metadata exposes degraded provenance explicitly.
- The pre-deploy gate can use that metadata to block or warn in a deterministic way.
- Focused governance/package tests pass.
- `todo.md` links remain accurate for this workstream.

## References

- `docs/RELEASE_ARTIFACT_AND_ROLLBACK_CONTRACT.md`
- `docs/VERSION_AND_PROVENANCE_MODEL.md`
- `docs/DEPLOY_DECISION_MATRIX.md`
- `todo.md`
