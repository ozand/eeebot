# Runtime module coverage

A pinned inventory of runtime Python modules with individually stated responsibilities and thematic contract links; associations are not claims of direct test coverage.

## Cycle planning and execution

These modules perform distinct cycle coordination, candidate choice, task shaping, bounded execution, and gating roles. Theme: [[runtime-contracts#Cycle orchestration and planning]] and [[runtime-contracts#Gate, mutation, and promotion boundaries]]. The planner-first invariant is tested by [[tests/test_agent_chooses.py#test_planner_runs_first_and_its_plan_is_the_task]]; missing-directory state status is covered separately below.

- `__init__.py` — package-level state-reporting and bounded-cycle coordination exports; no independent behavior/test mapping asserted.
- [[nanobot/runtime/bridge.py]] — cycle coordinator: assembles planning inputs, runs subagent, applies gates and records outcomes; planning behavior anchored by [[nanobot/runtime/bridge.py#_run_planning_session]].
- [[nanobot/runtime/demand.py]] — deterministic candidate collection from demand sources; [[nanobot/runtime/demand.py#collect_demand]].
- [[nanobot/runtime/demand_ranking.py]] — ranks demand using value, urgency, and size estimates; exact test mapping unknown.
- [[nanobot/runtime/llm_proposer.py]] — proposes candidate requests but does not select executor task; [[nanobot/runtime/llm_proposer.py#maybe_propose]].
- [[nanobot/runtime/planner_candidates.py]] — exposes and records planner candidate choices; [[nanobot/runtime/planner_candidates.py#peek_new_priority_items]]; direct test mapping unknown.
- [[nanobot/runtime/planner_dedup_evidence.py]] — retains pending evidence for rejected duplicate candidates; direct test mapping unknown.
- [[nanobot/runtime/planner_hypothesis.py]] — parses planner hypothesis form; direct test mapping unknown.
- [[nanobot/runtime/planner_rest.py]] — validates planner rest response and wake condition; direct test mapping unknown.
- [[nanobot/runtime/no_plan_recovery.py#classify]] — classifies supply versus planner outcomes; [[nanobot/runtime/no_plan_recovery.py#record_outcome]] applies consecutive thresholds and integrated-plan reset, while [[nanobot/runtime/no_plan_recovery.py#resume]] preserves recovery state when an operator resumes. Explicit CLI resume uses [[nanobot/runtime/operator_recovery.py#resume_operator]] and [[tests/test_no_plan_recovery_cli.py#test_recovery_resume_requires_explicit_root_and_reports_safe_result]]. Asserted cases: [[tests/test_agent_chooses.py#test_no_plan_recovery_is_bounded_and_visible]], [[tests/test_agent_chooses.py#test_supply_and_planner_no_plan_are_counted_apart]], [[tests/test_agent_chooses.py#test_switching_no_plan_family_resets_the_other_consecutive_streak]], [[tests/test_agent_chooses.py#test_reset_and_resume_after_stop]], [[tests/test_agent_chooses.py#test_no_automatic_candidate_selection]], and [[tests/test_agent_chooses.py#test_no_plan_does_not_fall_back_to_assignment]]. Additional supplier-error classification: [[tests/test_agent_chooses.py#test_planner_supplier_error_classified_as_supply]]. See [[runtime-contracts#Cycle orchestration and planning#No-plan recovery]]; finer duration/retry guarantees are not established by these assertions.
- [[nanobot/runtime/open_increment.py]] — persists/interprets interrupted increment resolution with bounded backoff; [[nanobot/runtime/open_increment.py#resolve]].
- [[nanobot/runtime/bounded_subagent_executor.py]] — deterministic bounded executor entrypoint; detailed test association unknown.
- [[nanobot/runtime/enhancement_gate.py]] — defers enhancement requests targeting scripts with no runtime use; direct test mapping unknown.
- [[nanobot/runtime/task_criteria.py]] — validates task criteria and metric references; direct test mapping unknown.
- [[nanobot/runtime/task_shape.py]] — classifies task shape from repository evidence; direct test mapping unknown.
- [[nanobot/runtime/gate.py]] — checks mutation surfaces and chooses bounded smoke tests; [[nanobot/runtime/gate.py#_validate_mutation_surfaces]], tested fail-closed by [[tests/test_bounded_gate.py#TestFailSafe#test_harness_exception_fails_closed]].
- [[nanobot/runtime/mutation_policy.py]] — defines read-versus-commit authority policy; exact test mapping unknown.
- [[nanobot/runtime/test_guard.py]] — detects test-weakening changes; exact test mapping unknown.
- [[nanobot/runtime/stop_guards.py#revision_outcome]] — summarizes smoke-gate repair revisions as passed, blocked at cap, or unresolved below cap; persists capped smoke output. Covered by [[tests/test_stop_guards.py#test_revision_passed]], [[tests/test_stop_guards.py#test_revision_cap_reached_blocks]], [[tests/test_stop_guards.py#test_revision_under_cap_is_unresolved_not_blocked]], [[tests/test_stop_guards.py#test_revision_outcome_persists_last_smoke_output]], and [[tests/test_stop_guards.py#test_revision_outcome_truncates_last_smoke_output_to_2000_chars]]. This helper describes repair-loop outcomes, not general cycle stopping.
- [[nanobot/runtime/service_paths.py]] — identifies service-only paths for outcome/package measurement, not mutation policy; exact test mapping unknown.
- [[nanobot/runtime/runtime_deny.py]] — defines immutable runtime-slice deny set and slice path helpers, not cycle stop conditions; exact test mapping unknown.
- [[nanobot/runtime/promoted_overlay.py]] — loads root-verified promoted modules; [[nanobot/runtime/promoted_overlay.py#install_promoted_overlay]], distinct from candidate rotation.
- [[nanobot/runtime/promotions_rotation.py]] — prunes/rotates promotion artifacts by retention policy; exact test mapping unknown.

## Evidence and evaluation

These modules preserve distinct cycle, evaluation, fitness, benchmark, and validation evidence.

Theme: [[runtime-contracts#Evidence and fitness trust]]. The representative unavailable-ledger contract is tested by [[tests/test_scorecard.py#TestReaderStatus1179#test_missing_ledger_is_unavailable_not_empty]]; this does not imply tests for the other mappings below.

- [[nanobot/runtime/cycle_ledger.py]] — append-only cycle phases and terminal outcomes; [[nanobot/runtime/cycle_ledger.py#append_event]].
- [[nanobot/runtime/scorecard.py]] — composes bounded fitness/evidence summary; [[nanobot/runtime/scorecard.py#compute_scorecard]].
- [[nanobot/runtime/heldout/__init__.py]] — runs held-out verification pack; [[nanobot/runtime/heldout/__init__.py#run_heldout]].
- [[nanobot/runtime/heldout/checkers.py]] — checker implementations for held-out artifact behavior; exact test mapping unknown.
- [[nanobot/runtime/heldout/microbench.py]] — harness-run causal microbench measurement; exact test mapping unknown.
- [[nanobot/runtime/benchmark_evidence.py]] — gates optimization claims on benchmark evidence; exact test mapping unknown.
- [[nanobot/runtime/hypothesis_verdict.py]] — harness-computed hypothesis verdict, distinct from backlog storage; exact test mapping unknown.
- [[nanobot/runtime/diary_fitness.py]] — measures whether a cycle read the day diary; exact test mapping unknown.
- [[nanobot/runtime/planning_fitness.py]] — records planner overhead ratio; exact test mapping unknown.
- [[nanobot/runtime/skill_fitness.py]] — records skill-read sidecar evidence; exact test mapping unknown.
- [[nanobot/runtime/skill_eval_harness.py]] — runs A/B skill evaluations, distinct from read telemetry; exact test mapping unknown.
- [[nanobot/runtime/usage_evidence.py#_touched_from_results_with_status]] — reads result `files_changed` as touched evidence, with explicit complete/partial/missing/corrupt status; touch is not execution/use. Archive union is exercised by [[tests/test_usage_evidence.py#TestArchiveAwareTouchedEvidence#test_archived_result_contributes_touched_evidence]]; one missing result directory yields partial in [[tests/test_usage_evidence.py#TestArchiveAwareTouchedEvidence#test_one_missing_result_dir_is_partial]].
- [[nanobot/runtime/usage_evidence.py#stale_artifacts_status]] and [[nanobot/runtime/usage_evidence.py#stale_artifacts]] — decay candidates require usable touch evidence and old last-used/last-touched evidence; incomplete/missing evidence blocks destructive decay. See [[tests/test_usage_evidence.py#TestArchiveAwareTouchedEvidence#test_missing_result_evidence_blocks_decay]], [[tests/test_usage_evidence.py#TestArchiveAwareTouchedEvidence#test_cap_does_not_launder_a_corrupt_file_inside_the_window]], and [[tests/test_usage_evidence.py#TestDecayDemand#test_stale_artifact_becomes_decay_demand_ordered_last]].
- [[nanobot/runtime/usage_evidence.py#confirm_serves]] — confirms completed work only when harness-observed use is newer than completion; touch alone cannot confirm. See [[tests/test_usage_evidence.py#TestConfirmServes#test_newer_usage_confirms_completed_entry]], [[tests/test_usage_evidence.py#TestConfirmServes#test_touched_alone_never_confirms]], and [[tests/test_usage_evidence.py#TestConfirmServes#test_older_usage_does_not_confirm]].
- [[nanobot/runtime/trainer_evidence.py]] — validates cited trainer evidence; exact test mapping unknown.
- [[nanobot/runtime/trajectory.py]] — checks trajectory-level verification shape; exact test mapping unknown.
- [[nanobot/runtime/validator_harness.py]] — executes built validators and consumes findings; exact test mapping unknown.
- [[nanobot/runtime/local_ci.py]] — runs/records local CI result and state summary; exact test mapping unknown.
- [[nanobot/runtime/prompt_fit.py]] — summarizes bounded prompt-fit telemetry; exact test mapping unknown.
- [[nanobot/runtime/activation_check.py]] — model-free activation/self-check and timeout recording; exact test mapping unknown.

## State, time, and operator inputs

These modules have separate data ownership and time semantics; shared state storage does not imply interchangeable contracts. Theme: [[runtime-contracts#State and operator authority]]. Missing ledger behavior is concretely specified by [[nanobot/runtime/state_access.py#ledger_window]] and [[tests/test_state_access.py#test_ledger_window_missing_dir_is_unavailable]].

- [[nanobot/runtime/operator_recovery.py]] — strict, lock-coordinated operator resume of stopped no-plan state; [[nanobot/runtime/operator_recovery.py#resume_operator]], tested by [[tests/test_no_plan_recovery_cli.py]].
- [[nanobot/runtime/state_access.py]] — bounded readers return status for rotated/unreadable state; [[nanobot/runtime/state_access.py#ledger_window]].
- [[nanobot/runtime/state_paths.py]] — registry of state-path writers, not a reader; [[nanobot/runtime/state_paths.py#STATE_PATH_WRITERS]].
- [[nanobot/runtime/state_path_invocations.py]] — checks selected writer invocation contracts; [[nanobot/runtime/state_path_invocations.py#check_writer_invocations]].
- [[nanobot/runtime/state.py]] — canonical state helpers for operator-facing summaries; exact test mapping unknown.
- [[nanobot/runtime/operator_documents.py]] — sole resolvers for operator docs; distinguishes absent/unreadable/empty; [[nanobot/runtime/operator_documents.py#resolve_operator_priorities]] and [[tests/test_operator_documents.py#test_whitespace_only_goal_text_json_is_unavailable_not_empty]].
- [[nanobot/runtime/goal_review.py]] — periodic bounded goal review and derived priorities; exact test mapping unknown.
- [[nanobot/runtime/goal_text_utils.py]] — filters completed priority text using artifacts/git evidence; exact test mapping unknown.
- [[nanobot/runtime/goal_gap_futility.py]] — bounded futility tracking for repeated goal-gap attempts; exact test mapping unknown.
- [[nanobot/runtime/day_clock.py]] — defines day boundaries and deliverable stage; distinct from diary file paths.
- [[nanobot/runtime/day_key.py]] — canonical cutover marker and local-day key, distinct from day-stage calculations; exact test mapping unknown.
- [[nanobot/runtime/day_diary.py]] — defines diary path/append format, not fitness measurement; exact test mapping unknown.
- [[nanobot/runtime/health.py]] — summarizes cycle health from runtime state; exact test mapping unknown.
- [[nanobot/runtime/doctor.py]] — deterministic read-only host health checks, distinct from state-derived cycle health; exact test mapping unknown.
- [[nanobot/runtime/strategist_inputs.py]] — bounded provenance-carrying archive inputs; exact test mapping unknown.
- [[nanobot/runtime/strategist.py]] — bounded archive review/advisories; distinct from input loading; exact test mapping unknown.
- [[nanobot/runtime/probes.py]] — handles runtime self-evolution probe commands; exact test mapping unknown.
- [[nanobot/runtime/avatar/state.py]] — observed/resolved avatar state and published state-file writer; exact test mapping unknown.
- [[nanobot/runtime/avatar/__init__.py]] — package marker only; no independent runtime contract or test asserted.

## Knowledge, lessons, and derived artifacts

These modules curate and retrieve different knowledge artifacts; lesson quality, selection context, archival rotation, and action histories are distinct duties. Related candidate boundary: [[planning#Candidate sources]]. Direct test mappings are named only where linked.

- [[nanobot/runtime/knowledge_curator.py]] — bounded auditable promotion of lesson-derived knowledge; exact test mapping unknown.
- [[nanobot/runtime/knowledge_lift.py]] — measures incremental value of knowledge context; exact test mapping unknown.
- [[nanobot/runtime/lesson_v2.py]] — validates and normalizes bounded lesson schema v2; exact test mapping unknown.
- [[nanobot/runtime/lesson_index.py]] — bounded deterministic Markdown lesson catalogue lookup; exact test mapping unknown.
- [[nanobot/runtime/lessons_context.py]] — constructs lesson context for proposer from task/corpus signals; exact test mapping unknown.
- [[nanobot/runtime/lessons_rotation.py]] — mechanically rotates lesson/error files; not lesson selection or validation; exact test mapping unknown.
- [[nanobot/runtime/reflector.py]] — writes decoupled per-cycle transcript reflections; exact test mapping unknown.
- [[nanobot/runtime/reflection_context.py]] — extracts bounded steering hints from reflector journal, not journal writing; exact test mapping unknown.
- [[nanobot/runtime/retirement_candidates.py]] — derives retirement candidates from measurement evidence; exact test mapping unknown.
- [[nanobot/runtime/hypothesis_backlog.py]] — stores/claims hypothesis priorities; verdict calculation is separate in `hypothesis_verdict.py`; exact test mapping unknown.
- [[nanobot/runtime/tech_tree.py]] — tracks improvement directions/portfolio rather than individual cycle hypotheses; exact test mapping unknown.
- [[nanobot/runtime/evolution_tree.py]] — git-native evolutionary ancestry tree; distinct from improvement-direction portfolio; exact test mapping unknown.
- [[nanobot/runtime/loop_explorer.py]] — creates static loop visualization, not runtime orchestration; exact test mapping unknown.
- [[nanobot/runtime/system_map.py]] — inventories self-evolving repository scripts for system map; exact test mapping unknown.
- [[nanobot/runtime/skills_index.py]] — derives skills index from repository state; exact test mapping unknown.
- [[nanobot/runtime/skill_candidate_mining.py]] — mines recurring actions into candidate skills; distinct from skill evaluation/fitness; exact test mapping unknown.
- [[nanobot/runtime/action_index.py]] — builds durable deterministic per-cycle action index; exact test mapping unknown.
- [[nanobot/runtime/action_registry.py]] — snapshots available actions/tools; distinct from observed action index; exact test mapping unknown.
- [[nanobot/runtime/archive.py]] — records/reads bridge-lane stepping stones for proposer steering; exact test mapping unknown.
- [[nanobot/runtime/artifact_graph.py]] — models artifact dependency graph; exact test mapping unknown.
- [[nanobot/runtime/existence_index.py]] — FTS5 near-duplicate detection index for cycle dedup, not artifact dependency graph; exact test mapping unknown.

## Shared support, contracts, and bounded helpers

These modules have focused reusable duties; a helper's proximity to a subsystem does not establish its test coverage. The relevant contract boundary is [[runtime-contracts#Constraints and unknowns]].

- [[nanobot/runtime/_io.py]] — shared JSON/time IO primitives; behavior-to-test mapping unknown.
- [[nanobot/runtime/schemas.py]] — TypedDict schemas for runtime state; validation behavior not inferred from declarations.
- [[nanobot/runtime/context_compaction.py]] — bounds subagent context and compaction; exact test mapping unknown.
- [[nanobot/runtime/session_clock.py]] — call percentile/final-budget/progress-timeout safety timing; exact test mapping unknown.
- [[nanobot/runtime/model_registry.py]] — resolves runtime model routing; distinct from provider construction; exact test mapping unknown.
- [[nanobot/runtime/llm_error_classification.py]] — classifies supplier outage versus other LLM errors; exact test mapping unknown.
- [[nanobot/runtime/persona_harness.py]] — persona self-consistency harness and statements; exact test mapping unknown.
- [[nanobot/runtime/role_prompt.py]] — builds role prompts from files and release root; exact test mapping unknown.
- [[nanobot/runtime/commit_markers.py]] — identifies/sanitizes harness commit markers and trailers; exact test mapping unknown.
- [[nanobot/runtime/__init__.py]] — package exports/documentation for canonical state reporting and cycle coordination; package-level role, no separate test asserted.

## Inventory and evidence limits

All paths listed above are individually assigned a role; this is not a claim that each has a direct test or that a thematic section proves every implementation detail.

The inventory is 89 Python paths at baseline `e5c52196cbef9d21498b7f87563b2b1c4049b802`. Named tests are direct evidence only for their linked behavior; other test mappings remain unknown. `avatar/__init__.py` is a package marker without independent behavior. Current implementation is not ADR acceptance; see [[architecture#Decision-record navigation]] and [[runtime-contracts#Constraints and unknowns]].
