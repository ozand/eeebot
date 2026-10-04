# Proposal: Version-bound current context rule provenance

**Issue:** [#2058](https://github.com/ozand/eeebot/issues/2058)
**Consumer:** [eeebot-ops-dashboard#386](https://github.com/ozand/eeebot-ops-dashboard/issues/386)
**Status:** ADR-037 was operator-accepted via PR #2059. Runtime implementation is present on the #2058 branch; acceptance remains subject to the PR's exact-SHA review and CI gates. No host deployment is authorized.

## Problem

The runtime owns its context builder rules, but consumers can currently see only per-build telemetry, not a bounded, current rule/source contract. A dashboard needs deployed rules and current approved input status independently of the last prompt row. Copying rule values into the dashboard would create competing authorities; exposing prompt/source text would violate the established privacy boundary.

## Proposal

Implement the accepted ADR-037 bounded, versioned provenance contract. The proposal is the implementation plan; ADR-037 is the architectural decision. Move only the static declarative rule fields currently owned by `ContextBuilder` into a small standard-library-only runtime module consumed by both the builder and exporter. Keep `PRIORITIES_BLOCK_CAP` owned by `nanobot.runtime.operator_documents` and imported by the exporter; do not relocate or duplicate it. Keep dynamic allocation and input reads in the builder.

The exporter serializes a descriptor-only artifact from the **exact selected target commit** used by `host/eeepc/scripts/deploy_release.sh`, with the full commit SHA supplied explicitly. It must not import from the developer checkout's unrelated `HEAD`. Add the artifact to that candidate release archive. Before activation, the remote deployment path validates the artifact schema/version and its embedded full SHA against that release's `SOURCE_COMMIT`; any absence, malformed contract, or mismatch aborts before switching `current`. The verify-only candidate path exercises the same validation without activation. Legacy/install paths without trustworthy commit provenance expose the rules as unavailable rather than guessing.

The contract describes static rules and labels dynamic values honestly: default system cap plus override variable name (effective override not claimed), release pool/order/floor (not per-file effective capacity), and descriptors/conditional labels (not current input presence or historical inclusion). Current approved-root input status is a separate read; `system_prompt` telemetry remains a per-cycle observation; exact payload remains #316. No prompt text, source content, credentials, persistent history writer, or daemon is added.

## Implementation acceptance criteria

- One canonical owner supplies each exported builder rule; rule extraction is consumed by `ContextBuilder`, not mirrored.
- Export is deterministic for a given target commit and carries that exact full SHA.
- Release and verify-only packaging use the selected target ref; malformed or mismatched metadata cannot activate a release.
- Contract is bounded and descriptor-only; no prompt/source content or secret values.
- Dynamic/environment-derived facts and current input status are explicitly distinguished from static rules.
- Missing provenance in legacy/install paths fails closed to unavailable.
- Existing builder behavior remains unchanged; tests bind the exporter to shared definitions and preserve release rollback/activation guarantees.
- Tests verify that only the approved metadata descriptors are exported, selected commit can differ from HEAD, unsupported legacy exporter fails closed, and verification never mutates or activates the live release.

## Out of scope

Dashboard implementation, telemetry redesign, a history store, daemon, host inspection or mutation, environment-secret disclosure, raw prompt/source text, and replacing cycle observations with the new contract. Host deployment remains separately gated and is not authorized by this implementation issue.
