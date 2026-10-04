---
title: Execute context metadata exporters only from an approved authority closure
status: proposed
date: 2026-10-04
authors: [eeebot maintainers]
supersedes: ADR-037 upon acceptance
related: ["#2058", "ADR-037", "docs/specs/promotion-and-release/spec.md"]
tags: [security, context, provenance, release]
---

# Status

Proposed 2026-10-04 under #2058. ADR-037 remains accepted and unchanged until an operator explicitly accepts this ADR. On acceptance, this ADR supersedes ADR-037; it does not retroactively alter the prior record. This proposal authorizes no implementation, host access, or deployment before that acceptance.

# Context

PR #2059 review finding #4176489504 established that `deploy_release.sh` executes the metadata exporter from the selected commit on the developer workstation. An explicit `--ref` can name code that is not trusted; that code and its imported modules would run with the invoking user's local privileges before the remote least-privilege validation boundary. Clearing environment variables or Python isolated mode is not a sandbox.

ADR-037 requires metadata from the exact selected commit, including when it differs from checkout HEAD, while `docs/specs/promotion-and-release/spec.md` R10 permits explicitly-tested candidate branches. Rejecting every non-main ref would unnecessarily break that candidate workflow. The security boundary must therefore separate the trusted code that computes metadata from the target commit whose identity the artifact describes.

# Decision

1. **Separate implementation from production trust.** ADR-038 must be explicitly accepted by the operator before implementation begins. During implementation and tests, a clearly non-authoritative fixture anchor may exercise the code; it MUST NOT authorize production export or be treated as trusted. Production local export remains disabled until the completed implementation has independent security review and the operator explicitly approves an immutable, full 40-character anchor SHA in the canonical repository. Record that SHA and its approval decision/reference, date, and review evidence in operator-owned configuration outside the target tree. This proposal intentionally does not choose the production SHA. Rotation requires the same explicit review and approval, never automatic trust of HEAD, `main`, tags, or a selected target.
2. **Trusted code only.** The exporter and all its imports execute only from a private extraction of the approved anchor commit. The target ref's files are data for comparison only: do not import, execute, or add its tree to `PYTHONPATH`. The anchor implementation is constrained to a reviewed standard-library-only import closure; unexpected imports fail closed.
3. **Bounded authority manifest and comparison.** A reviewed, explicit manifest pinned to the approved anchor defines the closure; the target cannot supply or modify this manifest. It includes the trusted exporter and every transitive local module it executes (initially `scripts/export_context_metadata.py`, `nanobot/runtime/context_rules.py`, `context_metadata.py`, `mutation_policy.py`, and `operator_documents.py`, subject to implementation review), plus the specific `ContextBuilder` rule/order/assembly authority used to justify exported claims (initially the relevant definitions in `nanobot/agent/context.py`). This is not a claim that the entire operational import graph of `ContextBuilder` is exporter code or must execute. Before any Python process is launched, compare each manifest entry's Git tree entry type and mode and its blob object ID at target and anchor; missing, symlink/type/mode-changed, or differing entries fail closed. Reject unexpected imports in the anchored exporter's reviewed execution closure; do not claim generic static discovery can prove arbitrary Python dependency closure. Independent review must finalize and test both the executable closure and the narrower compared-claim authority. No hand-written duplicate constants or AST evaluation is permitted.
4. **Target binding and candidate compatibility.** When and only when the complete closure matches, run the trusted anchor exporter with the selected target's full commit SHA as the metadata label. Thus unrelated candidate changes remain verifiable, including target != HEAD, without executing candidate code locally. A changed authority closure is unsupported for this exporter and produces no metadata; candidate activation/verification fails closed rather than claiming trusted provenance. This does not replace the promotion-and-release R10 policy with a main-only restriction.
5. **Legacy behavior and remote boundary.** A target with no exporter remains diagnostic-only/unavailable as already scoped; never fall back to executing its exporter. Candidate deployment/verify-only validation retains existing no-activation-on-failure and rollback guarantees. This ADR does not move exporter execution to the host, create a general sandbox, authenticate arbitrary artifacts cryptographically, or authorize host operations.
6. **Unchanged metadata contract.** Preserve ADR-037's bounded descriptor/privacy contract, shared canonical rule ownership, exact target SHA label, remote pre-activation validation, and verify-only semantics except where this ADR explicitly narrows which code may produce the artifact. No prompt/source content, secrets, or historical cycle payloads are exported.

# Consequences

## What gets easier

After formal ADR acceptance, implementation can proceed using only a non-authoritative test fixture. Once the finished implementation has independent review and an operator-approved production anchor, authority-identical candidate refs can retain exact-target provenance without running their code on a credential-bearing workstation. The trust anchor and authority comparison become auditable and testable.

## What gets harder

Production enablement follows, rather than precedes, implementation and independent review; the test fixture can never enable production export. Every exporter or rule-authority change requires a newly reviewed/approved anchor before metadata can be produced from it. The executable import closure and compared-claim authority manifest need maintenance; omissions would undermine the guarantee, so independent review and adversarial tests are mandatory.

## What does not change

The target full SHA remains the metadata's source identity; non-HEAD candidate verification remains supported when authority blobs match; R10 still governs deployable branch classes; remote activation, rollback, privacy, dashboard, and host-operation boundaries remain unchanged.

# Alternatives Considered

- **Execute target exporter locally:** rejected because arbitrary target code can access workstation credentials and files.
- **Clear environment / use Python `-I` or `-S`:** rejected; import isolation and environment cleanup are not process/filesystem/network sandboxes.
- **Require main-only or trusted-branch targets:** rejected because it changes approved explicit-candidate verification and is narrower than R10; trust in a branch name is also weaker than pinning reviewed code.
- **Run candidate exporter on the host:** rejected; moves untrusted execution across a different boundary and is not authorized by this change.
- **Duplicate constants, parse Python AST, or build a generic sandbox:** rejected because these either create drift/unsafe interpretation or require unproven cross-platform isolation infrastructure.

# Test Contract

| Claim | Test | Status |
|---|---|---|
| Target Python is never imported/executed, including adversarial exporter/import sentinels | `tests/test_deploy_release.py` (ADR-038 citation) | deferred (#2058) |
| Every authority-closure member is deterministically enumerated and blob/mode/type mismatch fails before process launch | `tests/test_context_metadata_export.py` (ADR-038 citation) | deferred (#2058) |
| Same-closure non-HEAD candidate is labeled with exact target SHA using anchor code | `tests/test_deploy_release.py` (ADR-038 citation) | deferred (#2058) |
| Unknown transitive imports/authority files fail closed; legacy no-exporter remains non-executing/unavailable | `tests/test_context_metadata_export.py` (ADR-038 citation) | deferred (#2058) |
| Verify-only candidate path validates without activation; invalid closure preserves prior current release | `tests/test_deploy_verify_only_remote_execution.py` (ADR-038 citation) | deferred (#2058) |
| Anchor provenance and approval/rotation cannot silently follow mutable refs | `tests/test_context_metadata_export.py` (ADR-038 citation) | deferred (#2058) |

# References

- #2058 — implementation and security prerequisite; anchor selection/bootstrap/rotation and acceptance remain pending operator decision.
- PR #2059 finding #4176489504 — selected-ref exporter local execution.
- ADR-037 — accepted metadata contract, superseded only upon acceptance of this ADR.
- `docs/specs/promotion-and-release/spec.md` R10 — approved stable, explicitly-tested candidate, and immutable release inputs.
- `host/eeepc/scripts/deploy_release.sh` — selected target, packaging, verify-only and activation flow.
