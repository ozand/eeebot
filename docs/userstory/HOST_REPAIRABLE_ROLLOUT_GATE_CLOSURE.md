# Userstory: Host Repairable Rollout Gate Closure

## User Story

As the maintainer of the `eeepc` host runtime,
I want candidate rollout to fail closed when host/runtime integrity is too weak,
so that weak-host deployment remains explainable, repairable, and rollback-safe
before we widen permissions or automation.

## Scope

This story covers one bounded package:

- classify rollout provenance as trusted or degraded,
- make weak rollout metadata explicit,
- preserve a deterministic rollback posture,
- keep the host in a repairable state instead of silently accepting ambiguous deploys.

It does not cover:

- identity hardening beyond the current rollout gate,
- a new deploy system,
- broader promotion automation,
- or unrelated runtime-lane UX work.

## Acceptance Criteria

- Degraded provenance is surfaced explicitly rather than treated as normal-good state.
- Candidate rollout metadata is strong enough to explain what was built and what is intended to run.
- Weak-host rollouts remain rollback-safe and reviewable.
- Focused validation proves the gate behavior without introducing a second deployment architecture.
- The package leaves the host either in a known-good running state or in a clearly degraded-but-repairable state.

## Definition of Ready

- The host/runtime stability workstream is still active and packaging integrity remains an open blocker.
- Existing userstories already cover provenance and rollout safety, so this slice does not require a new architecture.
- The next implementation can remain bounded to governance/deploy-gate paths.
- Focused tests can prove the behavior without a full host rebuild.

## Definition of Done

- The rollout gate makes degraded-vs-trusted state explicit.
- A weak or incomplete candidate cannot silently pass as normal-good rollout input.
- Focused tests pass.
- The host/runtime stability notes in `todo.md` reflect the new guardrail state.

## References

- `docs/RELEASE_ARTIFACT_AND_ROLLBACK_CONTRACT.md`
- `docs/DEPLOY_DECISION_MATRIX.md`
- `docs/VERSION_AND_PROVENANCE_MODEL.md`
- `todo.md`
