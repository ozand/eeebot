# Runtime module coverage

This ledger records the runtime Python module inventory at baseline `e5c52196cbef9d21498b7f87563b2b1c4049b802`; groups identify the thematic contract, not a claim that every module has a direct test.

## Cycle, planning, and gates

This group concerns orchestration, candidates, recovery, execution, and mutation control.

Links identify thematic contracts, not direct per-module test coverage. See [[runtime-contracts#Cycle orchestration and planning]] and [[runtime-contracts#Gate, mutation, and promotion boundaries]]. Modules: [[nanobot/runtime/bridge.py]], [[nanobot/runtime/bounded_subagent_executor.py]], [[nanobot/runtime/demand.py]], [[nanobot/runtime/demand_ranking.py]], [[nanobot/runtime/enhancement_gate.py]], [[nanobot/runtime/gate.py]], [[nanobot/runtime/llm_proposer.py]], [[nanobot/runtime/no_plan_recovery.py]], [[nanobot/runtime/open_increment.py]], [[nanobot/runtime/planner_candidates.py]], [[nanobot/runtime/planner_dedup_evidence.py]], [[nanobot/runtime/planner_hypothesis.py]], [[nanobot/runtime/planner_rest.py]], [[nanobot/runtime/task_criteria.py]], [[nanobot/runtime/task_shape.py]], [[nanobot/runtime/test_guard.py]], [[nanobot/runtime/mutation_policy.py]], [[nanobot/runtime/service_paths.py]], [[nanobot/runtime/promoted_overlay.py]], [[nanobot/runtime/promotions_rotation.py]], [[nanobot/runtime/runtime_deny.py]], [[nanobot/runtime/stop_guards.py]].

## Evidence, fitness, and validation

These modules provide evidence, fitness, or validation paths; only specifically linked symbols/tests are evidence of tested behavior. The thematic boundary is [[runtime-contracts#Evidence and fitness trust]]. Modules: [[nanobot/runtime/activation_check.py]], [[nanobot/runtime/benchmark_evidence.py]], [[nanobot/runtime/cycle_ledger.py]], [[nanobot/runtime/diary_fitness.py]], [[nanobot/runtime/heldout/__init__.py]], [[nanobot/runtime/heldout/checkers.py]], [[nanobot/runtime/heldout/microbench.py]], [[nanobot/runtime/hypothesis_verdict.py]], [[nanobot/runtime/planning_fitness.py]], [[nanobot/runtime/scorecard.py]], [[nanobot/runtime/skill_eval_harness.py]], [[nanobot/runtime/skill_fitness.py]], [[nanobot/runtime/trainer_evidence.py]], [[nanobot/runtime/usage_evidence.py]], [[nanobot/runtime/validator_harness.py]], [[nanobot/runtime/trajectory.py]], [[nanobot/runtime/prompt_fit.py]], [[nanobot/runtime/local_ci.py]].

## State, demand lifecycle, and knowledge

These modules handle state, demand lifecycle, or knowledge artifacts; their grouping is thematic, not proof of a common invariant or test. See [[runtime-contracts#State and operator authority]] and [[planning#Candidate sources]]. Modules: [[nanobot/runtime/_io.py]], [[nanobot/runtime/action_index.py]], [[nanobot/runtime/action_registry.py]], [[nanobot/runtime/archive.py]], [[nanobot/runtime/artifact_graph.py]], [[nanobot/runtime/avatar/__init__.py]], [[nanobot/runtime/avatar/state.py]], [[nanobot/runtime/commit_markers.py]], [[nanobot/runtime/day_clock.py]], [[nanobot/runtime/day_diary.py]], [[nanobot/runtime/day_key.py]], [[nanobot/runtime/evolution_tree.py]], [[nanobot/runtime/existence_index.py]], [[nanobot/runtime/goal_gap_futility.py]], [[nanobot/runtime/goal_review.py]], [[nanobot/runtime/goal_text_utils.py]], [[nanobot/runtime/hypothesis_backlog.py]], [[nanobot/runtime/knowledge_curator.py]], [[nanobot/runtime/knowledge_lift.py]], [[nanobot/runtime/lesson_index.py]], [[nanobot/runtime/lesson_v2.py]], [[nanobot/runtime/lessons_context.py]], [[nanobot/runtime/lessons_rotation.py]], [[nanobot/runtime/loop_explorer.py]], [[nanobot/runtime/operator_documents.py]], [[nanobot/runtime/reflector.py]], [[nanobot/runtime/retirement_candidates.py]], [[nanobot/runtime/skills_index.py]], [[nanobot/runtime/state.py]], [[nanobot/runtime/state_access.py]], [[nanobot/runtime/state_path_invocations.py]], [[nanobot/runtime/state_paths.py]], [[nanobot/runtime/strategist.py]], [[nanobot/runtime/strategist_inputs.py]], [[nanobot/runtime/system_map.py]], [[nanobot/runtime/tech_tree.py]], [[nanobot/runtime/usage_evidence.py]].

## Shared runtime support and interfaces

Support modules provide shared interfaces, limits, and coordination helpers.

Specific behavior-to-test correspondence is unknown unless linked elsewhere; inclusion is inventory coverage, not blanket semantic coverage. Modules: [[nanobot/runtime/__init__.py]], [[nanobot/runtime/context_compaction.py]], [[nanobot/runtime/doctor.py]], [[nanobot/runtime/health.py]], [[nanobot/runtime/llm_error_classification.py]], [[nanobot/runtime/model_registry.py]], [[nanobot/runtime/persona_harness.py]], [[nanobot/runtime/probes.py]], [[nanobot/runtime/reflection_context.py]], [[nanobot/runtime/role_prompt.py]], [[nanobot/runtime/schemas.py]], [[nanobot/runtime/session_clock.py]], [[nanobot/runtime/skill_candidate_mining.py]], [[nanobot/runtime/stop_guards.py]].

## Verification corpus and disposition limits

This ledger covers the pinned runtime inventory and distinguishes relevant tests from unknown mappings.

The groups above account for the 89 tracked `nanobot/runtime/**/*.py` paths at the pinned baseline; nested `heldout` and `avatar` modules are included. Test corpus is separate (354 Python tests per issue evidence); only the representative linked tests above are asserted as directly relevant. Other per-module test mappings are unknown here, not inferred absent. Source links identify module coverage; thematic links state the contract. No module is excluded from scope. Specifications express intended behavior and ADR decision status is preserved; implementation evidence does not imply acceptance.
