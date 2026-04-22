# Weak Host Bootstrap Drill

Status: bounded bootstrap proof artifact for issue #44. This drill is intentionally proof-oriented and read-only; it does not mutate the live host.

## Purpose

Prove that a similar weak host can be evaluated and prepared for bootstrap using only documented, low-risk steps before any risky package/service mutation occurs.

## Covered bootstrap stages

This drill covers only Stage 0–2 of the documented bootstrap path:
- Stage 0: host qualification
- Stage 1: canonical source and baseline configuration selection
- Stage 2: minimal trusted runtime/control surfaces

## Explicit exclusions

This drill does NOT include:
- package installation on the target host
- service restart/migration
- writes to live authority roots
- promotion/rollback activation
- broad self-modification

## Read-only proof checklist

1. Host class is documented
- source: `docs/SAFE_BOOTSTRAP_FROM_SCRATCH.md`
- source: `docs/BASE_CONFIGURATION_PROFILE.md`

2. Canonical source of truth is documented
- source: `README.md`
- source: `docs/PROJECT_CHARTER.md`

3. Minimal runtime/control surfaces are documented
- source: `docs/SAFE_BOOTSTRAP_FROM_SCRATCH.md`
- source: `docs/userstory/WEAK_HOST_RUNTIME_CONVERGENCE_PROOF.md`

4. Rollback / restart / operator gating remain defined
- source: `docs/EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md`
- source: `docs/EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md`

## Current bounded conclusion

A weak-host bootstrap drill can be performed safely as a read-only qualification/proof exercise.

The remaining blocker for a truly clean weak-host bootstrap is not host-class uncertainty itself.
It is the packaging/runtime dependency path around optional MCP/OpenSpace support on the weakest hosts.

That blocker should be handled as a separate packaging/runtime slice, not hidden inside the bootstrap drill.

## Next explicit blocker

- MCP / OpenSpace dependency path on weak hosts
- reference: `docs/MCP_OPENSPACE_HANDSHAKE_NOTE.md`
- reference: `docs/userstory/OPENSPACE_LOCAL_ONLY_SIDECAR_FIT_AUDIT.md`

## Closure rule for this bounded slice

This drill is sufficient when:
- the bootstrap path is documented,
- the proof remains read-only,
- the remaining blocker is isolated explicitly,
- and operators can distinguish documentation/proof readiness from package/runtime readiness.
