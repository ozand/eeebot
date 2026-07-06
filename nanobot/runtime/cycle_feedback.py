"""Cycle Feedback phase: reward/experiment/ambition feedback-decision pipeline.

Extracted from coordinator.py (issue #600). Holds ``_derive_feedback_decision``
and the helpers feeding it: approval-gate/active-goal state, experiment
contract/budget/snapshot construction, and ambition-underutilization
history analysis. Depends on nanobot.runtime.cycle_observe for shared
task/insight predicates and constants. No behavior change from the move.
"""

from __future__ import annotations

import heapq
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime._io import read_json_safe as _safe_read_json
from nanobot.runtime._io import utc_iso as _utc_iso
from nanobot.runtime._io import utc_now as _utc_now
from nanobot.runtime.cycle_observe import (
    _BACKLOG_PROGRESSION_IDS,
    AMBITION_UNDERUTILIZATION_STREAK_LIMIT,
    COMPLETED_TASK_STATUSES,
    CORE_TASK_IDS,
    DEFAULT_ACTIVE_GOAL,
    DEFAULT_EXPERIMENT_BUDGET,
    EXPANDED_EXPERIMENT_BUDGET,
    EXPERIMENT_BUDGET_HARD_CEILING,
    EXPERIMENT_CONTRACT_VERSION,
    EXPERIMENT_VERSION,
    GOAL_ROTATION_STREAK_LIMIT,
    KNOWN_TASK_IDS,
    LOW_REWARD_THRESHOLD,
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
    REPEATED_BLOCK_LIMIT,
    SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
    _insight_is_actionable,
    _json_files_sorted_by_mtime,
    _load_recent_history_entries,
    _next_open_goal_hypothesis,
    _normalize_artifact_paths,
    _parse_datetime,
    _pick_task_for_classes,
    _render_task_selection,
    _retire_orphaned_task_ids,
    _select_insight_for_goal,
    _task_action_class,
    _task_is_selectable,
    _task_status,
)
from nanobot.runtime.stop_guards import (
    MAX_ITERATIONS_DEFAULT,
    budget_exceeded,
    derive_stop_reason,
    evaluate_stall,
    lane_iteration,
)


def _enrich_decision_lane_with_insight(
    decision: dict[str, Any] | None,
    workspace: Path | None,
    goal_id: str | None,
) -> dict[str, Any] | None:
    """Make a selected synthesize/materialize lane insight-derived (HADI I->H).

    The lane the subagent actually executes comes from the feedback decision's
    selected_task (title -> selected_task_label -> _derive_bounded_tasks_from_plan).
    _derive_feedback_decision builds that lane from a generic template via many
    return paths, so we enrich the returned decision in one place: if it selected a
    synthesize/materialize lane whose title is not already insight-derived, rebuild
    the candidate with the best available insight and overwrite title/label so the
    executed lane carries a concrete hypothesis instead of a template. No insight
    available (e.g. no lessons) -> decision unchanged (backward-compatible).
    """
    if not isinstance(decision, dict) or workspace is None:
        return decision
    task_id = decision.get("selected_task_id")
    if task_id not in {MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID, SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID}:
        return decision
    if "insight:" in str(decision.get("selected_task_title") or "").lower():
        return decision  # already insight-derived
    insight = _select_insight_for_goal(workspace, goal_id if isinstance(goal_id, str) else None)
    # If the best lesson insight is vague (no concrete file target), prefer the top
    # open goal from todo.md so the materialize lane targets OUR goals (autoresearch
    # concrete-target style) instead of a non-actionable meta-lesson.
    if not _insight_is_actionable(insight):
        goal_hypothesis = _next_open_goal_hypothesis(workspace)
        if goal_hypothesis:
            insight = goal_hypothesis
    if not insight:
        return decision
    factory = (
        _synthesized_materialize_improvement_candidate
        if task_id == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        else _synthesized_next_improvement_candidate
    )
    signature = decision.get("goal_artifact_signature")
    candidate = factory(
        current_task_id=decision.get("current_task_id"),
        strong_pass_count=int(decision.get("strong_pass_count") or 0),
        goal_artifact_signature=signature if isinstance(signature, list) else None,
        status="active",
        insight=insight,
    )
    enriched = dict(decision)
    enriched["selected_task_id"] = candidate["task_id"]
    enriched["selected_task_class"] = _task_action_class(candidate["task_id"])
    enriched["selected_task_title"] = candidate["title"]
    enriched["selected_task_label"] = _render_task_selection(candidate)
    return enriched


def _synthesized_next_improvement_candidate(
    *,
    current_task_id: str | None,
    strong_pass_count: int,
    goal_artifact_signature: list[str] | None,
    status: str = "pending",
    insight: str | None = None,
) -> dict[str, Any]:
    insight_text = (insight or "").strip()
    if insight_text:
        title = f"Synthesize a bounded improvement candidate from insight: {insight_text[:80]}"
        acceptance = (
            f'Act on the accumulated insight "{insight_text[:200]}": produce one new bounded '
            "improvement candidate that is not a retired terminal/completed lane"
        )
    else:
        title = "Synthesize one new bounded improvement candidate from retired lanes"
        acceptance = (
            "produce one new bounded improvement candidate that is not a retired "
            "terminal/completed lane"
        )
    candidate: dict[str, Any] = {
        "task_id": SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
        "title": title,
        "status": status,
        "kind": "review",
        "acceptance": acceptance,
        "selection_source": "feedback_no_selectable_retired_lane_synthesis",
        "parent_task_id": current_task_id,
        "strong_pass_count": strong_pass_count,
        "goal_artifact_signature": goal_artifact_signature,
    }
    if insight_text:
        candidate["derived_from_insight"] = insight_text[:300]
    return candidate


def _synthesized_materialize_improvement_candidate(
    *,
    current_task_id: str | None,
    strong_pass_count: int,
    goal_artifact_signature: list[str] | None,
    status: str = "pending",
    insight: str | None = None,
) -> dict[str, Any]:
    insight_text = (insight or "").strip()
    if insight_text:
        title = f"Materialize a bounded improvement from insight: {insight_text[:80]}"
        acceptance = (
            f'Act on the accumulated insight "{insight_text[:200]}": write a concrete bounded '
            "HADI improvement proposal or artifact (hypothesis, action, data, insight) and "
            "route it into self-evolution"
        )
        hypothesis = (
            f"Acting on insight '{insight_text[:120]}' will produce a concrete bounded "
            "improvement and break the reward/candidate discard loop."
        )
        data = (
            "Use the accumulated reusable insight from lessons plus recent task history, "
            "experiment outcome, and budget/subagent utilization evidence."
        )
    else:
        title = "Materialize one bounded improvement from the synthesized candidate"
        acceptance = (
            "write a concrete bounded HADI improvement proposal or artifact (hypothesis, "
            "action, data, insight) and route it into self-evolution"
        )
        hypothesis = "A concrete bounded materialization will break the reward/candidate discard loop."
        data = "Use recent task history, experiment outcome, and budget/subagent utilization evidence."
    candidate: dict[str, Any] = {
        "task_id": MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
        "title": title,
        "status": status,
        "kind": "execution",
        "acceptance": acceptance,
        "selection_source": "feedback_synthesis_materialization",
        "parent_task_id": current_task_id,
        "strong_pass_count": strong_pass_count,
        "goal_artifact_signature": goal_artifact_signature,
        "hadi_required": True,
        "hadi_cycle": {
            "hypothesis": hypothesis,
            "action": "Create one reviewable artifact or follow-up task with explicit acceptance checks.",
            "data": data,
            "insight": "Decide whether the artifact should be accepted, escalated to subagent review, or blocked with a concrete reason.",
        },
        "task_readiness": _task_readiness_contract(
            definition_of_ready=[
                "HADI hypothesis/action/data/insight is attached to the task",
                "acceptance describes a durable artifact and next routing decision",
                "budget/subagent utilization evidence is available for selection pressure",
            ],
            definition_of_done=[
                "materialized artifact is written under state/improvements",
                "artifact includes HADI metadata and feedback decision",
                "lane routes to subagent verification or records an explicit blocker",
            ],
        ),
    }
    if insight_text:
        candidate["derived_from_insight"] = insight_text[:300]
    return candidate


def _task_readiness_contract(*, definition_of_ready: list[str], definition_of_done: list[str], hadi_required: bool = True) -> dict[str, Any]:
    return {
        "schema_version": "hadi-dor-dod-readiness-v1",
        "state": "ready",
        "hadi_required": hadi_required,
        "definition_of_ready": definition_of_ready,
        "definition_of_done": definition_of_done,
    }


