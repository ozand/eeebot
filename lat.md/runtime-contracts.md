# Runtime contracts and limits

The bounded runtime turns candidate work into isolated, checked changes while keeping evidence, fitness, and release authority distinct.

## Cycle orchestration and planning

A planner-selected task, not a candidate request, is the executor assignment. The bridge coordinates demand/proposer input, planning, bounded execution, gates, and evidence; [[nanobot/runtime/bridge.py#_run_planning_session]] selects plan or rest, and [[nanobot/runtime/demand.py#collect_demand]] supplies candidates. See [[planning#Planner choice]] and [[tests/test_agent_chooses.py#test_planner_runs_first_and_its_plan_is_the_task]].

## Gate, mutation, and promotion boundaries

Cycle work is isolated and checked before integration. [[nanobot/runtime/gate.py#_validate_mutation_surfaces]] applies mutation policy, and [[nanobot/runtime/promoted_overlay.py#install_promoted_overlay]] installs validated code.

The append-only check allows additions but blocks deletions under `memory/` and `lessons/`, and rejects renames when either endpoint is under those prefixes (including renames out). It also distinguishes an absent base history file from an unavailable or unreadable baseline, which blocks the gate, and prevents removal of existing `memory/HISTORY.md` lines. If full diff-status inspection fails or returns malformed status records, it blocks even when name-only paths appear unrelated because protected paths cannot be verified; a successful empty status result remains distinct from lookup failure. See [[nanobot/runtime/gate.py#_memory_lessons_append_only_violations]], [[tests/test_memory_lessons_append_only_gate.py#test_rename_out_of_memory_is_rejected_even_though_name_only_shows_only_destination]]. The product specification also records this requirement under Append-only memory and lesson changes (#1768).

## Evidence and fitness trust

Ledger rows and scorecard summaries are evidence with bounded availability, not proof of integration or host health. Fitness verdicts and protected fitness inputs remain harness-owned. [[nanobot/runtime/cycle_ledger.py#append_event]], [[nanobot/runtime/scorecard.py#compute_scorecard]], and [[nanobot/runtime/heldout/__init__.py#run_heldout]] implement distinct evidence paths. See [[evidence#Trust boundary]] and [[tests/test_scorecard.py#TestReaderStatus1179#test_missing_ledger_is_unavailable_not_empty]].

## State and operator authority

Runtime state, operator documents, and product code are different authority domains.

State readers report unavailable inputs explicitly: [[nanobot/runtime/state_access.py#ledger_window]] reports a missing ledger directory, while [[nanobot/runtime/operator_documents.py#resolve_operator_priorities]] distinguishes empty priorities from unavailable input. Writer inventory: [[nanobot/runtime/state_paths.py#STATE_PATH_WRITERS]]. Evidence: [[tests/test_state_access.py#test_ledger_window_missing_dir_is_unavailable]], [[tests/test_operator_documents.py#test_whitespace_only_goal_text_json_is_unavailable_not_empty]], and [[host-boundaries]].

## Constraints and unknowns

These contracts describe checked-in behavior, not host deployment or current runtime state.

Capability specifications describe intended product behavior and ADR status remains authoritative; observed implementation does not accept a proposed ADR. See [[architecture#Source precedence]], [[docs/specs/self-evolving-runtime/spec.md]], and [[docs/adr/ADR-035-the-planning-session-chooses-and-closes-hadi.md]].
