"""Minimal durable self-evolving runtime coordinator.

The cycle-phase helpers this module used to define directly now live in
nanobot.runtime.cycle_observe / cycle_feedback / cycle_planning /
cycle_persist (issue #600); this module keeps run_self_evolving_cycle()
as the thin driver plus re-exports of every name that used to live here
so `from nanobot.runtime.coordinator import X` keeps working unchanged.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from nanobot.observability.llm_telemetry import call_context
from nanobot.runtime._io import read_json_safe as _safe_read_json
from nanobot.runtime._io import utc_iso as _utc_iso
from nanobot.runtime._io import utc_now as _utc_now
from nanobot.runtime.autoevolve import resolve_terminal_selfevo_issue
from nanobot.runtime.promotion import (
    complete_promotion_readiness_packet,
    supply_missing_promotion_readiness_inputs,
)
from nanobot.runtime.lessons import update_lessons_from_cycle
from nanobot.runtime.state import _subagent_rollup_snapshot
from nanobot.runtime.stop_guards import (
    MAX_ITERATIONS_DEFAULT,
    budget_exceeded,
    derive_stop_reason,
    evaluate_stall,
    lane_iteration,
    pick_alternative_task,
    should_switch_lane,
)
from nanobot.runtime.subagent_materializer import materialize_subagent_requests
from nanobot.utils.helpers import estimate_prompt_tokens

_logger = logging.getLogger(__name__)

# --- Re-exports: every name that used to be defined directly in this module
# now lives in one of the cycle_*.py phase modules below (issue #600). Kept
# importable from here, unchanged, for backward compatibility (CLI, app,
# scripts, and ~90 tests import many of these by name from this module).
from nanobot.runtime.cycle_observe import (  # noqa: F401
    AMBITION_UNDERUTILIZATION_STREAK_LIMIT,
    COMPLETED_TASK_STATUSES,
    CORE_TASK_IDS,
    CREDITS_LEDGER_VERSION,
    DEFAULT_ACTIVE_GOAL,
    DEFAULT_EXPERIMENT_BUDGET,
    EXPANDED_EXPERIMENT_BUDGET,
    EXPERIMENT_BUDGET_HARD_CEILING,
    EXPERIMENT_CONTRACT_VERSION,
    EXPERIMENT_VERSION,
    GOAL_ROTATION_STREAK_LIMIT,
    HYPOTHESIS_BACKLOG_VERSION,
    KNOWN_TASK_IDS,
    LOW_REWARD_THRESHOLD,
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
    PATCH_BUNDLE_VERSION,
    PROMOTION_RECORD_VERSION,
    REPEATED_BLOCK_LIMIT,
    SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
    TASK_ACTION_CLASS_BY_ID,
    TASK_PLAN_VERSION,
    _BACKLOG_PROGRESSION_IDS,
    _freshest_reusable_insight,
    _git_output,
    _goal_relevance_tokens,
    _insight_is_actionable,
    _json_files_sorted_by_mtime,
    _lesson_insight_text,
    _lesson_reward_value,
    _load_recent_history_entries,
    _next_open_goal_as_backlog_task,
    _next_open_goal_hypothesis,
    _normalize_artifact_paths,
    _observed_product_head_source_fingerprint,
    _parse_datetime,
    _pick_task_for_classes,
    _prompt_mass_snapshot,
    _rank_insights_for_goal,
    _release_metadata_source_fingerprint,
    _render_task_selection,
    _resolve_runtime_state_root,
    _retire_orphaned_task_ids,
    _runtime_source_fingerprint,
    _select_insight_for_goal,
    _task_action_class,
    _task_has_recorded_terminal_selfevo_retirement,
    _task_is_selectable,
    _task_is_terminal_selfevo_retired,
    _task_status,
    _task_title_for_id,
)
from nanobot.runtime.cycle_feedback import (  # noqa: F401
    _ambition_streak_key,
    _ambition_underutilization_reasons,
    _bridge_handled_request_ids,
    _build_experiment_contract,
    _build_experiment_snapshot,
    _build_revert_record,
    _clamp_experiment_budget,
    _derive_budget_usage,
    _derive_experiment_budget_policy,
    _derive_experiment_current_task_id,
    _derive_feedback_decision,
    _derive_mutation_lane,
    _derive_reward_signal,
    _enrich_decision_lane_with_insight,
    _ensure_active_goal,
    _experiment_complexity_summary,
    _experiment_metric_summary,
    _extract_history_signature,
    _history_budget_used,
    _history_experiment_outcome,
    _history_failure_class,
    _latest_goal_rotation_streak,
    _load_approval_gate,
    _load_previous_experiment_snapshot,
    _subagent_consumption_snapshot,
    _synthesized_materialize_improvement_candidate,
    _synthesized_next_improvement_candidate,
    _task_readiness_contract,
    _task_readiness_gate,
    _write_active_goal,
)
from nanobot.runtime.cycle_planning import (  # noqa: F401
    _build_task_plan_snapshot,
    _curriculum_level,
    _derive_generated_candidates,
    _inferred_generated_candidates_from_tasks,
    _latest_failure_learning,
    _open_ended_novelty_directive,
    _parse_backlog_task_from_goal_text,
    _parse_backlog_task_from_memory,
    _pick_candidate_from_research_feed,
    _recent_git_log,
    _research_feed_entry_is_self_referential,
    _subagent_lane_health,
    _synthesize_hypothesis_from_state,
    _title_already_done_in_git_log,
    _write_materialized_improvement_artifact,
    _write_research_feed,
)
from nanobot.runtime.cycle_persist import (  # noqa: F401
    _bounded_priority_score,
    _build_hypothesis_backlog_snapshot,
    _commit_at_or_after_cycle_start,
    _derive_bounded_tasks_from_plan,
    _hadi_entry,
    _has_concrete_changes,
    _load_previous_credit_balance,
    _normalize_blocker_summary,
    _switch_off_stalled_lane,
    _task_effort_weight,
    _task_execution_acceptance,
    _validate_control_plane_summary_payload,
    _workspace_looks_like_eeepc_live_runtime,
    _write_control_plane_summary_artifact,
    _write_credits_ledger,
    _wsjf_components,
)

# Issue #864: reports/evolution-*.json is read-alive (cycle_planning.py's
# _recent_report_streak reads at most the 10 newest by mtime; state.py's
# load_runtime_state_from_root reads only the single newest) but was growing
# unbounded — every cycle wrote a new report and nothing ever pruned old
# ones. KEEP is set far above the actual reader window (10) so pruning can
# never affect any reader's behavior.
REPORTS_RETENTION_KEEP = 200


def _prune_stale_reports(reports_dir: Path, keep: int = REPORTS_RETENTION_KEEP) -> None:
    """Delete all but the ``keep`` newest ``evolution-*.json`` reports.

    Fail-open: any error is swallowed so a pruning failure can never break a
    cycle. Only targets the ``evolution-*.json`` naming this module writes —
    never touches other files that might land in ``reports_dir``.
    """
    try:
        files = sorted(
            reports_dir.glob("evolution-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale_path in files[keep:]:
            try:
                stale_path.unlink()
            except OSError:
                pass
    except Exception:
        pass


async def run_self_evolving_cycle(
    workspace: Path,
    tasks: str,
    execute_turn: Callable[[str], Awaitable[str]],
    now: datetime | None = None,
) -> str:
    """Run one bounded self-evolving cycle and persist canonical artifacts."""
    current = _utc_now(now)
    state_root = _resolve_runtime_state_root(workspace)
    reports_dir = state_root / "reports"
    goals_dir = state_root / "goals"
    outbox_dir = state_root / "outbox"
    hypotheses_dir = state_root / "hypotheses"
    promotions_dir = state_root / "promotions"
    experiments_dir = state_root / "experiments"
    credits_dir = state_root / "credits"
    for directory in (reports_dir, goals_dir, outbox_dir, hypotheses_dir, experiments_dir, credits_dir):
        directory.mkdir(parents=True, exist_ok=True)

    recorded_task_plan = _safe_read_json(goals_dir / "current.json")
    feedback_decision = _derive_feedback_decision(recorded_task_plan, goals_dir, state_root=state_root)
    feedback_decision = _enrich_decision_lane_with_insight(
        feedback_decision,
        workspace,
        (recorded_task_plan or {}).get("goal_id") if isinstance(recorded_task_plan, dict) else None,
    )
    # R11 enforcement: if the previous cycle stalled out, switch off that lane
    # before deriving the bounded tasks so the next cycle works something else.
    previous_experiment = _load_previous_experiment_snapshot(experiments_dir)
    feedback_decision = _switch_off_stalled_lane(feedback_decision, recorded_task_plan, previous_experiment)
    selected_tasks, task_selection_source = _derive_bounded_tasks_from_plan(tasks, recorded_task_plan, feedback_decision)

    active_goal = _ensure_active_goal(goals_dir, current)
    approval_gate, next_hint = _load_approval_gate(state_root, current)

    cycle_id = f"cycle-{uuid.uuid4().hex[:12]}"
    evidence_ref_id = f"evidence-{uuid.uuid4().hex[:12]}"
    cycle_started = _utc_iso(current)

    execution_response: str | None = None
    execution_error: str | None = None
    promotion_candidate_id: str | None = None
    review_status: str | None = None
    decision: str | None = None
    if approval_gate["state"] == "fresh":
        try:
            # Issue #675: attribute this cycle's LLM calls (component=coordinator)
            # for the duration of the actual work turn.
            with call_context(cycle_id, "coordinator"):
                execution_response = await execute_turn(selected_tasks)
            promotion_candidate_id = f"promotion-{uuid.uuid4().hex[:12]}"
            review_status = "not_ready_for_policy_review"
            decision = "not_ready_for_policy_review"
            result_status = "PASS"
            bounded_apply = "on"
            promotion_execute = "on"
            summary = f"Self-evolving cycle PASS — goal={active_goal} — evidence={evidence_ref_id}"
        except Exception as exc:
            execution_error = str(exc)
            result_status = "ERROR"
            bounded_apply = "off"
            promotion_execute = "off"
            summary = f"Self-evolving cycle ERROR — goal={active_goal} — {execution_error}"
    else:
        result_status = "BLOCK"
        bounded_apply = "off"
        promotion_execute = "off"
        summary = f"Self-evolving cycle BLOCK — goal={active_goal} — {next_hint}"

    cycle_ended = _utc_iso(datetime.now(timezone.utc))
    history_dir = goals_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"cycle-{cycle_id}.json"
    report_path = reports_dir / f"evolution-{current.strftime('%Y%m%dT%H%M%SZ')}-{cycle_id}.json"
    experiment_id = f"experiment-{cycle_id}"
    experiment_path = experiments_dir / f"{experiment_id}.json"
    # #864: the standalone experiments/contracts/ and experiments/reverts/
    # copies were write-only and are no longer written. The contract/revert
    # dicts live embedded in the experiment record, so the path fields that
    # flow into experiment/report JSON (and the operator CLI's "Experiment
    # contract" line) point at that record — a file that actually exists.
    contract_path = experiment_path
    revert_path = experiment_path
    outbox_path = outbox_dir / "latest.json"
    # previous_experiment already loaded above (R11 lane-switch); reuse it.
    preplan_current_task_id = _derive_experiment_current_task_id(result_status, feedback_decision)
    reward_signal = _derive_reward_signal(result_status, None, preplan_current_task_id, previous_experiment)

    # Reward-hacking guard (issue #565): a materialize-lane cycle with no
    # verified diff must not mint a promotion candidate at all — writing one
    # with null base_commit/candidate_patch_hash just parks dead entries in
    # the promotion queue forever. Non-materialize lanes (record-reward,
    # refresh-approval-gate, etc.) are legitimate low-reward housekeeping and
    # are left unchanged.
    if promotion_candidate_id is not None:
        _materialize_lane_task_ids = {"materialize-pass-streak-improvement", MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID}
        _is_materialize_lane_cycle = preplan_current_task_id in _materialize_lane_task_ids
        if not _is_materialize_lane_cycle and isinstance(recorded_task_plan, dict):
            _recorded_lane_task_id = recorded_task_plan.get("current_task_id") or recorded_task_plan.get("currentTaskId")
            _recorded_lane_feedback = (
                recorded_task_plan.get("feedback_decision")
                if isinstance(recorded_task_plan.get("feedback_decision"), dict)
                else {}
            )
            _is_materialize_lane_cycle = (
                _recorded_lane_task_id in _materialize_lane_task_ids
                or _recorded_lane_feedback.get("selected_task_id") in _materialize_lane_task_ids
            )
        if _is_materialize_lane_cycle and not _has_concrete_changes(
            workspace, state_root=state_root, cycle_started_utc=cycle_started
        ):
            promotion_candidate_id = None

    experiment = _build_experiment_snapshot(
        experiment_id=experiment_id,
        cycle_id=cycle_id,
        goal_id=active_goal,
        result_status=result_status,
        approval_gate_state=approval_gate["state"],
        next_hint=next_hint,
        selected_tasks=selected_tasks,
        task_selection_source=task_selection_source,
        cycle_started_utc=cycle_started,
        cycle_ended_utc=cycle_ended,
        report_path=report_path,
        history_path=history_path,
        outbox_path=outbox_path,
        promotion_candidate_id=promotion_candidate_id,
        review_status=review_status,
        decision=decision,
        reward_signal=reward_signal,
        feedback_decision=feedback_decision,
        previous_experiment=previous_experiment,
        contract_path=contract_path,
        revert_path=revert_path,
    )

    promotion_path = None
    promotion_provenance = None
    if promotion_candidate_id:
        promotions_dir.mkdir(parents=True, exist_ok=True)
        promotion_record = {
            "schema_version": PROMOTION_RECORD_VERSION,
            "promotion_candidate_id": promotion_candidate_id,
            "candidate_created_utc": cycle_ended,
            "origin_cycle_id": cycle_id,
            "origin_host": "local-workspace",
            "source_paths": [str(report_path)],
            "target_repo": "ozand/nanobot",
            "target_branch": "promote/self-evolving",
            "base_commit": None,
            "candidate_patch_hash": None,
            "evidence_refs": [evidence_ref_id],
            "validation_summary": result_status,
            "resource_impact_summary": None,
            "rollback_plan": "Revert the candidate and keep host-local only.",
            "review_status": review_status,
            "decision": decision,
            "experiment_id": experiment_id,
            "budget": experiment["budget"],
            "budget_policy": experiment.get("budget_policy"),
            "budget_used": experiment["budget_used"],
        }
        promotion_path = promotions_dir / f"{promotion_candidate_id}.json"
        promotion_path.write_text(json.dumps(promotion_record, indent=2, ensure_ascii=False), encoding="utf-8")
        (promotions_dir / "latest.json").write_text(
            json.dumps({**promotion_record, "candidate_path": str(promotion_path)}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    artifact_current_task_id = experiment.get("current_task_id")
    if isinstance(recorded_task_plan, dict):
        recorded_current_task_id = recorded_task_plan.get("current_task_id") or recorded_task_plan.get("currentTaskId")
        recorded_feedback = recorded_task_plan.get("feedback_decision") if isinstance(recorded_task_plan.get("feedback_decision"), dict) else {}
        if (
            recorded_current_task_id == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
            or recorded_feedback.get("selected_task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        ):
            artifact_current_task_id = MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID

    runtime_source = _runtime_source_fingerprint(workspace)
    current_plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id=cycle_id,
        goal_id=active_goal,
        result_status=result_status,
        approval_gate_state=approval_gate["state"],
        next_hint=next_hint,
        experiment=experiment,
        report_path=report_path,
        history_path=history_path,
        improvement_score=reward_signal["value"],
        feedback_decision=feedback_decision,
        goals_dir=goals_dir,
        materialized_improvement_artifact_path=_write_materialized_improvement_artifact(
            state_root=state_root,
            cycle_id=cycle_id,
            goal_id=active_goal,
            current_task_id=artifact_current_task_id,
            summary=summary,
            reward_signal=reward_signal,
            feedback_decision=feedback_decision,
            runtime_source=runtime_source,
            workspace=workspace,
        ),
    )
    current_plan_feedback_decision = current_plan.get("feedback_decision") if isinstance(current_plan.get("feedback_decision"), dict) else None
    resolved_feedback_decision = current_plan_feedback_decision or (feedback_decision if isinstance(feedback_decision, dict) else None)
    if resolved_feedback_decision is None and isinstance(recorded_task_plan, dict) and isinstance(recorded_task_plan.get("feedback_decision"), dict):
        resolved_feedback_decision = recorded_task_plan.get("feedback_decision")
    effective_current_task_id = current_plan.get("current_task_id")
    experiment["current_task_id"] = effective_current_task_id
    if resolved_feedback_decision is not None:
        experiment["feedback_decision"] = resolved_feedback_decision
    experiment["reward_signal"] = current_plan.get("reward_signal") if isinstance(current_plan.get("reward_signal"), dict) else reward_signal
    if resolved_feedback_decision is not None and not isinstance(current_plan_feedback_decision, dict):
        current_plan["feedback_decision"] = resolved_feedback_decision
        if not current_plan.get("materialized_improvement_artifact_path") and resolved_feedback_decision.get("artifact_path"):
            current_plan["materialized_improvement_artifact_path"] = resolved_feedback_decision.get("artifact_path")
    artifact_paths = [str(report_path)] if execution_response and result_status == "PASS" else []
    if current_plan.get("materialized_improvement_artifact_path"):
        artifact_path = current_plan.get("materialized_improvement_artifact_path")
        reward = current_plan.get("reward_signal") if isinstance(current_plan.get("reward_signal"), dict) else reward_signal
        upgraded_reward = dict(reward) if isinstance(reward, dict) else {"value": 1.0, "source": "result_status", "result_status": result_status}
        if _has_concrete_changes(workspace, state_root=state_root, cycle_started_utc=cycle_started):
            upgraded_reward["value"] = max(float(upgraded_reward.get("value") or 0.0), 1.2)
            upgraded_reward["source"] = "materialized_improvement_artifact"
        else:
            upgraded_reward["value"] = 0.8
            upgraded_reward["source"] = "metadata_only_improvement_penalty"
        current_plan["reward_signal"] = upgraded_reward
        experiment["reward_signal"] = upgraded_reward
        experiment["metric_current"] = upgraded_reward["value"]
        # Rebase the frontier off the previous cycle's recorded frontier rather
        # than the preliminary (pre-upgrade) frontier computed in
        # _build_experiment_snapshot: that preliminary value was derived from
        # the PASS-default reward (1.0) before the materialization penalty
        # downgraded it, so taking max() against it would permanently ratchet
        # the frontier up regardless of the downgrade — masking the same
        # no-progress condition the stall recompute below depends on (#581).
        _prev_frontier = None
        if isinstance(previous_experiment, dict):
            _prev_frontier_raw = previous_experiment.get("metric_frontier")
            if _prev_frontier_raw is None:
                _prev_frontier_raw = previous_experiment.get("metric_current")
            try:
                _prev_frontier = float(_prev_frontier_raw) if _prev_frontier_raw is not None else None
            except (TypeError, ValueError):
                _prev_frontier = None
        experiment["metric_frontier"] = (
            max(_prev_frontier, upgraded_reward["value"]) if _prev_frontier is not None else upgraded_reward["value"]
        )
        # Re-derive outcome now that metric_current may have been upgraded.
        # Without this, metric_current=1.2 >= metric_baseline=1.2 still shows outcome=discard
        # because outcome was set before the reward upgrade happened.
        _upgraded_baseline = float(experiment.get("metric_baseline") or 0.0)
        _upgraded_current = upgraded_reward["value"]
        if _upgraded_baseline is None or _upgraded_current >= _upgraded_baseline:
            experiment["outcome"] = "keep"
            experiment["revert_required"] = False
        else:
            experiment["outcome"] = "discard"
            experiment["revert_required"] = True
        # R11/R13: re-derive stall + stop_reason now that outcome/metric_current
        # were upgraded above. Without this, the preliminary PASS reward (1.0)
        # always beats the previous cycle's penalized final metric (0.8), so
        # stall_signal keeps seeing "metric advanced" and the no-progress
        # counter (R11) never fires — idle keep-loops never self-terminate
        # (issue #581). Reuse the already-computed budget/budget_used/
        # lane_iteration from the experiment snapshot rather than recomputing
        # them from scratch.
        experiment["stall"] = evaluate_stall(
            result_status=result_status,
            outcome=experiment["outcome"],
            metric_current=experiment["metric_current"],
            metric_frontier=experiment["metric_frontier"],
            previous_experiment=previous_experiment,
        )
        _upgraded_budget_exceeded = budget_exceeded(experiment.get("budget"), experiment.get("budget_used"))
        experiment["stop_reason"] = derive_stop_reason(
            outcome=experiment["outcome"],
            stall=experiment["stall"],
            budget_exceeded=_upgraded_budget_exceeded,
            max_iterations_reached=int(experiment.get("lane_iteration") or 0) >= MAX_ITERATIONS_DEFAULT,
        )
        experiment["budget_used"]["tool_calls"] = max(int(experiment["budget_used"].get("tool_calls") or 0), 2)
        if current_plan.get("current_task_id") == "subagent-verify-materialized-improvement":
            experiment["budget_used"]["subagents"] = max(int(experiment["budget_used"].get("subagents") or 0), 1)
        # #853: require a verified concrete diff before a cycle can ever reach
        # ready-for-policy-review — a metadata-only cycle must not qualify.
        if (current_plan.get("feedback_decision") or {}).get("mode") in {"complete_active_lane", "handoff_to_next_candidate", "handoff_to_subagent_verification"} and _has_concrete_changes(workspace, state_root=state_root, cycle_started_utc=cycle_started):
            experiment["review_status"] = "ready_for_policy_review"
            experiment["decision"] = "ready_for_policy_review"
            experiment["readiness_checks"] = [
                "materialized_improvement_artifact_present",
                "active_lane_completed",
                "reward_signal_upgraded_for_materialization",
            ]
            experiment["readiness_reasons"] = [
                "distinct durable materialized-improvement artifact written",
                "execution lane completed with explicit handoff",
                "artifact-producing lane exceeded baseline reward floor",
            ]
            review_status = "ready_for_policy_review"
            decision = "ready_for_policy_review"

    # Issue #747: the deterministic planner's request-minting lane is deleted.
    # The subagent bridge's LLM proposer (#707) is the sole request source, so
    # the coordinator cycle no longer mints subagent-verify requests here — it
    # only carries any previously recorded request path forward unchanged.
    current_plan["subagent_request_path"] = (
        recorded_task_plan.get("subagent_request_path")
        if isinstance(recorded_task_plan, dict) else None
    )

    subagent_materialization_summary = materialize_subagent_requests(
        state_root=state_root,
        now=_utc_now(now),
        limit=1,
    )
    if subagent_materialization_summary.get("terminalized_count") or subagent_materialization_summary.get("existing_result_count"):
        current_plan["subagent_materialization_summary"] = subagent_materialization_summary
        experiment["subagent_materialization_summary"] = subagent_materialization_summary
        if subagent_materialization_summary.get("terminalized_count"):
            experiment["budget_used"]["subagents"] = max(
                int(experiment["budget_used"].get("subagents") or 0),
                int(subagent_materialization_summary.get("terminalized_count") or 0),
            )
            current_plan["budget_used"] = experiment["budget_used"]
    subagent_rollup = _subagent_rollup_snapshot(
        state_root=state_root,
        current_task_id=current_plan.get("current_task_id"),
        current_task_title=current_plan.get("current_task"),
    )
    subagent_consumption = _subagent_consumption_snapshot(
        state_root=state_root,
        workspace=workspace,
        cycle_id=cycle_id,
        report_path=report_path,
        current_task_id=current_plan.get("current_task_id"),
        tracked_request_path=current_plan.get("subagent_request_path"),
    )
    if subagent_consumption.get("consumed_count"):
        experiment["subagent_consumption"] = subagent_consumption
        experiment["budget_used"]["subagents"] = max(
            int(experiment["budget_used"].get("subagents") or 0),
            int(subagent_consumption.get("budget_subagents") or 0),
        )
        current_plan["subagent_consumption"] = subagent_consumption
        current_plan["budget_used"] = experiment["budget_used"]
    runtime_source = _runtime_source_fingerprint(workspace)
    if promotion_candidate_id and promotion_path is not None:
        final_artifact_path = current_plan.get("materialized_improvement_artifact_path") or ((current_plan.get("feedback_decision") or {}).get("artifact_path") if isinstance(current_plan.get("feedback_decision"), dict) else None)
        promotion_artifact_id = promotion_candidate_id
        promotion_artifact_version = cycle_id
        promotion_release_channel = "self-evolving"
        promotion_target_host_profile = "weak-host"
        promotion_target_authority = "runtime-promotion-policy"
        promotion_deployment_fingerprint_id = f"{promotion_candidate_id}:{cycle_id}"
        promotion_build_recipe = {
            "source_commit": runtime_source.get("source_commit"),
            "origin_cycle_id": cycle_id,
            "candidate_id": promotion_candidate_id,
            "target_branch": "promote/self-evolving",
            "artifact_path": final_artifact_path,
            "release_channel": promotion_release_channel,
        }
        promotion_build_recipe_hash = hashlib.sha256(
            json.dumps(promotion_build_recipe, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        promotion_provenance = {
            "source_commit": runtime_source.get("source_commit"),
            "build_recipe_hash": promotion_build_recipe_hash,
            "artifact_id": promotion_artifact_id,
            "artifact_version": promotion_artifact_version,
            "release_channel": promotion_release_channel,
            "target_host_profile": promotion_target_host_profile,
            "target_authority": promotion_target_authority,
            "deployment_fingerprint": {
                "deployment_fingerprint_id": promotion_deployment_fingerprint_id,
                "artifact_id": promotion_artifact_id,
                "artifact_version": promotion_artifact_version,
                "release_channel": promotion_release_channel,
                "target_host_profile": promotion_target_host_profile,
                "target_authority": promotion_target_authority,
            },
            "rollback_evidence": {
                "evidence_refs": [evidence_ref_id] if evidence_ref_id else [],
                "rollback_plan": "Revert the candidate and keep host-local only.",
            },
        }
        final_promotion_record = {
            "schema_version": PROMOTION_RECORD_VERSION,
            "promotion_candidate_id": promotion_candidate_id,
            "candidate_created_utc": cycle_ended,
            "origin_cycle_id": cycle_id,
            "origin_host": "local-workspace",
            "source_paths": [str(report_path)],
            "target_repo": "ozand/nanobot",
            "target_branch": "promote/self-evolving",
            "base_commit": None,
            "candidate_patch_hash": None,
            "evidence_refs": [evidence_ref_id],
            "validation_summary": result_status,
            "resource_impact_summary": None,
            "rollback_plan": "Revert the candidate and keep host-local only.",
            "review_status": review_status,
            "decision": decision,
            "experiment_id": experiment_id,
            "budget": experiment["budget"],
            "budget_policy": experiment.get("budget_policy"),
            "budget_used": experiment["budget_used"],
            "artifact_path": final_artifact_path,
            "readiness_checks": experiment.get("readiness_checks"),
            "readiness_reasons": experiment.get("readiness_reasons"),
            "decision_record": "pending_operator_review_packet" if review_status == "ready_for_policy_review" else None,
            "accepted_record": None,
            "promotion_provenance": promotion_provenance,
            "governance_packet": {
                "review_packet_status": "pending_operator_review" if review_status == "ready_for_policy_review" else "not_ready",
                "review_status": review_status,
                "decision": decision,
                "source_artifact": final_artifact_path,
                "readiness_checks": experiment.get("readiness_checks"),
                "readiness_reasons": experiment.get("readiness_reasons"),
                "promotion_provenance": promotion_provenance,
            },
        }
        promotion_path.write_text(json.dumps(final_promotion_record, indent=2, ensure_ascii=False), encoding="utf-8")
        (promotions_dir / "latest.json").write_text(
            json.dumps({**final_promotion_record, "candidate_path": str(promotion_path)}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if review_status == "ready_for_policy_review" and final_artifact_path:
            # #853: an "accept" decision requires a real external actor. The
            # autonomous runtime must NOT accept its own promotion candidate —
            # doing so fabricated a false 'reviewed/accepted' audit trail with
            # no verified diff (base_commit/patch_hash=None). The candidate
            # stays pending operator review: the base promotion record written
            # above already carries review_status=ready_for_policy_review and a
            # pending_operator_review governance packet, so we only record the
            # pending decision markers and leave promotions/accepted/ empty.
            decision_record_value = "pending_operator_review_packet"
            accepted_record_value = None
            experiment["decision_record"] = decision_record_value
            experiment["accepted_record"] = accepted_record_value
        elif review_status == "not_ready_for_policy_review" and decision == "not_ready_for_policy_review":
            readiness_result = complete_promotion_readiness_packet(
                workspace=state_root.parent,
                state_root=state_root,
                candidate_id=promotion_candidate_id,
                now=_utc_now(now),
            )
            readiness_inputs_result = supply_missing_promotion_readiness_inputs(
                workspace=state_root.parent,
                state_root=state_root,
                candidate_id=promotion_candidate_id,
                now=_utc_now(now),
            )
            readiness_inputs_supplied = readiness_inputs_result.get("state") == "ready_for_policy_review"
            decision_record_value = "pending_operator_review_packet" if readiness_inputs_supplied else "blocked_not_ready"
            accepted_record_value = None if readiness_inputs_supplied else "not_created_not_ready"
            if readiness_inputs_supplied:
                review_status = "ready_for_policy_review"
                decision = "ready_for_policy_review"
            experiment["review_status"] = review_status
            experiment["decision"] = decision
            experiment["decision_record"] = decision_record_value
            experiment["accepted_record"] = accepted_record_value
            experiment["readiness_packet_path"] = readiness_result.get("readiness_packet_path")
            experiment["readiness_checks"] = readiness_inputs_result.get("readiness_checks")
            experiment["readiness_reasons"] = readiness_inputs_result.get("readiness_reasons")
            experiment["readiness_blocker"] = readiness_inputs_result
            experiment["recommended_next_action"] = readiness_inputs_result.get("recommended_next_action")
            final_promotion_record = {
                **final_promotion_record,
                "review_status": review_status,
                "decision": decision,
                "decision_record": decision_record_value,
                "accepted_record": accepted_record_value,
                "readiness_packet_path": readiness_result.get("readiness_packet_path"),
                "readiness_checks": readiness_inputs_result.get("readiness_checks"),
                "readiness_reasons": readiness_inputs_result.get("readiness_reasons"),
                "readiness_blocker": readiness_inputs_result,
                "recommended_next_action": readiness_inputs_result.get("recommended_next_action"),
                "governance_packet": {
                    **(final_promotion_record.get("governance_packet") if isinstance(final_promotion_record.get("governance_packet"), dict) else {}),
                    "review_packet_status": "pending_operator_review" if readiness_inputs_supplied else "blocked_not_ready",
                    "review_status": review_status,
                    "decision": decision,
                    "decision_record": decision_record_value,
                    "accepted_record": accepted_record_value,
                    "readiness_packet_path": readiness_result.get("readiness_packet_path"),
                    "readiness_blocker": readiness_inputs_result,
                },
            }
            promotion_path.write_text(json.dumps(final_promotion_record, indent=2, ensure_ascii=False), encoding="utf-8")
            (promotions_dir / "latest.json").write_text(
                json.dumps({**final_promotion_record, "candidate_path": str(promotion_path)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    if subagent_consumption.get("result_paths"):
        for subagent_artifact_path in subagent_consumption.get("result_paths") or []:
            if subagent_artifact_path not in artifact_paths:
                artifact_paths.append(subagent_artifact_path)
    report = {
        "cycle_id": cycle_id,
        "cycle_started_utc": cycle_started,
        "cycle_ended_utc": cycle_ended,
        "goal_id": active_goal,
        "current_task_id": current_plan.get("current_task_id"),
        "reward_signal": current_plan.get("reward_signal") if isinstance(current_plan.get("reward_signal"), dict) else reward_signal,
        "tasks": tasks,
        "selected_tasks": selected_tasks,
        "task_selection_source": task_selection_source,
        "result_status": result_status,
        "stop_reason": experiment.get("stop_reason"),
        "stall": experiment.get("stall"),
        "evidence_ref_id": evidence_ref_id,
        "promotion_candidate_id": promotion_candidate_id,
        "review_status": review_status,
        "decision": decision,
        "approval_gate": approval_gate,
        "next_hint": next_hint,
        "bounded_apply": bounded_apply,
        "promotion_execute": promotion_execute,
        "feedback_decision": resolved_feedback_decision,
        "budget": experiment["budget"],
        "budget_policy": experiment.get("budget_policy"),
        "budget_used": experiment["budget_used"],
        "experiment": experiment,
        "experiment_path": str(experiment_path),
        "summary": summary,
        "execution_response": execution_response,
        "execution_error": execution_error,
        "artifact_paths": artifact_paths,
        "subagent_consumption": subagent_consumption,
        "subagent_materialization_summary": current_plan.get("subagent_materialization_summary"),
        "materialized_improvement_artifact_path": current_plan.get("materialized_improvement_artifact_path"),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    _prune_stale_reports(reports_dir)

    outbox = {
        "approval_gate": approval_gate,
        "next_hint": next_hint,
        "summary": summary,
        "current_task_id": current_plan.get("current_task_id"),
        "current_task": current_plan.get("current_task"),
        "selected_tasks": selected_tasks,
        "task_selection_source": task_selection_source,
        "feedback_decision": resolved_feedback_decision,
        "budget": experiment["budget"],
        "budget_policy": experiment.get("budget_policy"),
        "budget_used": experiment["budget_used"],
        "subagent_consumption": subagent_consumption,
        "subagent_materialization_summary": current_plan.get("subagent_materialization_summary"),
        "experiment": experiment,
        "goal": {
            "goal_id": active_goal,
            "text": active_goal,
            "follow_through": {
                "status": "artifact" if execution_response and result_status == "PASS" else "blocked_next_action",
                "blocked_next_step": "" if result_status == "PASS" else next_hint,
                "artifact_paths": artifact_paths,
                "action_summary": summary,
            },
        },
        "latest_report": {
            "cycle_id": cycle_id,
            "goal_id": active_goal,
            "result_status": result_status,
            "evidence_ref_id": evidence_ref_id,
            "promotion_candidate_id": promotion_candidate_id,
            "review_status": review_status,
            "decision": decision,
            "candidate_path": str(promotion_path) if promotion_path else None,
            "summary": summary,
            "report_path": str(report_path),
            "experiment_id": experiment_id,
            "materialized_improvement_artifact_path": current_plan.get("materialized_improvement_artifact_path"),
        },
        "goal_context": {
            "subagent_rollup": subagent_rollup or {
                "enabled": False,
                "count_total": 0,
                "count_done": 0,
                "count_queued": 0,
                "count_completed": 0,
                "count_stale": 0,
            }
        },
    }
    if result_status == "BLOCK":
        outbox["goal"]["follow_through"]["file_action"] = {
            "kind": "file_write",
            "path": "state/approvals/apply.ok",
            "summary": "Write a fresh approval gate with a valid TTL",
        }
        outbox["goal"]["follow_through"]["verification_command"] = "PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py"
    outbox_path.write_text(
        json.dumps(outbox, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    report_index = {
        "ok": result_status != "ERROR",
        "source": str(report_path),
        "status": result_status,
        "current_task_id": current_plan.get("current_task_id"),
        "current_task": current_plan.get("current_task"),
        "selected_tasks": selected_tasks,
        "task_selection_source": task_selection_source,
        "improvement_score": current_plan.get("reward_signal", {}).get("value") if isinstance(current_plan.get("reward_signal"), dict) else reward_signal["value"],
        "budget": experiment["budget"],
        "budget_policy": experiment.get("budget_policy"),
        "budget_used": experiment["budget_used"],
        "subagent_consumption": subagent_consumption,
        "subagent_materialization_summary": current_plan.get("subagent_materialization_summary"),
        "experiment": experiment,
        "feedback_decision": resolved_feedback_decision,
        "goal": {
            "goal_id": active_goal,
            "text": active_goal,
            "follow_through": {
                "status": "artifact" if execution_response and result_status == "PASS" else "blocked_next_action",
                "blocked_next_step": "" if result_status == "PASS" else next_hint,
                "artifact_paths": artifact_paths,
                "action_summary": summary,
            },
        },
        "goal_context": {
            "subagent_rollup": subagent_rollup or {
                "enabled": False,
                "count_total": 0,
                "count_done": 0,
                "count_queued": 0,
                "count_completed": 0,
                "count_stale": 0,
            }
        },
        "capability_gate": {
            "approval": approval_gate,
        },
        "promotion": {
            "promotion_candidate_id": promotion_candidate_id,
            "candidate_path": str(promotion_path) if promotion_path else None,
            "review_status": review_status,
            "decision": decision,
            "promotion_provenance": promotion_provenance,
        },
        "materialized_improvement_artifact_path": current_plan.get("materialized_improvement_artifact_path"),
    }
    if result_status == "BLOCK":
        report_index["goal"]["follow_through"]["file_action"] = {
            "kind": "file_write",
            "path": "state/approvals/apply.ok",
            "summary": "Write a fresh approval gate with a valid TTL",
        }
        report_index["goal"]["follow_through"]["verification_command"] = "PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py"
    report_index_path = outbox_dir / "report.index.json"
    report_index_path.write_text(
        json.dumps(report_index, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    goal_registry = {
        "schema_version": "goal-registry-v1",
        "active_goal_id": active_goal,
        "goals": {
            active_goal: {
                "goal_id": active_goal,
                "status": "active" if result_status != "ERROR" else "error",
                "result_status": result_status,
                "current_task_id": current_plan.get("current_task_id"),
                "current_task": current_plan.get("current_task"),
                "latest_report_path": str(report_path),
                "latest_outbox_path": str(report_index_path),
                "updated_at_utc": cycle_ended,
            }
        },
        "current_task_id": current_plan.get("current_task_id"),
        "current_task": current_plan.get("current_task"),
        "latest_report_path": str(report_path),
        "latest_outbox_path": str(report_index_path),
        "updated_at_utc": cycle_ended,
    }
    (goals_dir / "registry.json").write_text(
        json.dumps(goal_registry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    history_entry = {
        **current_plan,
        "schema_version": "task-history-v1",
        "recorded_at_utc": cycle_ended,
        "report_index_path": str(report_index_path),
        "cycle_started_utc": cycle_started,
        "cycle_ended_utc": cycle_ended,
        "evidence_ref_id": evidence_ref_id,
        "approval_gate": approval_gate,
        "summary": summary,
        "artifact_paths": artifact_paths,
        "reward_signal": reward_signal,
        "current_task_id": experiment.get("current_task_id"),
    }
    (goals_dir / "current.json").write_text(
        json.dumps(current_plan, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    research_feed = _write_research_feed(
        state_root=state_root,
        generated_candidates=current_plan.get("generated_candidates") or [],
        cycle_id=cycle_id,
        goal_id=active_goal,
    )
    hypothesis_backlog = _build_hypothesis_backlog_snapshot(
        cycle_id=cycle_id,
        goal_id=active_goal,
        result_status=result_status,
        approval_gate_state=approval_gate["state"],
        next_hint=next_hint,
        experiment=experiment,
        report_path=report_path,
        history_path=history_path,
        outbox_path=outbox_path,
        task_plan_path=goals_dir / "current.json",
        task_plan=current_plan,
        research_feed=research_feed,
    )
    hypothesis_backlog_path = hypotheses_dir / "backlog.json"
    hypothesis_backlog_path.write_text(
        json.dumps(hypothesis_backlog, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    experiment_record = {
        **experiment,
        "report_path": str(report_path),
        "history_path": str(history_path),
        "outbox_path": str(outbox_path),
        "report_index_path": str(report_index_path),
    }
    # Issue #864: this module used to also write a standalone copy of the
    # contract/revert payload under experiments/contracts/{id}.json and
    # experiments/reverts/{id}.json, plus append every experiment_record to
    # experiments/history.jsonl. All three were write-only — no production
    # code ever opened those files (only the `contract_path`/`revert_path`
    # *string* fields embedded in the alive experiment/report JSON below were
    # ever read, e.g. for CLI display). Deleted per the #864 audit; the
    # embedded contract/revert dicts and path fields are unchanged so
    # experiments/latest.json stays byte-identical.
    experiment_path.write_text(
        json.dumps(experiment_record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (experiments_dir / "latest.json").write_text(
        json.dumps({**experiment_record, "experiment_path": str(experiment_path)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    credits = _write_credits_ledger(
        credits_dir=credits_dir,
        cycle_id=cycle_id,
        goal_id=active_goal,
        result_status=result_status,
        reward_signal=experiment_record.get("reward_signal") if isinstance(experiment_record.get("reward_signal"), dict) else reward_signal,
        budget_used=experiment["budget_used"],
        recorded_at_utc=cycle_ended,
        experiment=experiment_record,
    )
    control_plane_summary_path = _write_control_plane_summary_artifact(
        state_root=state_root,
        cycle_id=cycle_id,
        goal_id=active_goal,
        result_status=result_status,
        approval_gate=approval_gate,
        next_hint=next_hint,
        current_plan=current_plan,
        hypothesis_backlog=hypothesis_backlog,
        experiment_record=experiment_record,
        report_index=report_index,
        report_path=report_path,
        report_index_path=report_index_path,
        credits=credits,
        runtime_source=_runtime_source_fingerprint(workspace),
        prompt_mass=_prompt_mass_snapshot(
            selected_tasks=selected_tasks,
            current_plan=current_plan,
            hypothesis_backlog=hypothesis_backlog,
        ),
        research_feed=hypothesis_backlog.get('research_feed') if isinstance(hypothesis_backlog, dict) else None,
    )
    # Update lessons/errors databases — non-blocking, never raises
    # Extract commits_pushed from bridge subagent results for meaningful lesson recording
    _bridge_commits_pushed = 0
    for _res in (subagent_consumption.get("results") or []):
        _res_path = _res.get("path") or ""
        if _res_path:
            _res_data = _safe_read_json(Path(_res_path)) if _res_path else {}
            if isinstance(_res_data, dict):
                _bridge_commits_pushed = max(_bridge_commits_pushed,
                                            int(_res_data.get("commits_pushed") or 0))
    _lessons_result = update_lessons_from_cycle(
        workspace=workspace,
        result_status=result_status,
        current_task_id=current_plan.get("current_task_id"),
        summary=summary,
        artifact_paths=artifact_paths,
        reward_signal=experiment.get("reward_signal") if isinstance(experiment.get("reward_signal"), dict) else reward_signal,
        feedback_decision=resolved_feedback_decision,
        cycle_id=cycle_id,
        recorded_at=cycle_ended,
        commits_pushed=_bridge_commits_pushed,
    )
    if _lessons_result.get("action") not in ("skipped", "error"):
        history_entry["lessons_update"] = _lessons_result

    history_path.write_text(
        json.dumps(history_entry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Population archive — add this cycle, detect stall (issue #529)
    try:
        from nanobot.runtime.archive import CycleArchive
        _archive = CycleArchive()
        _archive_path = state_root / "goals" / "cycle_archive.json"
        _archive.load(_archive_path)
        _reward_val = 0.0
        if isinstance(reward_signal, dict):
            try:
                _reward_val = float(reward_signal.get("value") or 0.0)
            except Exception:
                pass
        _archive.add(
            cycle_id=cycle_id,
            reward=_reward_val,
            fd_mode=str((resolved_feedback_decision or {}).get("mode") or ""),
            task_id=str(current_plan.get("current_task_id") or ""),
            commits_pushed=_bridge_commits_pushed,
        )
        if _archive.stalled():
            _logger.warning(
                "[archive] stalled — last %d cycles all have reward < %.1f; "
                "consider sampling diverse parents or introducing a new hypothesis",
                5, 0.8,
            )
        _archive.save(_archive_path)
    except Exception:
        pass  # archive is non-blocking — never affects primary coordinator flow

    return summary
