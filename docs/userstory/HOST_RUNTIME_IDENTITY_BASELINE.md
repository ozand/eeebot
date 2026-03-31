# Userstory: Host Runtime Identity Baseline

## User Story

As the maintainer of the `eeepc` runtime,
I want the local runtime identity and bounded permission posture to be visible in
config and capability output,
so that host-side autonomy decisions remain explainable before broader identity
hardening or permission expansion begins.

## Scope

In scope:

- one explicit runtime identity field,
- one explicit staged GitHub access field,
- capability/status surfacing for both,
- focused tests for visibility and default behavior.

Out of scope:

- host-wide permission refactors,
- credential rotation,
- broad GitHub write rollout,
- deployment automation changes.

## Acceptance Criteria

- Runtime identity can be expressed in the autonomy config.
- GitHub access stage can be expressed in the autonomy config.
- Capability reporting shows both values clearly.
- Default behavior stays bounded and deny-by-default.
- Focused tests verify the new identity/access surfacing.

## Definition of Ready

- The active backlog still places host runtime stability above lower-WSJF work.
- Existing host rollout userstories are insufficient for this narrower identity slice.
- The implementation can stay within config/schema, capability snapshot, and focused tests.
- No broader identity hardening is required to complete this slice.

## Definition of Done

- Config fields are implemented with safe defaults.
- Runtime capability output renders the new identity/access state.
- Focused tests pass.
- `todo.md` links this userstory in the active host-runtime workstream.

## References

- `todo.md`
- `docs/IDENTITY_ACCESS_ROLLOUT.md`
- `docs/HOST_CAPABILITY_POLICY.md`
