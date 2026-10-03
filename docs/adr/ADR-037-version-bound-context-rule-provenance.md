---
title: Bind current context rule metadata to the exact runtime release
status: proposed
date: 2026-10-03
authors: [eeebot maintainers]
related: ["#2058", "ozand/eeebot-ops-dashboard#386", "ADR-022", "ADR-034", "ADR-036"]
tags: [context, provenance, release, dashboard]
---

# Status

**Proposed** under #2058. The parent architect approved the direction, but that review is advisory and is not formal ADR acceptance. Per `docs/adr/README.md`, only the operator may accept this record by a commit that flips its status after every test-contract item exists or is explicitly deferred. Until that operator commit, this ADR remains proposed and implementation remains blocked. Dashboard #386 remains its consumer; no host access or deployment is authorized here.

# Context

The runtime `ContextBuilder` in `nanobot/agent/context.py` owns context ordering, caps, floors, and conditional assembly. The dashboard needs current builder rules independently of cycle telemetry (#386), while per-cycle `system_prompt` rows remain observations and #316 owns exact historical payloads. The runtime deployment records a full `SOURCE_COMMIT` inside a candidate release and switches `current` only after candidate setup. The inspected deploy script selects `COMMIT` (HEAD or explicit `--ref`) and archives that exact commit with `git archive`; its developer checkout HEAD is not necessarily the target. The legacy `host/eeepc/scripts/install.sh` path creates `current` without a verified `SOURCE_COMMIT`, so it cannot establish version provenance.

The prior source audit is recorded at `docs/changes/context-source-provenance/proposal.md` and dashboard issue #386 comment [5972252089](https://github.com/ozand/eeebot-ops-dashboard/issues/386#issuecomment-5972252089). Runtime source inspected at current `origin/main` `c7e180d7122605082afc561225051e9a2c26f7f2`; `nanobot/agent/context.py` owns `_RELEASE_BLOCK_NAMES`, `_RELEASE_POOL_CHARS`, `_RELEASE_BLOCK_FLOORS`, workspace/memory/runtime/scorecard/position limits, `BOOTSTRAP_FILES`, `MAX_SYSTEM_PROMPT_CHARS`, `SYSTEM_PROMPT_CAP_ENV`, `_load_ontology_blocks()`, `_build_loop_system_prompt()`, `_cap()` and `_fit_system_prompt()`. `PRIORITIES_BLOCK_CAP` is owned by `nanobot.runtime.operator_documents` and imported by the builder. The whole `ContextBuilder` module has nontrivial imports (`loguru`, memory, skills and runtime helpers), so it is unsuitable as a supposedly lightweight exporter import path. `operator_documents` uses standard-library imports and owns the priorities cap.

#2058 requires versioned provenance, reuse of existing definitions, exact commit binding, descriptor-only data and fail-closed mismatch behavior. The dashboard's `GENERATOR_SHA` identifies dashboard code, not runtime source. No current deployed rules artifact was found. Evidence establishes source/deploy mechanisms, not the state of any live host.

# Problem statement

Define a single-owner, bounded way to expose current builder-rule metadata for the exact deployed runtime release, independently of cycle telemetry, without exposing prompt/source text or silently accepting metadata from another revision.

# Decision

1. **Canonical rule ownership.** Move only static declarative rule fields currently owned directly by `ContextBuilder` into a small standard-library-only runtime rules module. `ContextBuilder` and the metadata exporter consume those same definitions. Keep `PRIORITIES_BLOCK_CAP` owned by `nanobot.runtime.operator_documents`; the exporter imports it directly. Do not move that ownership, copy constants into a second table, or import `ContextBuilder` from the exporter. Preserve behavior with tests.
2. **Exact-target export.** During deployment packaging, export a bounded deterministic artifact from the exact selected target commit (`COMMIT`), not ambient checkout `HEAD`. Supply the full resolved commit SHA explicitly to the exporter; the source archive has no `.git` metadata. Include the artifact in that same candidate release archive. No remote CLI execution is needed.
3. **Pre-activation validation.** On the remote candidate, validate required schema/version and full embedded commit equality with the release `SOURCE_COMMIT` before switching `current`. Require bounded, typed allowlisted fields: known rule IDs, enums, booleans, and non-negative integer values within declared bounds; reject unknown fields, arbitrary paths, malformed, oversized, unsupported, or mismatched artifacts. This is provenance/schema validation within the trusted release archive and host deployment boundary; `SOURCE_COMMIT` alone does NOT cryptographically authenticate a modified artifact, and this ADR adds no signature/digest trust system. Any rejected artifact aborts candidate activation and retains the prior release. A candidate lacking the new exporter (for example, an older `--ref`) is unsupported and fails before activation; never generate from ambient HEAD or fall back to stale metadata. The verify-only candidate path exercises the same validator but never activates or mutates the live release.
4. **Bounded descriptor contract.** Export only approved rule identifiers, order, owner/root category, safe inclusion/conditional labels, and static builder values with clear classification. Include the exact full source commit. Do not export prompt/source content, secrets, raw environment values, per-cycle results, or current file contents.
5. **Dynamic facts remain distinct.** An immutable artifact may state the default total cap and override variable name, but not claim the effective cap when `NANOBOT_SYSTEM_PROMPT_MAX_CHARS` may override it. State release pool/order/floor, not content-dependent per-file effective capacities. Current approved input presence/readability is a separate read result. Eligibility/rule descriptions do not claim that current content was actually included. `system_prompt` ledger data stays a cycle/time-labeled observation; historical payloads remain exact-cycle #316 evidence.
6. **Unprovenanced compatibility paths fail closed.** A missing `SOURCE_COMMIT` (including legacy/install paths) or unavailable/invalid metadata means current rule provenance is unavailable. Never infer identity from dashboard SHA, repository HEAD, ledger rows, or historical prompt payloads. No host mutation or deployment is part of this ADR.

## What this is

A proposed release-bound provenance contract and single-source rule-definition seam, consumed as safe metadata by dashboard #386 or another authorized reader.

## What this is not

It is not runtime telemetry, an effective environment-config dump, a source/prompt disclosure, a new daemon/history store, a remote code-execution API, a dashboard implementation, or proof that an input was included in a past or current prompt.

## Success criteria

- Runtime builder and exporter use the same canonical static definitions; no duplicated rule values.
- Export from any selected commit is deterministic and reports that exact full SHA.
- The artifact is included in the archive for the selected target ref even when target differs from local HEAD.
- Both deployment and verify-only candidate paths reject missing/invalid/mismatched artifacts before any current-symlink flip; verify-only never activates. A rejected candidate leaves the prior `current` release untouched and available for normal rollback.
- Legacy/install releases without verifiable source identity render rule provenance unavailable.
- No prompt/source content or secrets enter the artifact; current input status, effective env overrides, observations, and historical payloads are not misrepresented as static rules.
- Existing prompt assembly behavior remains unchanged.

# Consequences

## What gets easier

The dashboard can display builder rules without importing the runtime or hand-copying numeric values. Current rules have exact release provenance; a missing or mismatched source contract is visible. Current input status and cycle observations can be rendered as separate claims.

## What gets harder

A small ownership refactor and deterministic exporter must remain aligned with the builder. Deployment packaging and verify-only tests must cover the target-ref/archive path, artifact validation, and rollback-before-activation. Legacy installs without identity remain unavailable until separately upgraded through governed deployment work. Effective environment overrides and dynamic per-file allocations require separately approved current-state reads if the product later needs those values.

## What does not change

Prompt assembly, runtime decision behavior, cycle telemetry, #316 historical payload ownership, dashboard/runtime import boundary, existing public/private redaction rules, deployment authority, and host state remain unchanged.

# Alternatives considered

## Parse runtime Python from the dashboard

Rejected: parser coupling to implementation syntax, uncertain exact source/deployment binding, and duplicated parser semantics; violates the dashboard/runtime boundary.

## Import all of `ContextBuilder` in an exporter

Rejected: its import graph pulls logging, memory, skill and runtime modules into what should be a bounded metadata operation. A stdlib rules module shared by builder and exporter is narrower.

## Keep constants in dashboard or metadata-only duplicate module

Rejected: a second rules table can drift and cannot prove it matches builder behavior. The shared rules module must be consumed by both.

## Generate from ambient checkout HEAD

Rejected: `deploy_release.sh --ref` archives the explicitly selected commit, which may differ from checkout HEAD. A metadata SHA could then describe another source than the release.

## Remote CLI execution on the host

Rejected: unnecessary remote execution surface. Candidate packaging already has the exact target commit and can carry the validated artifact to the host.

## Include effective environment/config and current input content in the artifact

Rejected: environment is runtime-specific and may contain sensitive values; input content may be private. Immutable source metadata cannot represent mutable process state or actual resolved content. Separate status-only reads may be considered under their own authorization/privacy rules.

## Do nothing

Rejected: leaves #386's current builder-rules acceptance criterion without an authoritative source and encourages stale observations or copied dashboard values to be mistaken for current configuration.

# Test Contract

| Claim | Test | Status |
|---|---|---|
| Builder and exporter share the same canonical static rule definitions without changing build behavior | `tests/test_context_rules.py` and existing `tests/test_context_builder.py` (ADR citation added with implementation) | deferred (#2058) |
| Export is deterministic and bound to supplied full target SHA | `tests/test_context_metadata_export.py` | deferred (#2058) |
| Deployment packages metadata from selected `COMMIT`, not ambient HEAD, and rejects SHA/schema mismatch before symlink activation | `tests/test_deploy_release.py` | deferred (#2058) |
| Verify-only validates candidate metadata without changing current release | `tests/test_deploy_verify_only_remote_execution.py` | deferred (#2058) |
| Artifact is bounded and contains no prompt/source text, secrets, mutable input content, or cycle observations | `tests/test_context_metadata_export.py` | deferred (#2058) |
| Missing provenance/metadata fails closed for legacy/install path | `tests/test_context_metadata_export.py` and dashboard #386 unavailable-state test | deferred (#2058) |

# Rollback and compatibility

The artifact is additive and release-local. If invalid, absent, unsupported by the selected source revision, or mismatched, candidate activation stops before the atomic `current` symlink change; the prior release remains current and available to the existing rollback mechanism. To roll back a landed producer, revert the rules/export/deploy changes and release the prior runtime, then revert the consumer. No historical records are written or migrated. Releases produced by legacy `install.sh` without a trustworthy `SOURCE_COMMIT` remain compatible with runtime operation but report provenance unavailable; no identity is synthesized. Any host deployment requires separate explicit rollout authorization.

# References

- #2058 — canonical runtime governance issue.
- [Dashboard #386](https://github.com/ozand/eeebot-ops-dashboard/issues/386) — consumer and acceptance context.
- `docs/changes/context-source-provenance/proposal.md` — bounded change proposal.
- `nanobot/agent/context.py` — `ContextBuilder` rule definitions and assembly.
- `nanobot/runtime/operator_documents.py` — canonical `PRIORITIES_BLOCK_CAP`.
- `nanobot/runtime/mutation_policy.py` — workspace read paths.
- `host/eeepc/scripts/deploy_release.sh` — target selection, archive, `SOURCE_COMMIT`, current-symlink activation/rollback, verify-only flow.
- `host/eeepc/scripts/install.sh` — legacy path that does not establish `SOURCE_COMMIT`.
- ADR-022 — context ontology ownership.
- ADR-034 — operator-document ownership/privacy.
- ADR-036 — dashboard/LAN public/private boundary.
