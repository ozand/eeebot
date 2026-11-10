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

The entry point is [[nanobot/runtime/bridge.py#main]]. A cycle collects demand and proposer candidates before planner selection; a no-plan/rest result does not start the executor. Planned work proceeds through isolated subagent execution, gates, and evidence recording. The bridge chooses whether an accepted result can be integrated; executor output is not itself authority to change the product repository.

The planning session is implemented by [[nanobot/runtime/bridge.py#_run_planning_session]]. It receives demand candidates collected by [[nanobot/runtime/demand.py#collect_demand]] and proposer candidates from [[nanobot/runtime/llm_proposer.py#proposer_candidate_items]], then returns a plan or an explicit no-plan/rest outcome. The executor task is derived from the planner's plan; a candidate is optional context, not a task assignment. Do not infer a no-demand idle rule from older architecture prose without checking current code and configuration. See [[planning]] for the candidate/selection boundary.

[[OPERATING.md]] exempts an executor-discovered already-done or inapplicable skip from the closing diary line and further tool calls; other executor outcomes retain the diary obligation. Before executor start, the harness attempts an opening entry that records the task, not the later outcome. The status-aware instruction test [[tests/test_operating_md.py#test_executor_skip_diary_exception]] checks both contract branches with positive and negative fixtures; ADR-053 is the provisional decision in this candidate.

The LLM proposer can author bounded candidates through [[nanobot/runtime/llm_proposer.py#maybe_propose]]. It is a candidate/request source, not the planner. Planner selection and proposer authoring are related but distinct steps, and an idle, failed, rested, blocked, or unplanned cycle is not an executed task.

The public derived-view producer serializes goal-review-owned derived-priority rows with explicit `self-derived` provenance; operator priority wording remains private and the external typed dashboard allowlist is a read-only consumer. See [[nanobot/runtime/demand.py#build_derived_view]], [[docs/specs/subagent-bridge/spec.md]], and [[docs/adr/ADR-048-persist-self-derived-provenance-in-public-projection.md]] (Proposed).

## Evidence, gates, and authority

Cycle and phase evidence is append-oriented in the runtime ledger at [[nanobot/runtime/cycle_ledger.py#append_event]]. Demand collection and ranking provide inputs; they do not establish that work passed a gate or integrated.

Selected prompt producers also attach bounded actual-call section provenance to existing LLM telemetry. It is process-local comparison evidence, not a trust anchor or proof of provider receipt. See [[docs/specs/observability/spec.md]].

The existing `cycle-health` summary reports warning or UNKNOWN from exact keyed start/outcome ledger evidence. Empty windows, malformed JSON field types or empty identifiers, future-dated/naive timestamps, byte-capped reads, missing active ledger with archive-only evidence, expired history, and incomplete reset coverage are UNKNOWN; valid unrelated phases are ignored without weakening malformed success-shaped-row checks. Because the ledger has no global inception/completeness watermark, a retained window cannot prove no older task keys were pruned, so below-threshold aggregate status remains UNKNOWN. Strict H5 ledger reads reject invalid UTF-8 rather than replacing corrupted key bytes; valid Unicode U+FFFD remains accepted. Same-key keep/resume attempts may repeat a cycle ID and each started row counts; a later duplicate skip for that reused ID is ambiguous even with a supplied matching key and produces UNKNOWN. Duplicate-skip subtraction otherwise requires an exact unique cycle/key join; mismatched, unjoined, or repeated joins produce UNKNOWN. See [[evidence]] for evidence status and fitness boundaries.

A subagent works in a cycle-scoped branch through [[nanobot/runtime/bridge.py#_setup_cycle_branch]]. The bridge applies the bounded smoke and mutation-surface checks before integration. Runtime-slice changes are promotion candidates for operator review rather than ordinary script-tier auto-integration; see [[gates-and-promotion]], [[docs/specs/subagent-bridge/spec.md]], and [[docs/specs/promotion-and-release/spec.md]].

The fitness boundary is harness-owned: scorecard and held-out verification are product-side evidence, outside the instance's writable workspace. The instance cannot define or edit its own fitness checkers. See [[nanobot/runtime/scorecard.py#compute_scorecard]], [[docs/specs/self-evolving-runtime/spec.md]], and [[docs/specs/promotion-and-release/spec.md]].

## State and deployment boundaries

Code, mutable instance workspace, runtime state, and product release are separate authority domains.

The self-evolving instance workspace is where bounded cycle changes are staged and integrated. Runtime state is held separately under the configured state root; it is evidence and operational state, not a second source-code repository. Product releases are selected through the host release mechanism and verified separately from repository CI. This page makes no claim about current host installation or health. See [[host-boundaries]], [[docs/specs/host-runtime/spec.md]], [[docs/specs/subagent-bridge/spec.md]], and [[docs/CURRENT_ARCHITECTURE.md]].

## Decision-record navigation

Decision records preserve rationale and their own acceptance status.

ADR-035 is marked **Proposed** in [[docs/adr/ADR-035-the-planning-session-chooses-and-closes-hadi.md]] and the [[docs/adr/README.md]] index, although planner-first behavior is present in current code; this page describes observed implementation and does not accept the ADR. [[docs/adr/ADR-037-record-handled-disposition-without-implying-cause.md|ADR-037, “Record handled-marker disposition without implying interruption cause,”]] has a delegated technical acceptance candidate under #2076; its exact revision awaits independent review before publication. This is not personal operator acceptance.

## Source precedence

Use executable code for present behavior; capability specs and ADRs state intended contracts and decision status.

When this overview, older architecture prose, an ADR, and code disagree, use current executable code and configuration to describe present behavior; capability specs define intended product contracts, and ADR status remains explicit. This overview is a navigation aid, not a replacement for those sources.
