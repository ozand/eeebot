# Runtime contracts and limits

The bounded runtime turns candidate work into isolated, checked changes while keeping evidence, fitness, and release authority distinct.

## Cycle orchestration and planning

A planner-selected task, not a candidate request, is the executor assignment. The bridge coordinates demand/proposer input, planning, bounded execution, gates, and evidence; [[nanobot/runtime/bridge.py#_run_planning_session]] selects plan or rest, and [[nanobot/runtime/demand.py#collect_demand]] supplies candidates. See [[planning#Planner choice]] and [[tests/test_agent_chooses.py#test_planner_runs_first_and_its_plan_is_the_task]].

## Gate, mutation, and promotion boundaries

Cycle work is isolated and checked before integration; runtime-slice work follows a separate promotion path. [[nanobot/runtime/gate.py#_validate_mutation_surfaces]] constrains changed surfaces and [[nanobot/runtime/mutation_policy.py#MutationPolicy]] represents allowed mutation policy. [[nanobot/runtime/promoted_overlay.py#install_promoted_overlay]] installs only validated promoted code. See [[gates-and-promotion#Cycle branch and gate]] and [[tests/test_bounded_gate.py#TestFailSafe#test_harness_exception_fails_closed]].

## Evidence and fitness trust

Ledger rows and scorecard summaries are evidence with bounded availability, not proof of integration or host health. Fitness verdicts and protected fitness inputs remain harness-owned. [[nanobot/runtime/cycle_ledger.py#append_event]], [[nanobot/runtime/scorecard.py#compute_scorecard]], and [[nanobot/runtime/heldout/__init__.py#run_heldout]] implement distinct evidence paths. See [[evidence#Trust boundary]] and [[tests/test_scorecard.py#TestReaderStatus1179#test_missing_ledger_is_unavailable_not_empty]].

## State and operator authority

Runtime state, operator documents, and product code are different authority domains; absence or unreadability must not imply healthy emptiness. [[nanobot/runtime/state_access.py#Window]] bounds state reads, [[nanobot/runtime/state_paths.py#STATE_PATH_WRITERS]] declares state writers, and [[nanobot/runtime/operator_documents.py#resolve_operator_priorities]] handles operator input. See [[host-boundaries]].

## Constraints and unknowns

These contracts describe checked-in behavior, not host deployment or current runtime state.

Capability specifications describe intended product behavior and ADR status remains authoritative; observed implementation does not accept a proposed ADR. See [[architecture#Source precedence]], [[docs/specs/self-evolving-runtime/spec.md]], and [[docs/adr/ADR-035-the-planning-session-chooses-and-closes-hadi.md]].
