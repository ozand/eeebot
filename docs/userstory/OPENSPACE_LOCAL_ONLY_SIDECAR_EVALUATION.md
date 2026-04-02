# Userstory: Evaluate HKUDS/OpenSpace as a Local-Only Sidecar

## User Story

As a maintainer of `eeepc`,
I want to evaluate HKUDS/OpenSpace only as a local bounded sidecar,
so that we can determine whether it strengthens `eeepc` without depending on the cloud/community solution and without expanding the local trust boundary.

## Scope

This story covers:

- checking whether the OpenSpace workflow can remain fully local,
- defining the sidecar boundary between `eeepc` and OpenSpace,
- identifying the smallest interface needed for local integration,
- and deciding whether the result is `go`, `no-go`, or `conditional go`.

This story does **not** cover:

- OpenSpace cloud/community deployment,
- public skill sharing,
- hosted multi-user setup,
- or a full production integration into the `eeepc` core runtime.

## Non-goals

- adopting `open-space.cloud`
- designing a hosted service
- replacing the current `eeepc` runtime
- production rollout of all OpenSpace features

## Acceptance Criteria

- It is explicitly confirmed whether HKUDS/OpenSpace can operate fully locally inside the `eeepc` trust boundary.
- The sidecar boundary is clearly described: what stays in `eeepc` and what would be delegated.
- The cloud/community path is explicitly excluded from the evaluated option.
- Major dependencies, host constraints, and operational risks are listed.
- The final verdict is clear: `go`, `no-go`, or `conditional go`.

## Definition of Ready

- The question is framed as a local-only sidecar evaluation.
- The cloud/community path is explicitly out of scope.
- Relevant local product constraints are known.

## Definition of Done

- A written verdict exists.
- The local-only boundary is explicit.
- Key blockers/risks are documented.
- The recommendation is actionable for the next product step.

## References

- `docs/userstory/README.md`
- `docs/userstory/HOST_RUNTIME_IDENTITY_BASELINE.md`
- `docs/userstory/HOST_RUNTIME_PACKAGING_CONSISTENCY_GATE.md`
- `docs/SOURCE_OF_TRUTH_AND_PROMOTION_POLICY.md`
