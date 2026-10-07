# Trusted exporter implementation plan (ADR-038)

**Issue:** #2058 · **Status:** design only; no code authorized until ADR-038 is explicitly accepted.

## Boundary and bootstrap

- Implementation/test mode takes an explicitly supplied fixture anchor SHA. It is marked non-authoritative in tests and cannot enter production deployment code paths. Keep fixture selection out of production CLI/environment switches; test harnesses must invoke a separate test-only API or inject the fixture directly. Missing production approval must fail closed.
- Production mode reads an operator-owned immutable full SHA and approval reference from configuration outside the selected target/archive. It must reject absent/malformed SHA, mutable refs, or unrecorded approval. Enable production only after implementation, independent security review, and explicit operator approval of the chosen SHA.

## Unresolved production bootstrap delivery decision

ADR-038's policy requires the bootstrap verifier, authority manifest, and approved pin to be independent of the selected target. The repository has no approved production provisioning path for those operator-owned artifacts, and this implementation plan does not authorize choosing an anchor or installing host/operator state. Before production enablement, the owner must choose and approve how the verifier and pin are delivered outside the target tree:

1. **Recommended:** an operator-managed local configuration directory outside the checkout supplies a read-only pin/approval record and manifest; a separately installed, independently reviewed verifier reads target Git objects as data, compares the closure, then launches only anchor code. This requires an explicit owner decision naming the provisioning/installation mechanism and access controls; none is implied here.
2. **Alternative:** keep production export disabled until a separately governed operator tool/release installs that checker and pin. #2058 can deliver the testable checker implementation and remain fail-closed in production meanwhile.

Until that decision and provisioning exist, production invocations with an exporter must fail closed before executing selected-ref code. This is a deployment/provisioning blocker, not permission to trust the checkout's current helper or a branch name. This bounded implementation may still deliver and unit-test the Git-object comparison logic against explicitly injected non-authoritative fixture pins; that test seam must be impossible to select through production CLI/environment inputs and cannot bypass production bootstrap validation.
- Target ref supplies only target commit identity and Git objects for comparison. Never execute/import target Python, use its `PYTHONPATH`, or accept its manifest/config as trust policy.

## Bounded authority manifest (to finalize in implementation review)

Store a reviewed manifest in the trusted anchor/config, not in the target. For every path compare target and anchor Git tree-entry type, mode, and blob object ID using Git object APIs before launching Python. Reject missing entries, symlinks, non-regular files, mode/type changes, or unequal blobs. Do not rely on `git show` that follows a symlink or compare content alone.

Initial execution closure to verify from import graph and tests:

- `scripts/export_context_metadata.py`
- `nanobot/runtime/context_rules.py`
- `nanobot/runtime/context_metadata.py`
- `nanobot/runtime/mutation_policy.py`
- `nanobot/runtime/operator_documents.py`
- any actual local module imported by the above (must be explicitly reviewed and added, otherwise reject)

Initial compared-claim authority (not necessarily executed by exporter):

- `nanobot/agent/context.py`, whose builder rule/order/assembly definitions must remain exactly the ones described by the exported metadata.
- Any other source owning a value the exporter claims; identify during independent review, then pin it in the manifest.

This is intentionally not the entire operational ContextBuilder dependency graph. The trusted exporter must stay stdlib-only; unexpected third-party or local imports fail closed. Manifest changes require independent review and a new approved anchor before production enablement.

## Execution sequence

1. Resolve target to full commit SHA; resolve approved anchor only from operator-owned config.
2. Validate mode and approval record; test-fixture mode is impossible to select in production invocation.
3. Read anchor-pinned manifest; query target and anchor Git tree entries; compare type, mode, blob IDs. Exit before subprocess creation on any difference.
4. Extract only the approved anchor tree to a private trusted-execution temporary directory. Never import or execute target files, and never add target paths to Python import paths. If selected-target source must be staged for the release archive or remote candidate gate, stage it separately as inert packaging data; no scripts/modules from that staging tree may run locally.
5. Execute the trusted exporter from the anchor-only extraction with the target full SHA as a data argument; output remains descriptor-only and protected by existing schema/size/permissions checks.
6. Package the selected-target release source and generated metadata in the release artifact as separate inputs; remote deployment/verify-only validators remain responsible for source-SHA/schema checks and activation barrier. Failure must preserve prior current release. Legacy no-exporter targets remain unavailable/diagnostic without local Python execution.
7. Clean the private extraction on all exits. No host access/deploy is part of this plan.

## Required adversarial tests

- Target exporter contains a sentinel writing/reading a canary: sentinel is never executed in normal or verify-only local flow.
- Target imported module contains a sentinel: never executed.
- For each manifest item, mutate blob, delete item, change regular file to symlink, or alter Git mode/type: fail before any Python subprocess; assert no metadata artifact.
- Add target-only exporter import/module and target-supplied manifest/config: ignored as trust sources; fail closed if authority/import closure is unknown.
- Same authority closure but unrelated candidate commit: anchor exporter runs and metadata embeds exact target full SHA (including target != HEAD).
- Fixture anchor can exercise all logic in tests but cannot be used by production deploy command or satisfy production approval validation.
- Missing/invalid production anchor and attempted mutable ref (branch/tag/HEAD alias) fail before execution.
- Anchor rotation requires a new explicit approval record; old approved SHA remains deterministic, no auto-follow.
- Verify-only drives actual non-HEAD candidate path; no current symlink activation; invalid closure/schema preserves existing live release.
- Existing selected-source absence/legacy behavior remains diagnostic-only and never runs selected source code.

## Acceptance order

1. Operator explicitly accepts Proposed ADR-038; until then docs/research only.
2. Implement with non-authoritative fixture anchor; no production export mode.
3. Focused tests, full applicable CI, and fresh independent security review at exact SHA.
4. Operator explicitly approves production anchor SHA and records approval outside candidate source.
5. Enable production export; any subsequent authority change repeats independent review and anchor approval.
6. PR merge remains blocked until P1 #4176489504 is cleared against exact reviewed SHA. Host deployment still requires separate authorization.
