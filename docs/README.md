# eeebot Docs Index

Two principles govern this set: **simplicity** (smallest accurate model) and
**transparency/observability** (every claim maps to a durable artifact). See
[`CONSTITUTION.md`](../CONSTITUTION.md).

How the docs are organized:
- [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md) — **start here**: the
  one-page map of the live state-light proposer loop (#702–#708).
- [`specs/`](specs/README.md) — **current product truth**, one normative spec per capability.
- [`changes/`](changes/README.md) — **changes in flight** (proposal/design), archived on merge.
- **reference docs** (below) — explanation and runbooks, not contract.
- **historical / superseded** (bottom) — pre-July-2026 lane/HADI architecture docs, kept for history.
- `.legacy/` (archived/superseded docs) was removed 2026-07-05 (#613); recoverable
  via `git log -- .legacy`.

## Start here (current architecture)

Read these first, in order, for what is true **now**:

1. [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md) — the live loop end to end.
2. Current source docs behind it:
   [`changes/702-ledger-loop-architecture-decision`](changes/702-ledger-loop-architecture-decision/decision.md) (why the loop was redesigned),
   [`changes/704-ledger-artifact-memory`](changes/704-ledger-artifact-memory/design.md) (ledger memory),
   [`changes/760-demand-driven-proposer`](changes/760-demand-driven-proposer/proposal.md) (demand-driven proposer),
   [`changes/765-scorecard`](changes/765-scorecard/proposal.md) (fitness scorecard),
   [`changes/780-heldout-pack`](changes/780-heldout-pack/proposal.md) (held-out verification).
3. The normative capability specs in [`specs/`](specs/README.md).

## Capability specs

The contract layer — what is true now, per capability:

- [`specs/self-evolving-runtime`](specs/self-evolving-runtime/spec.md) — the bounded cycle, HADI, outcomes, subagent directive.
- [`specs/subagent-bridge`](specs/subagent-bridge/spec.md) — cycle-branch isolation → import-smoke → integrate-to-main.
- [`specs/promotion-and-release`](specs/promotion-and-release/spec.md) — host→candidate→canonical→release, rollback, provenance, drift.
- [`specs/host-runtime`](specs/host-runtime/spec.md) — eeepc state authority, approval gate, capability policy, deploy.
- [`specs/observability`](specs/observability/spec.md) — the operator-question contract + cycle-health.
- [`specs/model-routing`](specs/model-routing/spec.md) — task-type routing; executor = `un/qwen3.6-27b-mtp`.
- [`specs/data-contracts`](specs/data-contracts/spec.md) — canonical schema/provenance types.
- [`specs/chat-agent-framework`](specs/chat-agent-framework/spec.md) — pluggable channels, host↔bot comms.
- [`specs/migration`](specs/migration/spec.md) — final nanobot→eeebot naming state and permanent compatibility guardrails.

## Reference docs (explanation)

Detailed walkthroughs behind the specs:

- [SYSTEM_OPERATION_REFERENCE.md](SYSTEM_OPERATION_REFERENCE.md) — end-to-end mechanics (roles, timers, cycle, artifacts, bridge, models, diagnostics).
- [OBSERVABILITY.md](OBSERVABILITY.md) — how to see what the system is doing.
- [ARCHITECTURE.md](ARCHITECTURE.md) — high-level map.
- [ACTIVE_GOAL.md](ACTIVE_GOAL.md) — current goal and progress criteria.
- Self-evolving detail: [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md) and the current source docs listed under "Start here" above. (The older EEEBOT_SELF_IMPROVING_* / _BUDGET_AND_REWARD_ / _EXPERIMENT_AND_OUTCOME_ / _INSIGHT_HYPOTHESIS_ contract docs are superseded — see "Historical / superseded" below.)

## Runbooks (operational how-to)

- [EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md](EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md) — safe deploy/verify/rollback steps.
- [EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md](EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md) — opening the apply approval window.
- [CHANNEL_PLUGIN_GUIDE.md](CHANNEL_PLUGIN_GUIDE.md) — how to add a chat-channel plugin.

## Charter, roadmap & process

- [PROJECT_CHARTER.md](PROJECT_CHARTER.md) · [ROADMAP_EPICS.md](ROADMAP_EPICS.md) · [PLATFORM_EVOLUTION_IDEAS.md](PLATFORM_EVOLUTION_IDEAS.md)
- [MAINTAINER_OPERATING_MODEL.md](MAINTAINER_OPERATING_MODEL.md) · [OWNER_EXPERIENCE_TRACK.md](OWNER_EXPERIENCE_TRACK.md) · [PROCESS_NOTE_LIGHTWEIGHT_DDTR.md](PROCESS_NOTE_LIGHTWEIGHT_DDTR.md)
- [LAUNCH_CRITERIA_AND_REGRESSION_PROBES.md](LAUNCH_CRITERIA_AND_REGRESSION_PROBES.md) · [IDENTITY_ACCESS_ROLLOUT.md](IDENTITY_ACCESS_ROLLOUT.md)
- [userstory/](userstory/README.md) — user stories for active/upcoming work.

## Reading order for a new operator

1. [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md) — the live loop in one page.
2. `specs/self-evolving-runtime/spec.md` → `specs/subagent-bridge/spec.md`
3. `SYSTEM_OPERATION_REFERENCE.md` · `OBSERVABILITY.md`
4. `specs/host-runtime/spec.md` · `EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md`

> Tasks/backlog are in GitHub Issues + status labels, not in this repo. See [`AGENTS.md`](../AGENTS.md).

## Historical / superseded

These docs describe the **pre-July-2026 lane/HADI/reward architecture**, replaced
by the state-light proposer loop (#702–#708). Kept for history — each now carries
a SUPERSEDED banner pointing at [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md).
Do not treat them as current contract.

- [EEEBOT_SELF_IMPROVING_RUNTIME_OPERATING_CONTRACT.md](EEEBOT_SELF_IMPROVING_RUNTIME_OPERATING_CONTRACT.md)
- [EEEBOT_BUDGET_AND_REWARD_MODEL.md](EEEBOT_BUDGET_AND_REWARD_MODEL.md)
- [EEEBOT_EXPERIMENT_AND_OUTCOME_CONTRACT.md](EEEBOT_EXPERIMENT_AND_OUTCOME_CONTRACT.md)
- [EEEBOT_OPERATOR_WORKFLOW.md](EEEBOT_OPERATOR_WORKFLOW.md)
- [EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE.md](EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE.md)
