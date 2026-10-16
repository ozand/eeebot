# Architecture and domain overview

This page maps the implemented eeebot product, its bounded self-evolving runtime, and the principal authority boundaries.

## Product identity and package layout

`eeebot` is the product and public identity. The implementation and internal imports use `nanobot.*`; `eeebot/` is the external compatibility layer. Both `nanobot` and `eeebot` CLI entry points are intentional package contracts. See [[README.md]], [[pyproject.toml]], [[nanobot/cli/commands.py#main]], and [[nanobot/cli/eeebot.py#main]].

The repository also contains the constrained self-evolving runtime and host release tooling; it is not merely the chat-agent framework. Capability truth is organized under [[docs/specs/README.md]]. The separate operations dashboard repository is not the canonical product runtime.

## Two runtime surfaces

The package supports an operator-facing chat framework and a separate bounded host runtime.

### Operator-facing chat framework

The chat framework exposes channels, tools, model routing, and user sessions through the CLI and agent package. It is distinct from the timer-driven host runtime. Shared package code does not make the two execution authorities interchangeable.

### Bounded self-evolving bridge

The host bridge runs a bounded cycle through planning, subagent execution, gates, and evidence recording.

The entry point is [[nanobot/runtime/bridge.py#main]]. A cycle performs bounded planning, request selection, isolated subagent execution, gate evaluation, and evidence recording. The bridge chooses whether an accepted result can be integrated; executor output is not itself authority to change the product repository.

The planning session is implemented by [[nanobot/runtime/bridge.py#_run_planning_session]]. It receives demand candidates collected by [[nanobot/runtime/demand.py#collect_demand]] and proposer candidates from [[nanobot/runtime/llm_proposer.py#proposer_candidate_items]], then returns a plan or an explicit no-plan/rest outcome. The executor task is derived from the planner's plan; a candidate is optional context, not a task assignment. Do not infer a no-demand idle rule from older architecture prose without checking current code and configuration.

The LLM proposer can author bounded candidates through [[nanobot/runtime/llm_proposer.py#maybe_propose]]. It is a candidate/request source, not the planner. Planner selection and proposer authoring are related but distinct steps, and an idle, failed, rested, blocked, or unplanned cycle is not an executed task.

## Evidence, gates, and authority

Cycle and phase evidence is append-oriented in the runtime ledger at [[nanobot/runtime/cycle_ledger.py#append_event]]. Demand collection and ranking provide inputs; they do not establish that work passed a gate or integrated.

A subagent works in a cycle-scoped branch through [[nanobot/runtime/bridge.py#_setup_cycle_branch]]. The bridge applies the bounded smoke and mutation-surface checks before integration. Runtime-slice changes are promotion candidates for operator review rather than ordinary script-tier auto-integration; see [[docs/specs/subagent-bridge/spec.md]] and [[docs/specs/promotion-and-release/spec.md]].

The fitness boundary is harness-owned: scorecard and held-out verification are product-side evidence, outside the instance's writable workspace. The instance cannot define or edit its own fitness checkers. See [[nanobot/runtime/scorecard.py#compute_scorecard]], [[docs/specs/self-evolving-runtime/spec.md]], and [[docs/specs/promotion-and-release/spec.md]].

## State and deployment boundaries

Code, mutable instance workspace, runtime state, and product release are separate authority domains.

The self-evolving instance workspace is where bounded cycle changes are staged and integrated. Runtime state is held separately under the configured state root; it is evidence and operational state, not a second source-code repository. Product releases are selected through the host release mechanism and verified separately from repository CI. This page makes no claim about current host installation or health. See [[docs/specs/host-runtime/spec.md]], [[docs/specs/subagent-bridge/spec.md]], and [[docs/CURRENT_ARCHITECTURE.md]].

## Decision-record navigation

Decision records preserve rationale and their own acceptance status.

ADR-035 is marked **Proposed** in [[docs/adr/ADR-035-the-planning-session-chooses-and-closes-hadi.md]] and the [[docs/adr/README.md]] index, although planner-first behavior is present in current code; this page describes observed implementation and does not accept the ADR. ADR-037 is also Proposed. Do not infer acceptance from implementation, tests, or inclusion in this map.

## Source precedence

Use executable code for present behavior; capability specs and ADRs state intended contracts and decision status.

When this overview, older architecture prose, an ADR, and code disagree, use current executable code and configuration to describe present behavior; capability specs define intended product contracts, and ADR status remains explicit. This overview is a navigation aid, not a replacement for those sources.
