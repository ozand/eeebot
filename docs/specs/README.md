# Specs — current product truth

Each **capability** of eeebot has exactly one living spec: `docs/specs/<capability>/spec.md`.
A spec describes what is **true now** — not how it was built, not what changed.
Changes-in-flight live in [`docs/changes/`](../changes/README.md); when a change
lands, its delta is applied here.

See [`CONSTITUTION.md`](../../CONSTITUTION.md) principle 3 (current truth vs
changes) and principle 2 (one source of truth).

## Spec format

Every `spec.md` follows the same shape:

```md
# <Capability> — spec

_Status: current. Last updated: YYYY-MM-DD._

## Purpose
One paragraph: what this capability is and the value it provides.

## Requirements
Normative statements using SHALL. Each is checkable and maps to a test,
a gate, or an observable artifact.
- R1. The system SHALL ...
- R2. ... SHALL NOT ...

## Scenarios
Concrete behavior in Given / When / Then form.
### Scenario: <name>
- Given <state>
- When <event>
- Then <observable outcome>

## References
Pointers to the explanatory/reference docs and the code that implements this.
```

Keep specs **small and normative**. Detailed walkthroughs stay in the reference
docs (`ARCHITECTURE.md`, `SYSTEM_OPERATION_REFERENCE.md`, `OBSERVABILITY.md`); the
spec is the contract, the reference doc is the explanation.

## Capability map (consolidated)

All nine capability specs exist. The table records what each consolidated and
where its sources went.

| Capability | Source docs folded (removed 2026-07-05, #613; recoverable from git history) | Kept as reference / runbook |
|---|---|---|
| `self-evolving-runtime` | runtime-subagent directive (from AGENTS.md) | EEEBOT_SELF_IMPROVING_RUNTIME_OPERATING_CONTRACT, EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE, EEEBOT_BUDGET_AND_REWARD_MODEL, EEEBOT_EXPERIMENT_AND_OUTCOME_CONTRACT (detail behind the spec) |
| `subagent-bridge` | — | SYSTEM_OPERATION_REFERENCE §6, `nanobot/runtime/bridge.py` (authoritative) |
| `promotion-and-release` | SOURCE_OF_TRUTH_AND_PROMOTION_POLICY, PROMOTION_GATE_SPEC, RELEASE_ARTIFACT_AND_ROLLBACK_CONTRACT, BRANCH_AND_RELEASE_CHANNEL_POLICY, CHANGE_PROPAGATION_MODEL, VERSION_AND_PROVENANCE_MODEL, DRIFT_BUDGET_AND_RECONCILIATION_POLICY, VALIDATION_HOOKS_PLAN, DEPLOY_DECISION_MATRIX | — |
| `host-runtime` | EEEPC_AGENT_RUNTIME_INSTRUCTIONS, EEEPC_RUNTIME_STATE_AUTHORITY_USAGE, HOST_CAPABILITY_POLICY, HOST_GITHUB_SYNC_ARCHITECTURE, HOST_WORKSPACE_ARTIFACT_TRIAGE, SAFE_BOOTSTRAP_FROM_SCRATCH, BASE_CONFIGURATION_PROFILE | EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK, EEEPC_APPLY_OK_OPERATOR_RUNBOOK (runbooks) |
| `model-routing` | MODEL_ROUTING_FALLBACK_V1 | — |
| `observability` | — | OBSERVABILITY.md (reference) |
| `chat-agent-framework` | HOST_BOT_COMMUNICATION | CHANNEL_PLUGIN_GUIDE (how-to) |
| `data-contracts` | SCHEMA_REGISTRY | — |
| `migration` | EEEBOT_INTERNAL_RENAME_MIGRATION_PLAN, EEEBOT_PHASE2_RENAME_MATRIX, EEEBOT_MIGRATION_STATUS_AND_PROOF, EEEBOT_DUAL_IMPORT_SUPPORT_PROOF, EEEBOT_CANONICAL_REPOSITORY_AND_DASHBOARD_CONSOLIDATION | EEEBOT_INTERNAL_RENAME_INVENTORY.json (data) |

Reference docs that stay as-is (explanation, not contract): `ARCHITECTURE.md`,
`SYSTEM_OPERATION_REFERENCE.md`, `OBSERVABILITY.md`, `PROJECT_CHARTER.md`,
`ROADMAP_EPICS.md`.