def _task_readiness_gate(task: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(task, dict):
        return {"state": "blocked", "reasons": ["task_missing"]}
    task_id = str(task.get("task_id") or task.get("taskId") or "")
    task_kind = str(task.get("kind") or _task_action_class(task_id))
    requires_gate = bool(task.get("hadi_required")) or task_id == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID or task_kind == "execution"
    if not requires_gate:
        return {"state": "ready", "reasons": []}
    readiness = task.get("task_readiness") if isinstance(task.get("task_readiness"), dict) else {}
    reasons: list[str] = []
    if readiness.get("schema_version") != "hadi-dor-dod-readiness-v1":
        reasons.append("readiness_schema_missing")
    if readiness.get("state") != "ready":
        reasons.append("readiness_state_not_ready")
    dor = readiness.get("definition_of_ready")
    dod = readiness.get("definition_of_done")
    if not isinstance(dor, list) or not any(str(item).strip() for item in dor):
        reasons.append("definition_of_ready_missing")
    if not isinstance(dod, list) or not any(str(item).strip() for item in dod):
        reasons.append("definition_of_done_missing")
    if task.get("hadi_required") and not isinstance(task.get("hadi_cycle"), dict):
        reasons.append("hadi_cycle_missing")
    return {"state": "ready" if not reasons else "blocked", "reasons": reasons, "schema_version": readiness.get("schema_version")}


def _clamp_experiment_budget(budget: dict[str, Any]) -> dict[str, Any]:
    clamped: dict[str, Any] = {}
    for key, floor_value in DEFAULT_EXPERIMENT_BUDGET.items():
        value = budget.get(key, floor_value)
        ceiling = EXPERIMENT_BUDGET_HARD_CEILING.get(key, value)
        try:
            numeric_value = int(value)
        except Exception:
            numeric_value = int(floor_value)
        clamped[key] = max(int(floor_value), min(numeric_value, int(ceiling)))
    return clamped


def _derive_experiment_budget_policy(
    *,
    result_status: str,
    current_task_id: str,
    selected_tasks: str,
    task_selection_source: str,
    feedback_decision: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Choose a bounded experiment envelope instead of one fixed budget.

    The conservative 2/12/2 envelope remains the floor for blocked and
    bookkeeping/reflection cycles. Higher-ambition execution or subagent lanes
    can spend more of the available cycle envelope, but the subagent ceiling is
    intentionally clamped at five.
    """

    task_class = _task_action_class(current_task_id)
    mode = str(feedback_decision.get("mode") or "") if isinstance(feedback_decision, dict) else ""
    selected_id = str(feedback_decision.get("selected_task_id") or "") if isinstance(feedback_decision, dict) else ""
    policy_inputs = " ".join(
        part for part in (current_task_id, selected_tasks, task_selection_source, mode, selected_id, task_class) if part
    )
    ambitious_markers = (
        "materialize-synthesized-improvement",
        "subagent-verify-materialized-improvement",
        "synthesize-next-improvement-candidate",
        "analyze-last-failed-candidate",
        "generated_from_synthesized_improvement",
        "feedback_synthesis_materialization",
        "handoff_to_subagent_verification",
    )
    is_ambitious = result_status != "BLOCK" and (
        task_class in {"execution", "bounded_apply", "fix"}
        or any(marker in policy_inputs for marker in ambitious_markers)
    )
    if is_ambitious:
        budget = _clamp_experiment_budget(dict(EXPANDED_EXPERIMENT_BUDGET))
        tier = "expanded"
        reason = "higher_ambition_execution_or_subagent_lane"
    else:
        budget = dict(DEFAULT_EXPERIMENT_BUDGET)
        tier = "conservative"
        reason = "blocked_or_bookkeeping_lane"
    policy = {
        "schema_version": "experiment-budget-policy-v1",
        "tier": tier,
        "reason": reason,
        "task_class": task_class,
        "selected_task_id": selected_id or current_task_id,
        "selection_source": task_selection_source,
        "floor": dict(DEFAULT_EXPERIMENT_BUDGET),
        "hard_ceiling": dict(EXPERIMENT_BUDGET_HARD_CEILING),
    }
    return budget, policy


def _history_budget_used(history_entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(history_entry, dict):
        return {}
    budget_used = history_entry.get("budget_used")
    if isinstance(budget_used, dict):
        return budget_used
    experiment = history_entry.get("experiment")
    if isinstance(experiment, dict) and isinstance(experiment.get("budget_used"), dict):
        return experiment.get("budget_used") or {}
    detail = history_entry.get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("budget_used"), dict):
        return detail.get("budget_used") or {}
    return {}


def _ambition_streak_key(task_id: str | None) -> str | None:
    if not task_id:
        return None
    normalized = str(task_id)
    if normalized in {"record-reward", "inspect-pass-streak", SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID}:
        return "synthesized-reward-loop"
    return normalized


def _history_experiment_outcome(history_entry: dict[str, Any]) -> str | None:
    if not isinstance(history_entry, dict):
        return None
    experiment = history_entry.get("experiment")
    if isinstance(experiment, dict) and experiment.get("outcome"):
        return str(experiment.get("outcome"))
    detail = history_entry.get("detail")
    if isinstance(detail, dict):
        detail_experiment = detail.get("experiment")
        if isinstance(detail_experiment, dict) and detail_experiment.get("outcome"):
            return str(detail_experiment.get("outcome"))
        if detail.get("outcome"):
            return str(detail.get("outcome"))
    if history_entry.get("outcome"):
        return str(history_entry.get("outcome"))
    return None


def _ambition_underutilization_reasons(history_entries: list[dict[str, Any]], current_task_id: str | None) -> list[str]:
    if not current_task_id:
        return []
    current_streak_key = _ambition_streak_key(current_task_id)
    if not current_streak_key:
        return []
    inspected = 0
    repeated_task_ids: list[str] = []
    raw_task_ids: list[str] = []
    outcomes: list[str | None] = []
    total_tool_calls = 0
    total_subagents = 0
    total_elapsed_seconds = 0
    for entry in history_entries[:AMBITION_UNDERUTILIZATION_STREAK_LIMIT]:
        if (entry.get("result_status") or entry.get("status")) != "PASS":
            break
        task_id = entry.get("current_task_id") or entry.get("currentTaskId")
        task_streak_key = _ambition_streak_key(str(task_id) if task_id else None)
        if not task_streak_key:
            break
        repeated_task_ids.append(task_streak_key)
        raw_task_ids.append(str(task_id) if task_id else "unknown")
        outcomes.append(_history_experiment_outcome(entry))
        budget_used = _history_budget_used(entry)
        total_tool_calls += int(budget_used.get("tool_calls") or 0)
        total_subagents += int(budget_used.get("subagents") or 0)
        total_elapsed_seconds += int(budget_used.get("elapsed_seconds") or 0)
        inspected += 1
    if inspected < AMBITION_UNDERUTILIZATION_STREAK_LIMIT:
        return []
    if len(set(repeated_task_ids)) != 1 or repeated_task_ids[0] != current_streak_key:
        return []
    reasons = ["same_task_streak"]
    if len(set(raw_task_ids)) <= 2:
        reasons.append("low_task_diversity")
    if outcomes and all(outcome == "discard" for outcome in outcomes):
        reasons.append("recent_window_discard_only")
    if total_subagents == 0:
        reasons.append("subagents_unused")
    if total_tool_calls <= inspected * 2:
        reasons.append("tool_budget_underused")
    if total_elapsed_seconds <= inspected:
        reasons.append("time_budget_underused")
    if reasons == ["same_task_streak"]:
        return []
    return reasons


def _history_failure_class(history_entry: dict[str, Any]) -> str | None:
    result_status = history_entry.get("result_status") or history_entry.get("status")
    if result_status == "BLOCK":
        approval_gate = history_entry.get("approval_gate") if isinstance(history_entry.get("approval_gate"), dict) else None
        gate_state = None
        if isinstance(approval_gate, dict):
            gate_state = approval_gate.get("state") or approval_gate.get("status") or approval_gate.get("reason")
        next_hint = history_entry.get("next_hint") or history_entry.get("nextHint")
        normalized_gate = gate_state or next_hint or "unknown"
        return f"approval:{normalized_gate}"
    if result_status == "ERROR":
        execution_error = history_entry.get("execution_error") or history_entry.get("executionError") or history_entry.get("summary")
        if execution_error:
            return f"execution:{str(execution_error).split(':', 1)[0]}"
        return "execution:unknown"
    return None


def _bridge_handled_request_ids(state_root: Path) -> set[str]:
    """Return set of request_ids that were actually handled by the bridge LLM executor.

    The bridge writes handled_<safe_id>.txt markers and result files with
    materialized_from=bridge_llm_execution. The coordinator uses this to
    avoid marking subagents_unused when the bridge already ran a subagent.
    """
    handled: set[str] = set()
    bridge_state_dir = state_root / "subagent_bridge"
    if bridge_state_dir.is_dir():
        for marker in bridge_state_dir.glob("handled_*.txt"):
            # marker name encodes safe request_id: handled_<safe_id>.txt
            stem = marker.stem[len("handled_"):]
            handled.add(stem)
            try:
                content = marker.read_text(encoding="utf-8").strip()
                if content:
                    handled.add(content)
            except Exception:
                pass
    # Also scan results/ for bridge_llm_execution entries
    results_dir = state_root / "subagents" / "results"
    if results_dir.is_dir():
        for rp in results_dir.glob("*.json"):
            try:
                rd = json.loads(rp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(rd.get("materialized_from") or "") == "bridge_llm_execution":
                if rid := rd.get("request_id") or rd.get("verification_task_id"):
                    handled.add(str(rid))
    return handled


def _orphaned_current_task_switch(
    task_records: list[dict[str, Any]],
    *,
    current_task_id: str,
    current_task_class: str,
    reward_value: Any,
    repeat_block_count: int,
    repeat_block_failure_class: str | None,
    strong_pass_signature_list: list[str] | None,
    strong_pass_count: int,
) -> dict[str, Any] | None:
    """Issue #580 follow-up helper for _derive_feedback_decision.

    current_task_id itself can be an orphan left behind by removed code
    (e.g. a live cycle already made it "active" before the repair pass
    ran). Never let a continue/streak branch keep it selected — switch
    off it here, before any other branch gets a chance. Returns a
    switch-lane feedback decision if a selectable alternative exists, or
    None if the caller should fall through to the generic branches below
    (extracted verbatim from _derive_feedback_decision; no logic change).
    """
    _orphan_sorted_candidates = sorted(
        task_records,
        key=lambda t: 0
        if isinstance(t, dict) and (t.get("task_id") or t.get("taskId")) in _BACKLOG_PROGRESSION_IDS
        else 1,
    )
    orphan_alternative: dict[str, Any] | None = None
    for _candidate in _orphan_sorted_candidates:
        if not isinstance(_candidate, dict):
            continue
        _candidate_id = _candidate.get("task_id") or _candidate.get("taskId")
        if not _candidate_id or _candidate_id == current_task_id:
            continue
        if _task_is_selectable(_candidate):
            orphan_alternative = _candidate
            break
    if orphan_alternative is None:
        return None
    _alt_id = orphan_alternative.get("task_id") or orphan_alternative.get("taskId")
    return {
        "mode": "switch_stalled_lane",
        "reason": f"current_task_id {current_task_id!r} is not a known/producible task_id (orphaned); switched to {_alt_id}",
        "reward_value": reward_value,
        "current_task_id": current_task_id,
        "current_task_class": current_task_class,
        "repeat_block_count": repeat_block_count,
        "repeat_block_failure_class": repeat_block_failure_class,
        "goal_artifact_signature": strong_pass_signature_list,
        "strong_pass_count": strong_pass_count,
        "retire_goal_artifact_pair": False,
        "selected_task_id": _alt_id,
        "selected_task_class": _task_action_class(_alt_id),
        "selection_source": "orphaned_current_task_retired",
        "selected_task_title": orphan_alternative.get("title") or orphan_alternative.get("summary") or _alt_id,
        "selected_task_label": _render_task_selection(orphan_alternative),
    }


def _derive_feedback_decision(task_plan: dict[str, Any] | None, goals_dir: Path, state_root: Path | None = None) -> dict[str, Any] | None:
    if not isinstance(task_plan, dict):
        return None

    history_entries = _load_recent_history_entries(goals_dir / "history", limit=max(4, AMBITION_UNDERUTILIZATION_STREAK_LIMIT))
    latest_history = history_entries[0] if history_entries else None
    reward_signal = task_plan.get("reward_signal") if isinstance(task_plan.get("reward_signal"), dict) else None
    reward_value = None
    if isinstance(reward_signal, dict):
        reward_value = reward_signal.get("value")

    current_task_id = task_plan.get("current_task_id") or task_plan.get("currentTaskId")
    current_task_class = _task_action_class(current_task_id if isinstance(current_task_id, str) else None)
    tasks = task_plan.get("tasks") if isinstance(task_plan.get("tasks"), list) else []
    task_records = [task for task in tasks if isinstance(task, dict)]
    _retire_orphaned_task_ids(task_records)
    _task_by_id: dict[str, dict[str, Any]] = {}
    for _tr in task_records:
        _tid = _tr.get("task_id") or _tr.get("taskId")
        if _tid and _tid not in _task_by_id:
            _task_by_id[str(_tid)] = _tr
    recorded_feedback_decision = task_plan.get("feedback_decision") if isinstance(task_plan.get("feedback_decision"), dict) else None

    ambition_underutilization_reasons = _ambition_underutilization_reasons(history_entries, current_task_id if isinstance(current_task_id, str) else None)
    # If the bridge already handled a subagent request, remove subagents_unused from reasons
    # so the coordinator doesn't escalate ambition unnecessarily.
    if "subagents_unused" in ambition_underutilization_reasons and state_root is not None:
        bridge_handled = _bridge_handled_request_ids(state_root)
        if bridge_handled:
            ambition_underutilization_reasons = [r for r in ambition_underutilization_reasons if r != "subagents_unused"]

    if (
        isinstance(recorded_feedback_decision, dict)
        and recorded_feedback_decision.get("mode") in {"retire_terminal_selfevo_lane", "retire_terminal_noop_lane", "retire_stale_subagent_lane", "retire_completed_subagent_lane"}
        and recorded_feedback_decision.get("current_task_id") == current_task_id
        and recorded_feedback_decision.get("selected_task_id")
        and recorded_feedback_decision.get("selected_task_id") != current_task_id
    ):
        return recorded_feedback_decision

    repeat_block_failure_class = None
    repeat_block_count = 0
    if latest_history and (latest_history.get("result_status") or latest_history.get("status")) == "BLOCK":
        latest_failure_class = _history_failure_class(latest_history)
        if latest_failure_class:
            repeat_block_failure_class = latest_failure_class
            for entry in history_entries:
                if (entry.get("result_status") or entry.get("status")) != "BLOCK":
                    break
                if _history_failure_class(entry) != latest_failure_class:
                    break
                repeat_block_count += 1

    strong_pass_signature = None
    strong_pass_count = 0
    if latest_history and (latest_history.get("result_status") or latest_history.get("status")) == "PASS":
        strong_pass_signature = _extract_history_signature(latest_history)
        if strong_pass_signature is not None:
            for entry in history_entries:
                if (entry.get("result_status") or entry.get("status")) != "PASS":
                    break
                if _extract_history_signature(entry) != strong_pass_signature:
                    break
                strong_pass_count += 1

    # Precompute the list form of strong_pass_signature once; it is reused
    # in every feedback-decision branch below (18+ sites).
    strong_pass_signature_list: list[str] | None = (
        list(str(value) for value in strong_pass_signature)
        if strong_pass_signature is not None
        else None
    )

    # Issue #580 follow-up: current_task_id itself can be an orphan left behind
    # by removed code (e.g. a live cycle already made it "active" before the
    # repair pass ran). Never let a continue/streak branch below keep it
    # selected — switch off it here, before any other branch gets a chance.
    if isinstance(current_task_id, str) and current_task_id and current_task_id not in KNOWN_TASK_IDS:
        orphan_switch = _orphaned_current_task_switch(
            task_records,
            current_task_id=current_task_id,
            current_task_class=current_task_class,
            reward_value=reward_value,
            repeat_block_count=repeat_block_count,
            repeat_block_failure_class=repeat_block_failure_class,
            strong_pass_signature_list=strong_pass_signature_list,
            strong_pass_count=strong_pass_count,
        )
        if orphan_switch is not None:
            return orphan_switch
        # No selectable alternative exists — fall through to the existing
        # logic below rather than crash; the generic branches will still see
        # _task_by_id.get(current_task_id) resolve to the (now-retired)
        # orphan record, but _task_is_selectable's KNOWN_TASK_IDS check keeps
        # any downstream selection logic from treating it as selectable.

    selected_task: dict[str, Any] | None = None
    selection_source = "recorded_current_task"
    mode = "stable"
    reason = ""
    # ambition_underutilization_reasons already computed above (with bridge-handled filter)

    latest_experiment = _safe_read_json(goals_dir.parent / "experiments" / "latest.json")
    latest_experiment_task_id = latest_experiment.get("current_task_id") if isinstance(latest_experiment, dict) else None
    latest_experiment_revert_queued = (
        isinstance(latest_experiment, dict)
        and latest_experiment.get("outcome") == "discard"
        and latest_experiment.get("revert_required") is True
        and latest_experiment.get("revert_status") == "queued"
        and latest_experiment_task_id == current_task_id
    )
    materialized_artifact_path = task_plan.get("materialized_improvement_artifact_path")
    materialized_artifact_payload = _safe_read_json(Path(str(materialized_artifact_path))) if materialized_artifact_path else None
    materialize_task = _task_by_id.get(MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID)
    # Precompute materialize task status once; reused in three cascading branches below.
    _materialize_task_status = _task_status(materialize_task)
    _materialize_task_completed = _materialize_task_status in COMPLETED_TASK_STATUSES
    post_materialization_reward_already_confirmed = (
        current_task_id == "record-reward"
        and isinstance(recorded_feedback_decision, dict)
        and recorded_feedback_decision.get("mode") == "record_reward_after_synthesized_materialization"
        and recorded_feedback_decision.get("selected_task_id") == "record-reward"
        and (
            not materialized_artifact_path
            or not recorded_feedback_decision.get("artifact_path")
            or str(recorded_feedback_decision.get("artifact_path")) == str(materialized_artifact_path)
        )
    )
    if (
        current_task_id == "record-reward"
        and isinstance(materialized_artifact_payload, dict)
        and materialized_artifact_payload.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        and _materialize_task_completed
        and post_materialization_reward_already_confirmed
        and {"recent_window_discard_only", "subagents_unused"}.issubset(set(ambition_underutilization_reasons))
    ):
        selected_task = _synthesized_materialize_improvement_candidate(
            current_task_id=current_task_id,
            strong_pass_count=strong_pass_count,
            goal_artifact_signature=strong_pass_signature_list,
            status="active",
        )
        return {
            "mode": "escalate_underutilized_ambition",
            "reason": "HADI escalation: recent reward/candidate cycles are discard-only and resource/subagent budgets are underused; materialize a stronger bounded experiment instead of repeating reward/synthesis bookkeeping",
            "reward_value": reward_value,
            "current_task_id": current_task_id,
            "current_task_class": current_task_class,
            "repeat_block_count": repeat_block_count,
            "repeat_block_failure_class": repeat_block_failure_class,
            "goal_artifact_signature": strong_pass_signature_list,
            "strong_pass_count": strong_pass_count,
            "retire_goal_artifact_pair": False,
            "ambition_escalation": {
                "state": "selected",
                "blocker": None,
                "schema_version": "hadi-ambition-escalation-v1",
                "strategy": "hadi_materialize_after_discard_only_underuse",
                "reasons": ambition_underutilization_reasons,
                "hypothesis": "A HADI materialization lane will break the discard-only reward/candidate loop.",
                "action": "Select a concrete materialization task with explicit hypothesis/action/data/insight evidence.",
                "data": {
                    "recent_window_size": AMBITION_UNDERUTILIZATION_STREAK_LIMIT,
                    "current_task_id": current_task_id,
                    "materialized_artifact_path": str(materialized_artifact_path),
                },
                "insight": "Reward bookkeeping is already confirmed; further progress requires a fresh materialized experiment or explicit blocker.",
            },
            "selected_task_id": selected_task.get("task_id") or selected_task.get("taskId"),
            "selected_task_class": _task_action_class(selected_task.get("task_id") or selected_task.get("taskId")),
            "selection_source": "feedback_hadi_discard_loop_materialize",
            "selected_task_title": selected_task.get("title") or selected_task.get("summary") or selected_task.get("task_id"),
            "selected_task_label": _render_task_selection(selected_task),
        }
    if (
        current_task_id == "record-reward"
        and isinstance(materialized_artifact_payload, dict)
        and materialized_artifact_payload.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        and _materialize_task_completed
        and post_materialization_reward_already_confirmed
    ):
        selected_task = _synthesized_next_improvement_candidate(
            current_task_id=current_task_id,
            strong_pass_count=strong_pass_count,
            goal_artifact_signature=strong_pass_signature_list,
            status="active",
        )
        return {
            "mode": "synthesize_next_candidate",
            "reason": "post-materialization reward accounting is already confirmed; rotate to a fresh bounded improvement candidate instead of repeating reward bookkeeping",
            "reward_value": reward_value,
            "current_task_id": current_task_id,
            "current_task_class": current_task_class,
            "repeat_block_count": repeat_block_count,
            "repeat_block_failure_class": repeat_block_failure_class,
            "goal_artifact_signature": strong_pass_signature_list,
            "strong_pass_count": strong_pass_count,
            "retire_goal_artifact_pair": False,
            "ambition_escalation": None,
            "selected_task_id": selected_task.get("task_id") or selected_task.get("taskId"),
            "selected_task_class": _task_action_class(selected_task.get("task_id") or selected_task.get("taskId")),
            "selection_source": "feedback_no_selectable_retired_lane_synthesis",
            "selected_task_title": selected_task.get("title") or selected_task.get("summary") or selected_task.get("task_id"),
            "selected_task_label": _render_task_selection(selected_task),
        }
    if (
        current_task_id == "record-reward"
        and isinstance(materialized_artifact_payload, dict)
        and materialized_artifact_payload.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        and _materialize_task_completed
    ):
        selected_task = _task_by_id.get("record-reward") or {"task_id": "record-reward", "title": "Record cycle reward"}
        return {
            "mode": "record_reward_after_synthesized_materialization",
            "reason": "synthesized materialization artifact is already completed; prioritize post-materialization reward accounting before ambition escalation",
            "reward_value": reward_value,
            "current_task_id": "record-reward",
            "current_task_class": _task_action_class("record-reward"),
            "repeat_block_count": repeat_block_count,
            "repeat_block_failure_class": repeat_block_failure_class,
            "goal_artifact_signature": strong_pass_signature_list,
            "strong_pass_count": strong_pass_count,
            "retire_goal_artifact_pair": False,
            "selected_task_id": "record-reward",
            "selected_task_class": _task_action_class("record-reward"),
            "selection_source": "feedback_synthesized_materialization_complete_reward",
            "selected_task_title": selected_task.get("title") or "Record cycle reward",
            "selected_task_label": _render_task_selection(selected_task),
            "artifact_path": str(materialized_artifact_path),
        }

    if latest_experiment_revert_queued:
        mode = "execute_queued_revert"
        reason = "latest experiment discarded the active lane and queued revert follow-through"
        for task in task_records:
            task_id = task.get("task_id") or task.get("taskId")
            if task_id in {None, current_task_id, "record-reward"}:
                continue
            if (task.get("status") or "pending") in {"pending", "active"}:
                selected_task = task
                selection_source = "feedback_discard_revert_followthrough"
                break
        if selected_task is None:
            selected_task = {
                "task_id": "execute-queued-revert",
                "title": "Handle queued revert for discarded experiment lane",
                "status": "active",
                "kind": "remediation",
                "discarded_task_id": current_task_id,
                "experiment_id": latest_experiment.get("experiment_id") if isinstance(latest_experiment, dict) else None,
            }
            selection_source = "feedback_discard_revert_generated"
    elif ambition_underutilization_reasons:
        materialize_task = _task_by_id.get(MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID)
        if current_task_id == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID:
            if materialize_task is None or _task_is_selectable(materialize_task):
                selected_task = materialize_task or _synthesized_materialize_improvement_candidate(
                    current_task_id=current_task_id,
                    strong_pass_count=strong_pass_count,
                    goal_artifact_signature=strong_pass_signature_list,
                    status="active",
                )
            else:
                selected_task = _synthesized_materialize_improvement_candidate(
                    current_task_id=current_task_id,
                    strong_pass_count=strong_pass_count,
                    goal_artifact_signature=strong_pass_signature_list,
                    status="active",
                )
            mode = "escalate_underutilized_ambition"
            reason = "healthy-progress lane is underusing tools/subagents; materialize the synthesized candidate instead of repeating low-ambition review"
            selection_source = "feedback_ambition_escalation_materialize"
        else:
            for preferred_id in [MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID, "subagent-verify-materialized-improvement", SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID]:
                candidate = _task_by_id.get(preferred_id)
                if candidate is not None and _task_is_selectable(candidate):
                    selected_task = candidate
                    break
            if selected_task is not None:
                mode = "escalate_underutilized_ambition"
                reason = "healthy-progress lane is underusing tools/subagents; select the next safe bounded higher-ambition lane"
                selection_source = "feedback_ambition_escalation_bounded_lane"
            else:
                selected_task = _synthesized_materialize_improvement_candidate(
                    current_task_id=current_task_id,
                    strong_pass_count=strong_pass_count,
                    goal_artifact_signature=strong_pass_signature_list,
                    status="active",
                )
                mode = "escalate_underutilized_ambition"
                reason = "healthy-progress lane is underutilized and all recorded safe lanes are completed; generate a fresh bounded materialization lane instead of staying blocked"
                selection_source = "feedback_ambition_escalation_generated_lane"
    elif current_task_id == "inspect-pass-streak":
        followup_task = _task_by_id.get("materialize-pass-streak-improvement")
        if followup_task is not None and _task_is_selectable(followup_task):
            decision = {
                "mode": "promote_review_followup",
                "reason": "active inspect-pass-streak review produced a concrete bounded follow-up candidate",
                "reward_value": reward_value,
                "current_task_id": current_task_id,
                "current_task_class": current_task_class,
                "repeat_block_count": repeat_block_count,
                "repeat_block_failure_class": repeat_block_failure_class,
                "goal_artifact_signature": strong_pass_signature_list,
                "strong_pass_count": strong_pass_count,
                "retire_goal_artifact_pair": False,
                "selected_task_id": followup_task.get("task_id") or followup_task.get("taskId"),
                "selected_task_class": _task_action_class(followup_task.get("task_id") or followup_task.get("taskId")),
                "selection_source": "feedback_review_to_execution",
                "selected_task_title": followup_task.get("title") or followup_task.get("summary") or (followup_task.get("task_id") or followup_task.get("taskId")),
                "selected_task_label": _render_task_selection(followup_task),
            }
            return decision
        active_task = _task_by_id.get(current_task_id)
        strong_pass_belongs_to_current_task = (
            strong_pass_signature is not None
            and len(strong_pass_signature) > 1
            and current_task_id in set(str(value) for value in strong_pass_signature[1])
        )
        if active_task is not None and strong_pass_count >= GOAL_ROTATION_STREAK_LIMIT and strong_pass_belongs_to_current_task:
            if followup_task is None or not _task_is_selectable(followup_task):
                fallback_task = _pick_task_for_classes(task_records, current_task_id, ["reflection", "execution", "verification"])
                if fallback_task is not None:
                    return {
                        "mode": "retire_goal_artifact_pair",
                        "reason": "active inspect-pass-streak review lane should rotate to a concrete bounded follow-up once the materialization lane is already completed",
                        "reward_value": reward_value,
                        "current_task_id": current_task_id,
                        "current_task_class": current_task_class,
                        "repeat_block_count": repeat_block_count,
                        "repeat_block_failure_class": repeat_block_failure_class,
                        "goal_artifact_signature": strong_pass_signature_list,
                        "strong_pass_count": strong_pass_count,
                        "retire_goal_artifact_pair": True,
                        "selected_task_id": fallback_task.get("task_id") or fallback_task.get("taskId"),
                        "selected_task_class": _task_action_class(fallback_task.get("task_id") or fallback_task.get("taskId")),
                        "selection_source": "feedback_pass_streak_switch",
                        "selected_task_title": fallback_task.get("title") or fallback_task.get("summary") or (fallback_task.get("task_id") or fallback_task.get("taskId")),
                        "selected_task_label": _render_task_selection(fallback_task),
                    }
            if followup_task is None and not strong_pass_belongs_to_current_task:
                return {
                    "mode": "continue_active_lane",
                    "reason": "active inspect-pass-streak review lane remains bounded when the repeated PASS signature belongs to a prior lane",
                    "reward_value": reward_value,
                    "current_task_id": current_task_id,
                    "current_task_class": current_task_class,
                    "repeat_block_count": repeat_block_count,
                    "repeat_block_failure_class": repeat_block_failure_class,
                    "goal_artifact_signature": strong_pass_signature_list,
                    "strong_pass_count": strong_pass_count,
                    "retire_goal_artifact_pair": False,
                    "selected_task_id": current_task_id,
                    "selected_task_class": current_task_class,
                    "selection_source": "feedback_continue_active_lane",
                    "selected_task_title": active_task.get("title") or active_task.get("summary") or current_task_id,
                    "selected_task_label": _render_task_selection(active_task),
                }
        if (
            active_task is not None
            and followup_task is None
            and strong_pass_signature is not None
            and not strong_pass_belongs_to_current_task
        ):
            return {
                "mode": "continue_active_lane",
                "reason": "active inspect-pass-streak review lane remains bounded when the repeated PASS signature belongs to a prior lane",
                "reward_value": reward_value,
                "current_task_id": current_task_id,
                "current_task_class": current_task_class,
                "repeat_block_count": repeat_block_count,
                "repeat_block_failure_class": repeat_block_failure_class,
                "goal_artifact_signature": strong_pass_signature_list,
                "strong_pass_count": strong_pass_count,
                "retire_goal_artifact_pair": False,
                "selected_task_id": current_task_id,
                "selected_task_class": current_task_class,
                "selection_source": "feedback_continue_active_lane",
                "selected_task_title": active_task.get("title") or active_task.get("summary") or current_task_id,
                "selected_task_label": _render_task_selection(active_task),
            }
    elif current_task_id == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID:
        active_task = _task_by_id.get(current_task_id)
        # Fast-path: if BOTH materialize AND subagent-verify tasks are already Done,
        # the full synthesize→materialize→verify cycle completed successfully.
        # Skip the ambition-streak wait (which requires 5 consecutive cycles and is
        # routinely interrupted by subagent-verify cycles with subs=1).
        # Instead, immediately escalate to the next materialize so the backlog advances.
        _verify_task = _task_by_id.get("subagent-verify-materialized-improvement")
        _fast_path_materialize = (
            _materialize_task_completed  # materialize status in COMPLETED_TASK_STATUSES
            and _verify_task is not None
            and _task_status(_verify_task) in COMPLETED_TASK_STATUSES
        )
        if _fast_path_materialize:
            _next_materialize = _synthesized_materialize_improvement_candidate(
                current_task_id=current_task_id,
                strong_pass_count=strong_pass_count,
                goal_artifact_signature=strong_pass_signature_list,
                status="active",
            )
            return {
                "mode": "materialize_synthesized_improvement",
                "reason": (
                    "synthesize+materialize+verify cycle fully completed; "
                    "fast-path to next materialize without waiting for ambition streak"
                ),
                "reward_value": reward_value,
                "current_task_id": current_task_id,
                "current_task_class": current_task_class,
                "repeat_block_count": repeat_block_count,
                "repeat_block_failure_class": repeat_block_failure_class,
                "goal_artifact_signature": strong_pass_signature_list,
                "strong_pass_count": strong_pass_count,
                "retire_goal_artifact_pair": False,
                "selected_task_id": _next_materialize.get("task_id") or _next_materialize.get("taskId"),
                "selected_task_class": _task_action_class(_next_materialize.get("task_id") or _next_materialize.get("taskId")),
                "selection_source": "feedback_synthesize_verify_complete_fast_path",
                "selected_task_title": _next_materialize.get("title") or MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
                "selected_task_label": _render_task_selection(_next_materialize),
            }
        should_materialize_synthesized_candidate = (
            strong_pass_count >= GOAL_ROTATION_STREAK_LIMIT
            and isinstance(latest_experiment, dict)
            and latest_experiment.get("outcome") == "discard"
            and latest_experiment.get("revert_status") == "skipped_no_material_change"
        )
        if should_materialize_synthesized_candidate:
            materialize_task = _task_by_id.get(MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID)
            if materialize_task is None:
                materialize_task = _synthesized_materialize_improvement_candidate(
                    current_task_id=current_task_id,
                    strong_pass_count=strong_pass_count,
                    goal_artifact_signature=strong_pass_signature_list,
                    status="active",
                )
            return {
                "mode": "materialize_synthesized_improvement",
                "reason": "repeated discard-only synthesized-improvement review must materialize a concrete execution follow-up instead of re-selecting itself",
                "reward_value": reward_value,
                "current_task_id": current_task_id,
                "current_task_class": current_task_class,
                "repeat_block_count": repeat_block_count,
                "repeat_block_failure_class": repeat_block_failure_class,
                "goal_artifact_signature": strong_pass_signature_list,
                "strong_pass_count": strong_pass_count,
                "retire_goal_artifact_pair": False,
                "selected_task_id": materialize_task.get("task_id") or materialize_task.get("taskId"),
                "selected_task_class": _task_action_class(materialize_task.get("task_id") or materialize_task.get("taskId")),
                "selection_source": "feedback_synthesis_materialization",
                "selected_task_title": materialize_task.get("title") or materialize_task.get("summary") or materialize_task.get("task_id") or materialize_task.get("taskId"),
                "selected_task_label": _render_task_selection(materialize_task),
            }
        if active_task is not None:
            if strong_pass_signature is not None and strong_pass_count >= GOAL_ROTATION_STREAK_LIMIT:
                return {
                    "mode": "retire_goal_artifact_pair",
                    "reason": "active synthesized-improvement review lane should keep the synthesized candidate selected until discard-only pressure materializes a concrete execution follow-up",
                    "reward_value": reward_value,
                    "current_task_id": current_task_id,
                    "current_task_class": current_task_class,
                    "repeat_block_count": repeat_block_count,
                    "repeat_block_failure_class": repeat_block_failure_class,
                    "goal_artifact_signature": strong_pass_signature_list,
                    "strong_pass_count": strong_pass_count,
                    "retire_goal_artifact_pair": True,
                    "selected_task_id": current_task_id,
                    "selected_task_class": _task_action_class(current_task_id),
                    "selection_source": "feedback_pass_streak_switch",
                    "selected_task_title": active_task.get("title") or active_task.get("summary") or current_task_id,
                    "selected_task_label": _render_task_selection(active_task),
                }
            return {
                "mode": "continue_active_lane",
                "reason": "active synthesized-improvement review lane remains bounded while awaiting materialization pressure",
                "reward_value": reward_value,
                "current_task_id": current_task_id,
                "current_task_class": current_task_class,
                "repeat_block_count": repeat_block_count,
                "repeat_block_failure_class": repeat_block_failure_class,
                "goal_artifact_signature": strong_pass_signature_list,
                "strong_pass_count": strong_pass_count,
                "retire_goal_artifact_pair": False,
                "selected_task_id": current_task_id,
                "selected_task_class": _task_action_class(current_task_id),
                "selection_source": "feedback_continue_active_lane",
                "selected_task_title": active_task.get("title") or active_task.get("summary") or current_task_id,
                "selected_task_label": _render_task_selection(active_task),
            }
    if mode == "stable" and current_task_id and current_task_id not in CORE_TASK_IDS and current_task_id != "inspect-pass-streak":
        active_task = _task_by_id.get(current_task_id)
        if (
            active_task is not None
            and not (strong_pass_signature is not None and strong_pass_count >= GOAL_ROTATION_STREAK_LIMIT)
        ):
            return {
                "mode": "continue_active_lane",
                "reason": "active non-core lane remains the best bounded next step",
                "reward_value": reward_value,
                "current_task_id": current_task_id,
                "current_task_class": current_task_class,
                "repeat_block_count": repeat_block_count,
                "repeat_block_failure_class": repeat_block_failure_class,
                "goal_artifact_signature": strong_pass_signature_list,
                "strong_pass_count": strong_pass_count,
                "retire_goal_artifact_pair": False,
                "selected_task_id": current_task_id,
                "selected_task_class": _task_action_class(current_task_id),
                "selection_source": "feedback_continue_active_lane",
                "selected_task_title": active_task.get("title") or active_task.get("summary") or current_task_id,
                "selected_task_label": _render_task_selection(active_task),
            }
    if mode == "stable" and repeat_block_failure_class and repeat_block_count >= REPEATED_BLOCK_LIMIT:
        mode = "force_remediation"
        reason = f"repeated BLOCK on {repeat_block_failure_class}; force remediation"
        preferred_classes = ["verification", "remediation", "diagnostic"]
        selected_task = _pick_task_for_classes(task_records, current_task_id, preferred_classes)
        if selected_task is None:
            selected_task = {
                "task_id": "diagnose-blocker",
                "title": f"Diagnose blocker for {repeat_block_failure_class}",
                "status": "active",
                "kind": "remediation",
                "failure_class": repeat_block_failure_class,
            }
            selection_source = "feedback_repeat_block_remediation"
        else:
            selection_source = "feedback_repeat_block_remediation"
    elif mode == "stable" and reward_value is not None and reward_value < LOW_REWARD_THRESHOLD:
        mode = "switch_task_class"
        reason = f"reward {reward_value} below threshold {LOW_REWARD_THRESHOLD}; change task class next cycle"
        preferred_classes = ["execution", "verification", "remediation"]
        selected_task = _pick_task_for_classes(task_records, current_task_id, preferred_classes)
        if selected_task is not None:
            selection_source = "feedback_low_reward_switch"
    elif mode == "stable" and strong_pass_signature is not None and strong_pass_count >= GOAL_ROTATION_STREAK_LIMIT:
        mode = "retire_goal_artifact_pair"
        reason = "goal/artifact PASS streak reached retirement threshold; deprioritize the pair next cycle"
        if current_task_id and current_task_id not in CORE_TASK_IDS:
            followup_task = _task_by_id.get("materialize-pass-streak-improvement")
            if followup_task is not None and _task_is_selectable(followup_task):
                selected_task = followup_task
                selection_source = "feedback_review_to_execution"
                mode = "promote_review_followup"
                reason = "active inspect-pass-streak review produced a concrete bounded follow-up candidate"
        if selected_task is None:
            preferred_ids = [SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID]
            for preferred_id in preferred_ids:
                candidate = _task_by_id.get(preferred_id)
                if candidate is not None and _task_is_selectable(candidate):
                    selected_task = candidate
                    selection_source = "feedback_pass_streak_switch"
                    break
        if selected_task is None:
            for tid, task in _task_by_id.items():
                if tid in CORE_TASK_IDS or tid == str(current_task_id):
                    continue
                if _task_is_selectable(task):
                    selected_task = task
                    selection_source = "feedback_pass_streak_switch"
                    break
        if selected_task is None:
            synthesized_parent_completed = (
                SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID in _task_by_id
                and not _task_is_selectable(_task_by_id[SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID])
            )
            synthesized_materialization_completed = (
                MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID in _task_by_id
                and not _task_is_selectable(_task_by_id[MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID])
            )
            post_materialization_reward_already_confirmed = (
                current_task_id == "record-reward"
                and isinstance(recorded_feedback_decision, dict)
                and recorded_feedback_decision.get("mode") == "record_reward_after_synthesized_materialization"
                and recorded_feedback_decision.get("selected_task_id") == "record-reward"
            )
            if synthesized_parent_completed and synthesized_materialization_completed and not post_materialization_reward_already_confirmed:
                selected_task = _task_by_id.get("record-reward") or {
                    "task_id": "record-reward", "title": "Record cycle reward", "status": "active",
                }
                mode = "record_reward_after_synthesized_materialization"
                reason = "synthesized candidate and its materialization artifact are complete; return to reward accounting instead of replaying the parent review lane"
                selection_source = "feedback_synthesized_materialization_complete_reward"
            else:
                mode = "synthesize_next_candidate"
                if post_materialization_reward_already_confirmed:
                    reason = "post-materialization reward accounting is already confirmed; rotate to a fresh bounded improvement candidate instead of repeating reward bookkeeping"
                else:
                    reason = "goal/artifact PASS retirement pressure reached with no selectable bounded lane; synthesize a new bounded improvement candidate"
                selected_task = _synthesized_next_improvement_candidate(
                    current_task_id=current_task_id,
                    strong_pass_count=strong_pass_count,
                    goal_artifact_signature=strong_pass_signature_list,
                    status="active",
                )
                selection_source = "feedback_no_selectable_retired_lane_synthesis"

    decision = {
        "mode": mode,
        "reason": reason,
        "reward_value": reward_value,
        "current_task_id": current_task_id,
        "current_task_class": current_task_class,
        "repeat_block_count": repeat_block_count,
        "repeat_block_failure_class": repeat_block_failure_class,
        "goal_artifact_signature": strong_pass_signature_list,
        "strong_pass_count": strong_pass_count,
        "retire_goal_artifact_pair": mode == "retire_goal_artifact_pair",
        "ambition_escalation": {
            "state": "blocked" if mode == "ambition_escalation_blocked" else "selected",
            "reasons": ambition_underutilization_reasons,
            "blocker": "no_safe_bounded_escalation_lane_selectable" if mode == "ambition_escalation_blocked" else None,
        } if ambition_underutilization_reasons else None,
        "selected_task_id": None,
        "selected_task_class": None,
        "selection_source": selection_source,
    }

    if selected_task is not None:
        decision["selected_task_id"] = selected_task.get("task_id") or selected_task.get("taskId")
        decision["selected_task_class"] = _task_action_class(decision["selected_task_id"])
        decision["selected_task_title"] = selected_task.get("title") or selected_task.get("summary") or decision["selected_task_id"]
        decision["selected_task_label"] = _render_task_selection(selected_task)

    if mode == "stable" and not reason:
        return None
    return decision


def _extract_history_signature(history_entry: dict[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(history_entry, dict):
        return None
    result_status = history_entry.get("result_status") or history_entry.get("status")
    if result_status != "PASS":
        return None

    goal_id = history_entry.get("goal_id") or history_entry.get("active_goal") or history_entry.get("goalId")
    if not goal_id and isinstance(history_entry.get("goal"), dict):
        goal = history_entry.get("goal") or {}
        goal_id = goal.get("goal_id") or goal.get("goalId")

    current_task_id = history_entry.get("current_task_id") or history_entry.get("currentTaskId")
    artifact_paths = history_entry.get("artifact_paths") or history_entry.get("artifactPaths")
    if artifact_paths is None and isinstance(history_entry.get("follow_through"), dict):
        artifact_paths = history_entry["follow_through"].get("artifact_paths") or history_entry["follow_through"].get("artifactPaths")
    if artifact_paths is None and isinstance(history_entry.get("goal"), dict):
        follow_through = history_entry["goal"].get("follow_through")
        if isinstance(follow_through, dict):
            artifact_paths = follow_through.get("artifact_paths") or follow_through.get("artifactPaths")

    normalized_artifacts = _normalize_artifact_paths(artifact_paths)
    if current_task_id:
        artifact_signature = (str(current_task_id),)
    elif normalized_artifacts:
        artifact_signature = tuple(str(path) for path in normalized_artifacts)
    else:
        artifact_signature = ()
    if not goal_id or not artifact_signature:
        return None
    return str(goal_id), artifact_signature


def _latest_goal_rotation_streak(goals_dir: Path, active_goal: str) -> tuple[int, tuple[str, tuple[str, ...]] | None]:
    if active_goal == DEFAULT_ACTIVE_GOAL:
        return 0, None

    history_dir = goals_dir / "history"
    if not history_dir.exists():
        return 0, None

    # Use os.scandir-based helper to avoid double-stat penalty of glob()+stat().
    history_files = [p for p, _ in _json_files_sorted_by_mtime(True, history_dir) if p.name.startswith("cycle-")][:GOAL_ROTATION_STREAK_LIMIT + 1]
    if not history_files:
        return 0, None

    streak = 0
    signature: tuple[str, tuple[str, ...]] | None = None
    for path in history_files:
        payload = _safe_read_json(path)
        current_signature = _extract_history_signature(payload or {}) if isinstance(payload, dict) else None
        if current_signature is None:
            break
        if streak == 0:
            signature = current_signature
            if current_signature[0] != active_goal:
                break
            streak = 1
            continue
        if current_signature != signature:
            break
        streak += 1
    return streak, signature


def _write_active_goal(goals_dir: Path, active_goal: str, metadata: dict[str, Any] | None = None) -> None:
    goals_dir.mkdir(parents=True, exist_ok=True)
    active_path = goals_dir / "active.json"
    payload: dict[str, Any] = {"active_goal": active_goal}
    if metadata:
        payload.update(metadata)
    active_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _ensure_active_goal(goals_dir: Path, now: datetime | None = None) -> str:
    goals_dir.mkdir(parents=True, exist_ok=True)
    active_path = goals_dir / "active.json"
    active_goal = DEFAULT_ACTIVE_GOAL
    if active_path.exists():
        payload = _safe_read_json(active_path) or {}
        active_goal = (
            payload.get("active_goal")
            or payload.get("activeGoal")
            or payload.get("active_goal_id")
            or payload.get("activeGoalId")
            or payload.get("goal_id")
            or payload.get("goalId")
            or DEFAULT_ACTIVE_GOAL
        )

    streak, signature = _latest_goal_rotation_streak(goals_dir, active_goal)
    if streak >= GOAL_ROTATION_STREAK_LIMIT and signature is not None:
        rotated_from, artifact_paths = signature
        active_goal = DEFAULT_ACTIVE_GOAL
        _write_active_goal(
            goals_dir,
            active_goal,
            metadata={
                "rotation_reason": "goal/artifact PASS streak exceeded loop-breaker limit",
                "rotation_streak": streak,
                "rotation_trigger_goal": rotated_from,
                "rotation_trigger_artifact_paths": list(artifact_paths),
                "rotation_triggered_at_utc": _utc_iso(_utc_now(now)),
            },
        )
        return active_goal

    _write_active_goal(goals_dir, active_goal)
    return active_goal


def _load_approval_gate(state_root: Path, now: datetime) -> tuple[dict[str, Any], str]:
    approvals_dir = state_root / "approvals"
    gate_path = approvals_dir / "apply.ok"
    if not gate_path.exists():
        return (
            {"state": "missing", "ttl_minutes": None, "source": str(gate_path)},
            "approval gate missing; refresh manually",
        )

    raw_payload = _safe_read_json(gate_path)
    if not isinstance(raw_payload, dict):
        return (
            {"state": "invalid", "ttl_minutes": None, "source": str(gate_path)},
            "refresh approval gate manually",
        )

    payload = raw_payload
    expires_at = _parse_datetime(
        payload.get("expires_at_utc")
        or payload.get("expiresAtUtc")
        or payload.get("expires_at")
        or payload.get("expiresAt")
    )
    if expires_at is None:
        expires_at_epoch = (
            payload.get("expires_at_epoch")
            or payload.get("expiresAtEpoch")
            or payload.get("expires_at_unix")
            or payload.get("expiresAtUnix")
        )
        if expires_at_epoch is not None:
            try:
                expires_at = datetime.fromtimestamp(float(expires_at_epoch), tz=timezone.utc)
            except (TypeError, ValueError, OSError, OverflowError):
                expires_at = None
    ttl_minutes = payload.get("ttl_minutes") or payload.get("ttlMinutes")
    if expires_at is not None:
        remaining_seconds = (expires_at - now).total_seconds()
        if remaining_seconds <= 0:
            return (
                {
                    "state": "expired",
                    "ttl_minutes": 0,
                    "expires_at_utc": _utc_iso(expires_at),
                    "source": str(gate_path),
                },
                "refresh approval gate manually",
            )
        computed_ttl = max(1, math.ceil(remaining_seconds / 60))
        return (
            {
                "state": "fresh",
                "ttl_minutes": int(ttl_minutes or computed_ttl),
                "expires_at_utc": _utc_iso(expires_at),
                "source": str(gate_path),
            },
            "none",
        )

    if ttl_minutes is not None:
        return (
            {
                "state": "fresh",
                "ttl_minutes": int(ttl_minutes),
                "source": str(gate_path),
            },
            "none",
        )

    return (
        {"state": "invalid", "ttl_minutes": None, "source": str(gate_path)},
        "refresh approval gate manually",
    )


def _derive_reward_signal(
    result_status: str,
    improvement_score: Any,
    current_task_id: str | None = None,
    previous_experiment: dict[str, Any] | None = None,
    fd: dict[str, Any] | None = None,
    commits_pushed: int = 0,
) -> dict[str, Any]:
    """Derive reward signal, optionally enriched by the frozen scorer (issue #527).

    When fd (feedback_decision) is provided, the frozen scorer's value is blended
    in as a secondary signal for auditability.  The primary value is still
    improvement_score / result_status (backward compatible).
    """
    from nanobot.runtime.scorer import SCORER_VERSION, score_cycle

    reward_value: float
    reward_source: str
    if improvement_score is not None:
        try:
            reward_value = float(improvement_score)
            reward_source = "improvement_score"
        except (TypeError, ValueError):
            reward_value = 0.0
            reward_source = "improvement_score_unusable"
    else:
        reward_value = {"PASS": 1.0, "BLOCK": 0.0, "ERROR": -1.0}.get(result_status, 0.0)
        reward_source = "result_status"
        if (
            result_status == "PASS"
            and current_task_id == "record-reward"
            and isinstance(previous_experiment, dict)
            and previous_experiment.get("result_status") == "PASS"
            and previous_experiment.get("current_task_id") == "record-reward"
        ):
            reward_value = 0.6
            reward_source = "bookkeeping_pass_streak_penalty"

    # Frozen scorer: compute auxiliary score for auditability (does not override primary)
    # Operator opt-in: if SELFEVO_SURFACES_DIR is set, load weights from surfaces/score_weights.json.
    # When unset, hardcoded defaults apply — frozen invariant preserved.
    _surfaces_dir = os.environ.get('SELFEVO_SURFACES_DIR', '').strip()
    _scorer_weights_path: Path | None = None
    if _surfaces_dir:
        _wp = Path(_surfaces_dir) / 'surfaces' / 'score_weights.json'
        if _wp.exists():
            _scorer_weights_path = _wp

    scorer_result = None
    if fd is not None:
        try:
            scorer_result = score_cycle(
                fd=fd,
                budget={},
                commits_pushed=commits_pushed,
                result_status=result_status.lower() if result_status else "",
                weights_path=_scorer_weights_path,
            )
        except Exception:
            pass  # never let scorer errors affect primary reward

    return {
        "value": round(reward_value, 4),
        "source": reward_source,
        "result_status": result_status,
        "scorer_version": SCORER_VERSION,
        **(({
            "frozen_scorer_value": scorer_result.value,
            "frozen_scorer_outcome": scorer_result.outcome,
            "frozen_scorer_rationale": scorer_result.rationale,
            "scorer_weights_source": scorer_result.weights_source,
        }) if scorer_result is not None else {}),
    }


def _load_previous_experiment_snapshot(experiments_dir: Path) -> dict[str, Any] | None:
    latest_path = experiments_dir / 'latest.json'
    data = _safe_read_json(latest_path)
    return data if isinstance(data, dict) else None


def _experiment_metric_summary(result_status: str, reward_signal: dict[str, Any], previous_experiment: dict[str, Any] | None) -> dict[str, Any]:
    metric_name = 'reward_signal.value'
    try:
        metric_current = float(reward_signal.get('value') or 0.0)
    except Exception:
        metric_current = 0.0
    metric_baseline = None
    metric_frontier = metric_current
    if isinstance(previous_experiment, dict):
        try:
            if previous_experiment.get('metric_current') is not None:
                metric_baseline = float(previous_experiment.get('metric_current'))
        except Exception:
            metric_baseline = None
        try:
            if previous_experiment.get('metric_frontier') is not None:
                metric_frontier = max(metric_frontier, float(previous_experiment.get('metric_frontier')))
            elif previous_experiment.get('metric_current') is not None:
                metric_frontier = max(metric_frontier, float(previous_experiment.get('metric_current')))
        except Exception:
            pass
    if result_status == 'BLOCK':
        outcome = 'blocked'
    elif result_status == 'ERROR':
        outcome = 'crash'
    elif metric_baseline is None:
        outcome = 'keep'
    elif metric_current >= metric_baseline:
        outcome = 'keep'
    else:
        outcome = 'discard'
    return {
        'metric_name': metric_name,
        'metric_current': round(metric_current, 4),
        'metric_baseline': round(metric_baseline, 4) if metric_baseline is not None else None,
        'metric_frontier': round(metric_frontier, 4),
        'outcome': outcome,
    }


def _derive_experiment_current_task_id(result_status: str, feedback_decision: dict[str, Any] | None) -> str:
    if isinstance(feedback_decision, dict) and feedback_decision.get('selected_task_id'):
        return str(feedback_decision['selected_task_id'])
    if result_status == 'BLOCK':
        return 'refresh-approval-gate'
    if result_status == 'ERROR':
        return 'run-bounded-turn'
    return 'record-reward'


def _experiment_complexity_summary(result_status: str, selected_tasks: str, feedback_decision: dict[str, Any] | None) -> dict[str, Any]:
    if result_status == 'BLOCK':
        complexity_delta = 0
        simplicity_judgment = 'simple'
    elif result_status == 'PASS':
        complexity_delta = 1
        simplicity_judgment = 'moderate'
    elif isinstance(feedback_decision, dict) and feedback_decision.get('selected_task_id'):
        complexity_delta = 1
        simplicity_judgment = 'moderate'
    elif selected_tasks and '[' in selected_tasks:
        complexity_delta = 1
        simplicity_judgment = 'moderate'
    else:
        complexity_delta = 0
        simplicity_judgment = 'simple'
    return {
        'complexity_delta': complexity_delta,
        'simplicity_judgment': simplicity_judgment,
    }


def _build_experiment_contract(
    *,
    experiment_id: str,
    cycle_id: str,
    goal_id: str,
    current_task_id: str,
    selected_tasks: str,
    task_selection_source: str,
    budget: dict[str, Any],
    budget_policy: dict[str, Any],
    metric_summary: dict[str, Any],
    contract_path: Path,
) -> dict[str, Any]:
    return {
        'schema_version': EXPERIMENT_CONTRACT_VERSION,
        'experiment_id': experiment_id,
        'cycle_id': cycle_id,
        'goal_id': goal_id,
        'current_task_id': current_task_id,
        'selected_tasks': selected_tasks,
        'task_selection_source': task_selection_source,
        'contract_type': 'bounded-hourly-self-improvement',
        'run_budget': budget,
        'budget_policy': budget_policy,
        'success_metric': metric_summary['metric_name'],
        'baseline_ref': metric_summary['metric_baseline'],
        'hypothesis': f"If task `{current_task_id}` succeeds, `{metric_summary['metric_name']}` should stay at or above baseline.",
        'success_checks': [
            'result_status=PASS',
            f"metric_name={metric_summary['metric_name']}",
            'metric_current >= metric_baseline when baseline exists',
        ],
        'keep_rule': 'keep when result_status=PASS and metric_current >= metric_baseline, or when no baseline exists',
        'discard_rule': 'discard when result_status=PASS and metric_current < metric_baseline',
        'crash_rule': 'crash when result_status=ERROR',
        'blocked_rule': 'blocked when result_status=BLOCK',
        'mutation_scope': {
            'selected_tasks': selected_tasks,
            'selection_source': task_selection_source,
            'within_hourly_budget': True,
        },
        'contract_path': str(contract_path),
    }


def _subagent_consumption_snapshot(
    *,
    state_root: Path,
    workspace: Path,
    cycle_id: str,
    report_path: Path,
    current_task_id: str | None,
    tracked_request_path: str | None = None,
    max_results: int = 8,
) -> dict[str, Any]:
    """Return bridge subagent results that should be consumed by this cycle.

    The bridge may write JSON telemetry either into the canonical state root or
    into the runtime checkout's `.nanobot/subagents` directory.  Canonical
    accounting is keyed by the concrete cycle/report/task, not by dashboard
    inference, so this snapshot is written into reports/experiments before
    credits and outbox artifacts are emitted.
    """
    candidate_dirs = [
        state_root / "subagents",
        state_root / "subagents" / "results",
        workspace / ".nanobot" / "subagents",
    ]
    report_path_str = str(report_path)
    rows: list[tuple[float, str, dict[str, Any]]] = []
    seen: set[Path] = set()
    logical_seen: set[tuple[str, str, str]] = set()
    for root in candidate_dirs:
        if not root.exists():
            continue
        try:
            entries = list(os.scandir(str(root)))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".json") or not entry.is_file():
                continue
            path = root / entry.name
            if path in seen:
                continue
            seen.add(path)
            payload = _safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            status = str(payload.get("status") or payload.get("result_status") or "").lower()
            if status not in {"ok", "done", "completed", "pass", "approved"}:
                continue
            match_reasons: list[str] = []
            if payload.get("cycle_id") == cycle_id:
                match_reasons.append("cycle_id")
            if payload.get("report_path") == report_path_str or payload.get("report_source") == report_path_str:
                match_reasons.append("report_path")
            payload_task_id = payload.get("current_task_id") or payload.get("task_id")
            if current_task_id and payload_task_id == current_task_id:
                match_reasons.append("current_task_id")
            if tracked_request_path and payload.get("request_path") == tracked_request_path:
                match_reasons.append("request_path")
            if not ("cycle_id" in match_reasons or "report_path" in match_reasons or "request_path" in match_reasons):
                continue
            # Reuse the stat cached by os.scandir to avoid a second stat() syscall.
            # This cuts syscalls in half for each candidate directory (e.g. 143 files
            # → 143 stat calls instead of 286), matching the optimization already
            # applied to _json_files_sorted_by_mtime in state.py.
            try:
                mtime = entry.stat().st_mtime
            except Exception:
                mtime = 0.0
            subagent_id = str(payload.get("subagent_id") or payload.get("id") or path.stem)
            logical_key = (
                subagent_id,
                str(payload.get("cycle_id") or ""),
                str(payload.get("report_path") or payload.get("report_source") or ""),
            )
            if logical_key in logical_seen:
                continue
            logical_seen.add(logical_key)
            rows.append((mtime, str(path), {
                "path": str(path),
                "subagent_id": subagent_id,
                "status": payload.get("status") or payload.get("result_status"),
                "summary": payload.get("summary") or payload.get("result"),
                "goal_id": payload.get("goal_id"),
                "cycle_id": payload.get("cycle_id"),
                "report_path": payload.get("report_path") or payload.get("report_source"),
                "current_task_id": payload_task_id,
                "task_feedback_decision": payload.get("task_feedback_decision") or payload.get("feedback_decision"),
                "match_reasons": match_reasons,
            }))
    # Use heapq.nlargest for O(n log k) instead of sorted() O(n log n)
    # when only the top max_results entries are needed from all candidate rows.
    top_rows = heapq.nlargest(max_results, rows, key=lambda item: item[0])
    results = [row[2] for row in top_rows]
    result_paths = [row[1] for row in top_rows]
    return {
        "schema_version": "subagent-consumption-v1",
        "state": "consumed" if results else "none",
        "consumed_count": len(results),
        "budget_subagents": len(results),
        "result_paths": result_paths,
        "results": results,
        "correlation": {
            "cycle_id": cycle_id,
            "report_path": report_path_str,
            "current_task_id": current_task_id,
            "sources": [str(path) for path in candidate_dirs],
        },
    }


def _derive_budget_usage(
    *,
    result_status: str,
    cycle_started_utc: str,
    cycle_ended_utc: str,
) -> dict[str, Any]:
    started = _parse_datetime(cycle_started_utc)
    ended = _parse_datetime(cycle_ended_utc)
    elapsed_seconds = 0
    if started is not None and ended is not None:
        elapsed_seconds = max(0, int((ended - started).total_seconds()))

    request_count = 1 if result_status in {"PASS", "ERROR"} else 0
    tool_call_count = 1 if result_status == "PASS" else 0
    return {
        "requests": request_count,
        "tool_calls": tool_call_count,
        "subagents": 0,
        "elapsed_seconds": elapsed_seconds,
    }


def _build_revert_record(
    *,
    experiment_id: str,
    cycle_id: str,
    goal_id: str,
    outcome: str,
    metric_name: str,
    metric_baseline: float | None,
    metric_current: float | None,
    contract_path: Path,
    revert_path: Path,
) -> dict[str, Any]:
    status = 'skipped_no_material_change'
    reason = 'discarded telemetry did not produce a material file change to revert'
    return {
        'schema_version': 'experiment-revert-v1',
        'experiment_id': experiment_id,
        'cycle_id': cycle_id,
        'goal_id': goal_id,
        'outcome': outcome,
        'metric_name': metric_name,
        'metric_baseline': metric_baseline,
        'metric_current': metric_current,
        'revert_status': status,
        'terminal': True,
        'reason': reason,
        'contract_path': str(contract_path),
        'revert_path': str(revert_path),
    }


def _build_experiment_snapshot(
    *,
    experiment_id: str,
    cycle_id: str,
    goal_id: str,
    result_status: str,
    approval_gate_state: str,
    next_hint: str,
    selected_tasks: str,
    task_selection_source: str,
    cycle_started_utc: str,
    cycle_ended_utc: str,
    report_path: Path,
    history_path: Path,
    outbox_path: Path,
    promotion_candidate_id: str | None,
    review_status: str | None,
    decision: str | None,
    reward_signal: dict[str, Any],
    feedback_decision: dict[str, Any] | None,
    previous_experiment: dict[str, Any] | None,
    contract_path: Path,
    revert_path: Path,
) -> dict[str, Any]:
    budget_used = _derive_budget_usage(
        result_status=result_status,
        cycle_started_utc=cycle_started_utc,
        cycle_ended_utc=cycle_ended_utc,
    )
    if result_status == "BLOCK":
        budget_used["requests"] = 0
    metric_summary = _experiment_metric_summary(result_status, reward_signal, previous_experiment)
    # R11: no-progress stall counter (chains off the previous snapshot).
    stall = evaluate_stall(
        result_status=result_status,
        outcome=metric_summary['outcome'],
        metric_current=metric_summary['metric_current'],
        metric_frontier=metric_summary['metric_frontier'],
        previous_experiment=previous_experiment,
    )
    complexity_summary = _experiment_complexity_summary(result_status, selected_tasks, feedback_decision)
    current_task_id = _derive_experiment_current_task_id(result_status, feedback_decision)
    budget, budget_policy = _derive_experiment_budget_policy(
        result_status=result_status,
        current_task_id=current_task_id,
        selected_tasks=selected_tasks,
        task_selection_source=task_selection_source,
        feedback_decision=feedback_decision,
    )
    # R13: per-lane iteration counter + exceeded budget cap → single stop reason.
    lane_iter = lane_iteration(goal_id, previous_experiment)
    budget_over = budget_exceeded(budget, budget_used)
    stop_reason = derive_stop_reason(
        outcome=metric_summary['outcome'],
        stall=stall,
        budget_exceeded=budget_over,
        max_iterations_reached=lane_iter >= MAX_ITERATIONS_DEFAULT,
    )
    contract = _build_experiment_contract(
        experiment_id=experiment_id,
        cycle_id=cycle_id,
        goal_id=goal_id,
        current_task_id=current_task_id,
        selected_tasks=selected_tasks,
        task_selection_source=task_selection_source,
        budget=budget,
        budget_policy=budget_policy,
        metric_summary=metric_summary,
        contract_path=contract_path,
    )
    revert_required = metric_summary['outcome'] == 'discard'
    revert_record = _build_revert_record(
        experiment_id=experiment_id,
        cycle_id=cycle_id,
        goal_id=goal_id,
        outcome=metric_summary['outcome'],
        metric_name=metric_summary['metric_name'],
        metric_baseline=metric_summary['metric_baseline'],
        metric_current=metric_summary['metric_current'],
        contract_path=contract_path,
        revert_path=revert_path,
    ) if revert_required else None
    return {
        "schema_version": EXPERIMENT_VERSION,
        "experiment_id": experiment_id,
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "result_status": result_status,
        "approval_gate_state": approval_gate_state,
        "next_hint": next_hint,
        "selected_tasks": selected_tasks,
        "task_selection_source": task_selection_source,
        "cycle_started_utc": cycle_started_utc,
        "cycle_ended_utc": cycle_ended_utc,
        "budget": budget,
        "budget_policy": budget_policy,
        "budget_used": budget_used,
        "reward_signal": reward_signal,
        "feedback_decision": feedback_decision,
        "promotion_candidate_id": promotion_candidate_id,
        "review_status": review_status,
        "decision": decision,
        "report_path": str(report_path),
        "history_path": str(history_path),
        "outbox_path": str(outbox_path),
        "current_task_id": current_task_id,
        "metric_name": metric_summary['metric_name'],
        "metric_baseline": metric_summary['metric_baseline'],
        "metric_current": metric_summary['metric_current'],
        "metric_frontier": metric_summary['metric_frontier'],
        "outcome": metric_summary['outcome'],
        "stall": stall,
        "stop_reason": stop_reason,
        "lane_iteration": lane_iter,
        "complexity_delta": complexity_summary['complexity_delta'],
        "simplicity_judgment": complexity_summary['simplicity_judgment'],
        "revert_required": revert_required,
        "revert_status": revert_record['revert_status'] if revert_record else None,
        "revert_path": str(revert_path) if revert_required else None,
        "contract_path": str(contract_path),
        "contract": contract,
        "hypothesis": contract.get('hypothesis'),
        "success_checks": contract.get('success_checks'),
        "revert": revert_record,
    }


def _derive_mutation_lane(*, current_task_id: str | None, selected_tasks: str | None, task_selection_source: str | None) -> dict[str, Any]:
    task_class = _task_action_class(current_task_id)
    if task_class in {'bounded_apply', 'fix'}:
        lane = 'bounded_apply'
    elif task_class in {'diagnose', 'review'}:
        lane = 'diagnostic'
    else:
        lane = 'read_only'
    return {
        'lane': lane,
        'task_class': task_class,
        'selection_source': task_selection_source,
        'selected_tasks': selected_tasks,
        'reason': 'derived_from_task_class',
    }
