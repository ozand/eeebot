# Planning, demand, and proposals

The bridge separates candidate supply from the planner's choice of the increment executed in a cycle.

## Candidate sources

Demand and proposer requests are candidate inputs, not assignments to the executor.

[[nanobot/runtime/demand.py#collect_demand]] assembles eligible demand in trust order from operator priorities, defects, goal gaps, skills, hypotheses, decay, and reflection. Stable demand IDs and completion/exhaustion evidence let the runtime fold, suppress, or re-present items without treating every ledger row as a new task. The proposer exposes live requests through [[nanobot/runtime/llm_proposer.py#proposer_candidate_items]] and can author bounded candidates through [[nanobot/runtime/llm_proposer.py#maybe_propose]]. These inputs do not themselves authorize execution.

## Planner choice

The planner chooses or declines candidate work and supplies the executor's task.

[[nanobot/runtime/bridge.py#_run_planning_session]] receives demand and proposer candidates as context and returns a plan or an explicit no-plan/rest outcome. It resolves the telemetry root once from the planner's state directory using the existing resolver precedence and passes it to the manager; legacy [[nanobot/agent/subagent.py#_subagent_telemetry_context]] callers may omit attempt/root arguments, but then do not gain synthetic attempt attribution. When a plan exists, its plan text becomes the executor task; no-plan/rest does not fall back to assigned proposer text. An interrupted increment is persisted for the planner to resolve through [[nanobot/runtime/open_increment.py#resolve]], rather than automatically re-running the prior task. See [[tests/test_agent_chooses.py#test_planner_runs_first_and_its_plan_is_the_task]], [[tests/test_agent_chooses.py#test_no_plan_does_not_fall_back_to_assignment]], and [[tests/test_agent_chooses.py#test_supply_interruption_carries_open_increment]].

### Planner tool capabilities

The bridge supplies the planner's exact read/list tool profile from one generated capability declaration; it does not sandbox filesystem access.

[[nanobot/runtime/bridge.py#_run_planning_session]] explicitly chooses `planner` for the [[nanobot/agent/subagent.py#SubagentManager]] profile. The immutable [[nanobot/agent/tools/toolsets.py#PLANNER_PROFILE]] registers exactly `read_file` and `list_dir`, with read/list operations and generated allowed-actions text inserted at the single `{{TOOL_CAPABILITIES}}` marker in [[roles/planner.md]]. The profile preserves existing path resolution: workspace-relative paths resolve under the workspace and absolute paths remain accepted outside it. This is an application tool boundary, not OS-level isolation. `search_memory` is excluded because its current implementation may create/update the runtime SQLite index; the planner reads `memory/index.md` directly. The executor compatibility profile remains the default and unchanged. Focused evidence is [[tests/test_planner_tool_profile.py#test_planner_registry_forbids_mutating_tools_without_side_effects]], [[tests/test_planner_tool_profile.py#test_planner_read_and_list_use_preserved_path_policy]], and [[tests/test_subagent_manager.py#test_executor_declared_and_registered_tool_names_match]]. ADR-044 is Accepted for this capability contract.

## Planner phase budget (#2050 H3)

Time-based planner finalization is behind a dedicated explicit source opt-in and remains OFF by default.

[[nanobot/runtime/session_clock.py#planner_phase_budget_enabled]] accepts only exact `true` or `false` in `SELFEVO_PLANNER_PHASE_BUDGET_ENABLED`; enabled mode requires explicit total wall `NANOBOT_SUBAGENT_WALL_SECS=9000`. [[nanobot/runtime/bridge.py#_run_planning_session]] partitions the bridge-start deadline into P=6000s planner and E=3000s executor/gate time; E includes F=300s once. The local final-call opportunity uses configured C; the separate shared guard remains C+F. Invalid or incompatible settings fail before planner spawn. Default-off preserves main's 600s timeout, iteration final turn, and legacy wall setting semantics. The flag is not host approval; no release/unit enables it and no deployment capacity is claimed. See [[nanobot/agent/subagent.py#SubagentManager]] and ADR-045 (Accepted for the narrow source contract; not host approval or rollout).

## Current behavior and decision status

Planner selection and proposer candidate authoring are separate in current code.

The old proposer rotation and planner selection are distinct concepts; do not infer that candidate authoring chooses the cycle's work. ADR-035 describes planner-first design but remains **Proposed**; ADR-044 is **Accepted** for the narrow planner-capability contract only. ADR-045 is **Accepted** for the narrow repository-source planner budget contract after its contract tests merged and passed CI; this does not establish host/service approval, activation, rollout, whole-#2050 acceptance, or #2036 completion. #2036 remains open for time-based finalization and forced-proposal pressure review. This page records checked-in behavior and does not accept ADR-035 or close #2036. See [[architecture#Decision-record navigation]].
