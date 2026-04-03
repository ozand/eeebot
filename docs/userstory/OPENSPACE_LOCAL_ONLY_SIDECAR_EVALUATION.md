# Userstory: Integrate HKUDS/OpenSpace as an External MCP Sidecar

## User Story

As a maintainer of `eeepc`,
I want to integrate HKUDS/OpenSpace as an external bounded MCP sidecar,
so that `eeepc` can use it through a controlled remote interface without depending on the cloud/community solution and without overloading the weak host.

## Scope

This story covers:

- defining the external sidecar boundary between `eeepc` and OpenSpace,
- wiring a remote MCP SSE server entry for OpenSpace,
- verifying that the weak host does not need to run OpenSpace locally,
- and deciding whether the result is `go`, `no-go`, or `conditional go`.

This story does **not** cover:

- OpenSpace cloud/community deployment,
- public skill sharing,
- replacing the current `eeepc` runtime,
- or a deep in-process integration into the `eeepc` core runtime.

## Non-goals

- adopting `open-space.cloud`
- designing or relying on the cloud/community service
- replacing the current `eeepc` runtime
- production rollout of all OpenSpace features

## Acceptance Criteria

- The sidecar boundary is clearly described: what stays in `eeepc` and what would be delegated to the external OpenSpace runtime.
- The OpenSpace MCP SSE endpoint is represented in the runtime configuration in a bounded way.
- The cloud/community path is explicitly excluded from the evaluated option.
- Major dependencies, host constraints, and operational risks are listed.
- The final verdict is clear: `go`, `no-go`, or `conditional go`.

## Definition of Ready

- The question is framed as an external MCP sidecar integration.
- The cloud/community path is explicitly out of scope.
- The external endpoint and transport are known.

## Definition of Done

- A written verdict exists.
- The external sidecar boundary is explicit.
- Key blockers/risks are documented.
- The recommendation is actionable for the next product step.

## References

- `docs/userstory/README.md`
- `docs/userstory/HOST_RUNTIME_IDENTITY_BASELINE.md`
- `docs/userstory/HOST_RUNTIME_PACKAGING_CONSISTENCY_GATE.md`
- `docs/SOURCE_OF_TRUTH_AND_PROMOTION_POLICY.md`
