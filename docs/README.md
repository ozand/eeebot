# eeebot Docs Index

Two principles govern this set: **simplicity** (smallest accurate model) and
**transparency/observability** (every claim maps to a durable artifact). See
[`CONSTITUTION.md`](../CONSTITUTION.md).

How the docs are organized:
- [`specs/`](specs/README.md) — **current product truth**, one normative spec per capability.
- [`changes/`](changes/README.md) — **changes in flight** (proposal/design), archived on merge.
- **reference docs** (below) — explanation and runbooks, not contract.
- `.legacy/` (archived/superseded docs) was removed 2026-07-05 (#613); recoverable
  via `git log -- .legacy`.

## Capability specs (start here)

The contract layer — what is true now, per capability:

- [`specs/self-evolving-runtime`](specs/self-evolving-runtime/spec.md) — the bounded cycle, HADI, outcomes, subagent directive.
- [`specs/subagent-bridge`](specs/subagent-bridge/spec.md) — cycle-branch isolation → import-smoke → integrate-to-main.
- [`specs/promotion-and-release`](specs/promotion-and-release/spec.md) — host→candidate→canonical→release, rollback, provenance, drift.
- [`specs/host-runtime`](specs/host-runtime/spec.md) — eeepc state authority, approval gate, capability policy, deploy.
- [`specs/observability`](specs/observability/spec.md) — the operator-question contract + cycle-health.
- [`specs/model-routing`](specs/model-routing/spec.md) — task-type routing; executor = `un/qwen3.6-27b-mtp`.
- [`specs/data-contracts`](specs/data-contracts/spec.md) — canonical schema/provenance types.
- [`specs/chat-agent-framework`](specs/chat-agent-framework/spec.md) — pluggable channels, host↔bot comms.
- [`specs/migration`](specs/migration/spec.md) — nanobot→eeebot rename guardrails (in progress).

## Reference docs (explanation)

Detailed walkthroughs behind the specs:

- [SYSTEM_OPERATION_REFERENCE.md](SYSTEM_OPERATION_REFERENCE.md) — end-to-end mechanics (roles, timers, cycle, artifacts, bridge, models, diagnostics).
- [OBSERVABILITY.md](OBSERVABILITY.md) — how to see what the system is doing.
- [ARCHITECTURE.md](ARCHITECTURE.md) — high-level map.
- [ACTIVE_GOAL.md](ACTIVE_GOAL.md) — current goal and progress criteria.
- Self-evolving detail: [EEEBOT_SELF_IMPROVING_RUNTIME_OPERATING_CONTRACT.md](EEEBOT_SELF_IMPROVING_RUNTIME_OPERATING_CONTRACT.md), [EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE.md](EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE.md), [EEEBOT_BUDGET_AND_REWARD_MODEL.md](EEEBOT_BUDGET_AND_REWARD_MODEL.md), [EEEBOT_EXPERIMENT_AND_OUTCOME_CONTRACT.md](EEEBOT_EXPERIMENT_AND_OUTCOME_CONTRACT.md).

## Runbooks (operational how-to)

- [EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md](EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md) — safe deploy/verify/rollback steps.
- [EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md](EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md) — opening the apply approval window.
- [CHANNEL_PLUGIN_GUIDE.md](CHANNEL_PLUGIN_GUIDE.md) — how to add a chat-channel plugin.
- [EEEBOT_OPERATOR_WORKFLOW.md](EEEBOT_OPERATOR_WORKFLOW.md) — operator responsibilities.

## Charter, roadmap & process

- [PROJECT_CHARTER.md](PROJECT_CHARTER.md) · [ROADMAP_EPICS.md](ROADMAP_EPICS.md) · [PLATFORM_EVOLUTION_IDEAS.md](PLATFORM_EVOLUTION_IDEAS.md)
- [MAINTAINER_OPERATING_MODEL.md](MAINTAINER_OPERATING_MODEL.md) · [OWNER_EXPERIENCE_TRACK.md](OWNER_EXPERIENCE_TRACK.md) · [PROCESS_NOTE_LIGHTWEIGHT_DDTR.md](PROCESS_NOTE_LIGHTWEIGHT_DDTR.md)
- [LAUNCH_CRITERIA_AND_REGRESSION_PROBES.md](LAUNCH_CRITERIA_AND_REGRESSION_PROBES.md) · [IDENTITY_ACCESS_ROLLOUT.md](IDENTITY_ACCESS_ROLLOUT.md)
- [userstory/](userstory/README.md) — user stories for active/upcoming work.

## Reading order for a new operator

1. `specs/self-evolving-runtime/spec.md` → `specs/subagent-bridge/spec.md`
2. `SYSTEM_OPERATION_REFERENCE.md` · `OBSERVABILITY.md`
3. `specs/host-runtime/spec.md` · `EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md`

> Tasks/backlog are in **[GitHub Project #7](https://github.com/users/ozand/projects/7)** + Issues, not in this repo. See [`AGENTS.md`](../AGENTS.md).
