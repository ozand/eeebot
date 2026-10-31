# Runtime contracts and limits

The bounded runtime turns candidate work into isolated, checked changes while keeping evidence, fitness, and release authority distinct.

## Cycle orchestration and planning

A planner-selected task, not a candidate request, is the executor assignment. The bridge coordinates demand/proposer input, planning, bounded execution, gates, and evidence; [[nanobot/runtime/bridge.py#_run_planning_session]] selects plan or rest, and [[nanobot/runtime/demand.py#collect_demand]] supplies candidates. See [[planning#Planner choice]] and [[tests/test_agent_chooses.py#test_planner_runs_first_and_its_plan_is_the_task]].

### No-plan recovery

Recovery classifies planner and supply outcomes separately and bounds consecutive failures before degradation or stopping.

[[nanobot/runtime/no_plan_recovery.py#classify]] separates supply failures (`timed_out`, `supplier_error`) from planner outcomes (`refused`, `malformed`, `no_plan`); other outcomes such as infrastructure failures do not count. [[nanobot/runtime/no_plan_recovery.py#record_outcome]] applies consecutive-family thresholds: 3 planner outcomes enable `planner_degraded` and minimal mode, 6 planner outcomes stop cycle starts, and 6 supply outcomes set `model_supply_degraded` without minimal mode or stopping. Switching families clears the opposite streak. These bounds are directly asserted by [[tests/test_agent_chooses.py#test_no_plan_recovery_is_bounded_and_visible]], [[tests/test_agent_chooses.py#test_supply_and_planner_no_plan_are_counted_apart]], [[tests/test_agent_chooses.py#test_switching_no_plan_family_resets_the_other_consecutive_streak]], and supplier-error coverage in [[tests/test_agent_chooses.py#test_planner_supplier_error_classified_as_supply]].

An integrated plan resets recovery counters and degraded/stopped flags; the existing [[nanobot/runtime/no_plan_recovery.py#resume]] helper clears `stopped`, preserving counters and minimal mode until an integrated plan. The explicit `nanobot recovery-resume --runtime-state-root PATH` operator command uses strict state validation and the bridge lock, refuses symlinked state/ledger paths and an unterminated active ledger line before mutation, preserves normal ledger rollover/cutover preparation (including the narrowly allowlisted raw archive symlink preflight required before pruning), and requires verified state and ledger evidence for success. Partial durability/audit failures return nonzero with explicit uncertainty. It does not start cycles or call providers. Its contract and tests are in [[docs/adr/ADR-043-operator-resume-requires-validated-state-and-shared-lock.md]] and [[tests/test_no_plan_recovery_cli.py]]. The distinction is asserted by [[tests/test_agent_chooses.py#test_reset_and_resume_after_stop]]. Recovery state does not select candidates or assign work: [[tests/test_agent_chooses.py#test_no_automatic_candidate_selection]] and [[tests/test_agent_chooses.py#test_no_plan_does_not_fall_back_to_assignment]] assert absent task/candidate selection and no fallback assignment. The bridge's planner-selected task flow remains the assignment authority, not optional candidate context.

These assertions establish the listed thresholds and state transitions, not a general duration, retry, or recovery-time guarantee. [[tests/test_agent_chooses.py#test_planner_rest_resets_supply_streak]] exercises a distinct open-increment supply-interruption streak reset on successful planner rest; it is not the integrated-plan reset above.

## Gate, mutation, and promotion boundaries

Cycle work is isolated and checked before integration. [[nanobot/runtime/gate.py#_validate_mutation_surfaces]] applies mutation policy, and [[nanobot/runtime/promoted_overlay.py#install_promoted_overlay]] installs validated code.

The append-only check allows additions but blocks deletions under `memory/` and `lessons/`, and rejects renames when either endpoint is under those prefixes (including renames out). It also distinguishes an absent base history file from an unavailable or unreadable baseline, which blocks the gate, and prevents removal of existing `memory/HISTORY.md` lines. Full diff-status inspection uses NUL-delimited records to preserve pathnames containing quotes, tabs, newlines, and non-ASCII characters. Lookup failure or malformed records block even when name-only paths appear unrelated because protected paths cannot be verified; a successful empty status result remains distinct from lookup failure. See [[nanobot/runtime/gate.py#_memory_lessons_append_only_violations]], [[tests/test_memory_lessons_append_only_gate.py#test_rename_out_of_memory_is_rejected_even_though_name_only_shows_only_destination]]. The product specification also records this requirement under Append-only memory and lesson changes (#1768).

## Evidence and fitness trust

Ledger rows and scorecard summaries are evidence with bounded availability, not proof of integration or host health. Fitness verdicts and protected fitness inputs remain harness-owned. [[nanobot/runtime/cycle_ledger.py#append_event]], [[nanobot/runtime/scorecard.py#compute_scorecard]], and [[nanobot/runtime/heldout/__init__.py#run_heldout]] implement distinct evidence paths. See [[evidence#Trust boundary]] and [[tests/test_scorecard.py#TestReaderStatus1179#test_missing_ledger_is_unavailable_not_empty]].

## Reflector transcript provenance

The reflector joins prompts only to the resolved executor attempt; missing evidence is unavailable, never a planner fallback. Unavailable evidence differs from pruning and has bounded retries.

[[nanobot/runtime/bridge.py#_authoritative_subagent_task_id]] resolves primary versus successful repair, while [[nanobot/observability/llm_telemetry.py#write_authoritative_attempt]] persists a private attempt-resolution sidecar coupled to prompt retention. Subagent prompt rows carry a private attempt identifier; [[nanobot/runtime/reflector.py#_prompt_records]] joins the exact attempt and exposes only executor component, sequence, and selection disposition in the reflection. Prompt pruning synchronously removes expired or unjoined resolution records. This is application-path privacy, not an OS-role boundary; see proposed [[docs/adr/ADR-040-reflector-selects-authoritative-executor-attempt.md]].

The tests include a real SubagentManager spawn with a stub provider, bridge primary selection, sidecar writing, and reflector consumption; successful repair selection uses a separate synthetic bridge fixture. `correlation_unavailable` outcomes are retried within the existing bounded attempt limit and terminate after a successful reflection; missing or malformed correlation is not mislabeled as pruning. [[tests/test_subagent_manager.py#test_subagent_spawn_id_reaches_executor_prompt_telemetry]] verifies spawn-to-writer attribution; [[tests/test_reflector.py#test_reflector_selects_bridge_authoritative_successful_repair_attempt]] proves a successful repair is selected; [[tests/test_reflector.py#test_reflector_selects_resolved_executor_over_higher_sequence_planner]] proves a higher planner sequence cannot override the exact executor attempt. [[tests/test_reflector.py#test_reflector_custom_root_and_decoy_default_root]] checks custom-root precedence, and [[tests/test_reflector.py#test_reflector_retries_unavailable_correlation_when_evidence_arrives]] checks bounded retry after late correlation. These tests do not establish post-deployment host mismatch rates.

## State and operator authority

Runtime state, operator documents, and product code are different authority domains.

State readers report unavailable inputs explicitly: [[nanobot/runtime/state_access.py#ledger_window]] reports a missing ledger directory, while [[nanobot/runtime/operator_documents.py#resolve_operator_priorities]] distinguishes empty priorities from unavailable input. Writer inventory: [[nanobot/runtime/state_paths.py#STATE_PATH_WRITERS]]. Evidence: [[tests/test_state_access.py#test_ledger_window_missing_dir_is_unavailable]], [[tests/test_operator_documents.py#test_whitespace_only_goal_text_json_is_unavailable_not_empty]], and [[host-boundaries]].

## Constraints and unknowns

These contracts describe checked-in behavior, not host deployment or current runtime state.

Capability specifications describe intended product behavior and ADR status remains authoritative; observed implementation does not accept a proposed ADR. See [[architecture#Source precedence]], [[docs/specs/self-evolving-runtime/spec.md]], and [[docs/adr/ADR-035-the-planning-session-chooses-and-closes-hadi.md]].
