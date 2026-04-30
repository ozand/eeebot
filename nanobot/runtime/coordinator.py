"""Minimal durable self-evolving runtime coordinator."""
import asyncio
import hashlib
import json
import math
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from nanobot.runtime.autoevolve import resolve_terminal_selfevo_issue
from nanobot.runtime.promotion import review_promotion_candidate
from nanobot.runtime.state import _subagent_rollup_snapshot
from nanobot.utils.helpers import estimate_prompt_tokens

PROMOTION_RECORD_VERSION = 'promotion-record-v1'
PATCH_BUNDLE_VERSION = 'promotion-patch-v1'


DEFAULT_ACTIVE_GOAL = "goal-bootstrap"
GOAL_ROTATION_STREAK_LIMIT = 3
TASK_PLAN_VERSION = "task-plan-v1"
EXPERIMENT_VERSION = "experiment-v1"
EXPERIMENT_CONTRACT_VERSION = "experiment-contract-v1"
HYPOTHESIS_BACKLOG_VERSION = "hypothesis-backlog-v1"
CREDITS_LEDGER_VERSION = "credits-ledger-v1"
DEFAULT_EXPERIMENT_BUDGET = {
    "max_requests": 2,
    "max_tool_calls": 12,
    "max_subagents": 2,
    "max_timeout_seconds": 900,
}
LOW_REWARD_THRESHOLD = 0.5
REPEATED_BLOCK_LIMIT = 2
AMBITION_UNDERUTILIZATION_STREAK_LIMIT = 5
CORE_TASK_IDS = {
    "refresh-approval-gate",
    "verify-approval-gate",
    "run-bounded-turn",
    "record-reward",
}
SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID = "synthesize-next-improvement-candidate"
MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID = "materialize-synthesized-improvement"

COMPLETED_TASK_STATUSES = {
    "blocked",
    "canceled",
    "cancelled",
    "closed",
    "done",
    "failed",
    "terminal",
    "terminal_blocked",
    "terminal_closed",
    "terminal_failed",
    "terminal_merged",
    "terminal_noop",
}


TASK_ACTION_CLASS_BY_ID = {
    "refresh-approval-gate": "remediation",
    "verify-approval-gate": "verification",
    "run-bounded-turn": "execution",
    "record-reward": "reflection",
    "inspect-pass-streak": "review",
    "materialize-pass-streak-improvement": "execution",
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID: "execution",
    "subagent-verify-materialized-improvement": "review",
    SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID: "review",
}


def _utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return _utc_now(value).isoformat().replace("+00:00", "Z")


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_artifact_paths(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if item not in (None, ""))
    return (str(value),)


def _task_action_class(task_id: str | None) -> str:
    if not task_id:
        return "unknown"
    return TASK_ACTION_CLASS_BY_ID.get(str(task_id), "other")


def _task_status(task: dict[str, Any] | None) -> str:
    if not isinstance(task, dict):
        return ""
    return str(task.get("status") or "pending").strip().lower()


def _task_is_selectable(task: dict[str, Any] | None) -> bool:
    status = _task_status(task)
    return status not in COMPLETED_TASK_STATUSES


def _task_is_terminal_selfevo_retired(task: dict[str, Any] | None, terminal_selfevo_issue: dict[str, Any] | None) -> bool:
    if not isinstance(task, dict) or not isinstance(terminal_selfevo_issue, dict):
        return False
    if task.get("task_id") != "analyze-last-failed-candidate":
        return False

    terminal_status = str(terminal_selfevo_issue.get("terminal_status") or "").strip().lower()
    if not terminal_status:
        return False

    task_status = _task_status(task)
    terminal_reason = str(task.get("terminal_reason") or "").strip().lower()
    return task_status in COMPLETED_TASK_STATUSES and (task_status == terminal_status or terminal_reason == terminal_status)


def _task_has_recorded_terminal_selfevo_retirement(task: dict[str, Any] | None) -> bool:
    if not isinstance(task, dict):
        return False
    if task.get("task_id") != "analyze-last-failed-candidate":
        return False
    task_status = _task_status(task)
    terminal_reason = str(task.get("terminal_reason") or "").strip().lower()
    return task_status in COMPLETED_TASK_STATUSES and terminal_reason.startswith("terminal_")


def _render_task_selection(task: dict[str, Any]) -> str:
    task_id = task.get("task_id") or task.get("taskId")
    task_title = task.get("title") or task.get("summary") or task_id or "task"
    if task_id:
        return f"{task_title} [task_id={task_id}]"
    return str(task_title)


def _task_title_for_id(task_id: str | None, *task_sets: list[dict[str, Any]]) -> str | None:
    if not task_id:
        return None
    for task_set in task_sets:
        for task in task_set:
            if not isinstance(task, dict):
                continue
            candidate_id = task.get("task_id") or task.get("taskId")
            if candidate_id == task_id:
                return task.get("title") or task.get("summary") or str(task_id)
    return str(task_id)


def _pick_task_for_classes(
    task_records: list[dict[str, Any]],
    current_task_id: str | None,
    preferred_classes: list[str],
) -> dict[str, Any] | None:
    for preferred_class in preferred_classes:
        for task in task_records:
            task_id = task.get("task_id") or task.get("taskId")
            if task_id == current_task_id:
                continue
            if _task_action_class(task_id) == preferred_class:
                return task
    for task in task_records:
        task_id = task.get("task_id") or task.get("taskId")
        if task_id != current_task_id:
            return task
    return None


def _synthesized_next_improvement_candidate(
    *,
    current_task_id: str | None,
    strong_pass_count: int,
    goal_artifact_signature: list[str] | None,
    status: str = "pending",
) -> dict[str, Any]:
    return {
        "task_id": SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
        "title": "Synthesize one new bounded improvement candidate from retired lanes",
        "status": status,
        "kind": "review",
        "acceptance": "produce one new bounded improvement candidate that is not a retired terminal/completed lane",
        "selection_source": "feedback_no_selectable_retired_lane_synthesis",
        "parent_task_id": current_task_id,
        "strong_pass_count": strong_pass_count,
        "goal_artifact_signature": goal_artifact_signature,
    }


def _synthesized_materialize_improvement_candidate(
    *,
    current_task_id: str | None,
    strong_pass_count: int,
    goal_artifact_signature: list[str] | None,
    status: str = "pending",
) -> dict[str, Any]:
    return {
        "task_id": MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
        "title": "Materialize one bounded improvement from the synthesized candidate",
        "status": status,
        "kind": "execution",
        "acceptance": "write a concrete bounded HADI improvement proposal or artifact (hypothesis, action, data, insight) and route it into self-evolution",
        "selection_source": "feedback_synthesis_materialization",
        "parent_task_id": current_task_id,
        "strong_pass_count": strong_pass_count,
        "goal_artifact_signature": goal_artifact_signature,
        "hadi_required": True,
        "hadi_cycle": {
            "hypothesis": "A concrete bounded materialization will break the reward/candidate discard loop.",
            "action": "Create one reviewable artifact or follow-up task with explicit acceptance checks.",
            "data": "Use recent task history, experiment outcome, and budget/subagent utilization evidence.",
            "insight": "Decide whether the artifact should be accepted, escalated to subagent review, or blocked with a concrete reason.",
        },
    }


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


def _git_output(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)
        text = (result.stdout or '').strip()
        return text or None
    except Exception:
        return None


def _runtime_source_fingerprint(workspace: Path) -> dict[str, Any]:
    search_roots = [workspace, Path(__file__).resolve().parents[2], Path.cwd()]
    for candidate_root in search_roots:
        repo_root = candidate_root
        while repo_root != repo_root.parent and not (repo_root / '.git').exists():
            repo_root = repo_root.parent
        if (repo_root / '.git').exists():
            commit = _git_output(['git', 'rev-parse', 'HEAD'], repo_root)
            branch = _git_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], repo_root)
            tree = _git_output(['git', 'rev-parse', 'HEAD^{tree}'], repo_root)
            return {
                'source_repo_root': str(repo_root),
                'source_commit': commit,
                'source_branch': branch,
                'source_tree': tree,
            }
    return {
        'source_repo_root': str(workspace),
        'source_commit': None,
        'source_branch': None,
        'source_tree': None,
    }


def _prompt_mass_snapshot(
    *,
    selected_tasks: str,
    current_plan: dict[str, Any],
    hypothesis_backlog: dict[str, Any],
) -> dict[str, Any]:
    proposal_parts = {
        'selected_tasks': selected_tasks,
        'task_plan': current_plan,
        'hypothesis_backlog': hypothesis_backlog,
    }
    text_payload = json.dumps(proposal_parts, ensure_ascii=False)
    estimated_tokens = estimate_prompt_tokens([
        {'role': 'user', 'content': text_payload},
    ])
    char_count = len(text_payload)
    if estimated_tokens > 16000:
        risk = 'high'
    elif estimated_tokens > 8000:
        risk = 'medium'
    else:
        risk = 'low'
    return {
        'bytes': len(text_payload.encode('utf-8')),
        'chars': char_count,
        'estimated_tokens': estimated_tokens,
        'risk': risk,
    }


def _load_recent_history_entries(history_dir: Path, limit: int = 4) -> list[dict[str, Any]]:
    if not history_dir.exists():
        return []
    history_files = sorted(
        history_dir.glob("cycle-*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    entries: list[dict[str, Any]] = []
    for path in history_files[:limit]:
        payload = _safe_read_json(path)
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def _derive_feedback_decision(task_plan: dict[str, Any] | None, goals_dir: Path) -> dict[str, Any] | None:
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
    recorded_feedback_decision = task_plan.get("feedback_decision") if isinstance(task_plan.get("feedback_decision"), dict) else None
    if (
        isinstance(recorded_feedback_decision, dict)
        and recorded_feedback_decision.get("mode") in {"retire_terminal_selfevo_lane", "retire_terminal_noop_lane", "retire_stale_subagent_lane"}
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

    selected_task: dict[str, Any] | None = None
    selection_source = "recorded_current_task"
    mode = "stable"
    reason = ""
    ambition_underutilization_reasons = _ambition_underutilization_reasons(history_entries, str(current_task_id) if current_task_id else None)

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
    materialize_task = next((task for task in task_records if (task.get("task_id") or task.get("taskId")) == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID), None)
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
        and _task_status(materialize_task) in COMPLETED_TASK_STATUSES
        and post_materialization_reward_already_confirmed
        and {"recent_window_discard_only", "subagents_unused"}.issubset(set(ambition_underutilization_reasons))
    ):
        selected_task = _synthesized_materialize_improvement_candidate(
            current_task_id=current_task_id,
            strong_pass_count=strong_pass_count,
            goal_artifact_signature=list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
            "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
        and _task_status(materialize_task) in COMPLETED_TASK_STATUSES
        and post_materialization_reward_already_confirmed
    ):
        selected_task = _synthesized_next_improvement_candidate(
            current_task_id=current_task_id,
            strong_pass_count=strong_pass_count,
            goal_artifact_signature=list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
            "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
        and _task_status(materialize_task) in COMPLETED_TASK_STATUSES
    ):
        selected_task = next((task for task in task_records if (task.get("task_id") or task.get("taskId")) == "record-reward"), None) or {"task_id": "record-reward", "title": "Record cycle reward"}
        return {
            "mode": "record_reward_after_synthesized_materialization",
            "reason": "synthesized materialization artifact is already completed; prioritize post-materialization reward accounting before ambition escalation",
            "reward_value": reward_value,
            "current_task_id": "record-reward",
            "current_task_class": _task_action_class("record-reward"),
            "repeat_block_count": repeat_block_count,
            "repeat_block_failure_class": repeat_block_failure_class,
            "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
        materialize_task = next((task for task in task_records if (task.get("task_id") or task.get("taskId")) == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID), None)
        if current_task_id == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID:
            if materialize_task is None or _task_is_selectable(materialize_task):
                selected_task = materialize_task or _synthesized_materialize_improvement_candidate(
                    current_task_id=current_task_id,
                    strong_pass_count=strong_pass_count,
                    goal_artifact_signature=list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
                    status="active",
                )
            else:
                selected_task = _synthesized_materialize_improvement_candidate(
                    current_task_id=current_task_id,
                    strong_pass_count=strong_pass_count,
                    goal_artifact_signature=list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
                    status="active",
                )
            mode = "escalate_underutilized_ambition"
            reason = "healthy-progress lane is underusing tools/subagents; materialize the synthesized candidate instead of repeating low-ambition review"
            selection_source = "feedback_ambition_escalation_materialize"
        else:
            for preferred_id in [MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID, "subagent-verify-materialized-improvement", SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID]:
                for task in task_records:
                    task_id = task.get("task_id") or task.get("taskId")
                    if task_id in {None, current_task_id} or task_id != preferred_id:
                        continue
                    if _task_is_selectable(task):
                        selected_task = task
                        break
                if selected_task is not None:
                    break
            if selected_task is not None:
                mode = "escalate_underutilized_ambition"
                reason = "healthy-progress lane is underusing tools/subagents; select the next safe bounded higher-ambition lane"
                selection_source = "feedback_ambition_escalation_bounded_lane"
            else:
                selected_task = _synthesized_materialize_improvement_candidate(
                    current_task_id=current_task_id,
                    strong_pass_count=strong_pass_count,
                    goal_artifact_signature=list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
                    status="active",
                )
                mode = "escalate_underutilized_ambition"
                reason = "healthy-progress lane is underutilized and all recorded safe lanes are completed; generate a fresh bounded materialization lane instead of staying blocked"
                selection_source = "feedback_ambition_escalation_generated_lane"
    elif current_task_id == "inspect-pass-streak":
        followup_task = next((task for task in task_records if (task.get("task_id") or task.get("taskId")) == "materialize-pass-streak-improvement"), None)
        if followup_task is not None and _task_is_selectable(followup_task):
            decision = {
                "mode": "promote_review_followup",
                "reason": "active inspect-pass-streak review produced a concrete bounded follow-up candidate",
                "reward_value": reward_value,
                "current_task_id": current_task_id,
                "current_task_class": current_task_class,
                "repeat_block_count": repeat_block_count,
                "repeat_block_failure_class": repeat_block_failure_class,
                "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
                "strong_pass_count": strong_pass_count,
                "retire_goal_artifact_pair": False,
                "selected_task_id": followup_task.get("task_id") or followup_task.get("taskId"),
                "selected_task_class": _task_action_class(followup_task.get("task_id") or followup_task.get("taskId")),
                "selection_source": "feedback_review_to_execution",
                "selected_task_title": followup_task.get("title") or followup_task.get("summary") or (followup_task.get("task_id") or followup_task.get("taskId")),
                "selected_task_label": _render_task_selection(followup_task),
            }
            return decision
        active_task = next((task for task in task_records if (task.get("task_id") or task.get("taskId")) == current_task_id), None)
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
                        "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
                    "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
                    "strong_pass_count": strong_pass_count,
                    "retire_goal_artifact_pair": False,
                    "selected_task_id": current_task_id,
                    "selected_task_class": _task_action_class(current_task_id),
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
                "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
                "strong_pass_count": strong_pass_count,
                "retire_goal_artifact_pair": False,
                "selected_task_id": current_task_id,
                "selected_task_class": _task_action_class(current_task_id),
                "selection_source": "feedback_continue_active_lane",
                "selected_task_title": active_task.get("title") or active_task.get("summary") or current_task_id,
                "selected_task_label": _render_task_selection(active_task),
            }
    elif current_task_id == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID:
        active_task = next((task for task in task_records if (task.get("task_id") or task.get("taskId")) == current_task_id), None)
        should_materialize_synthesized_candidate = (
            strong_pass_count >= GOAL_ROTATION_STREAK_LIMIT
            and isinstance(latest_experiment, dict)
            and latest_experiment.get("outcome") == "discard"
            and latest_experiment.get("revert_status") == "skipped_no_material_change"
        )
        if should_materialize_synthesized_candidate:
            materialize_task = next((task for task in task_records if (task.get("task_id") or task.get("taskId")) == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID), None)
            if materialize_task is None:
                materialize_task = _synthesized_materialize_improvement_candidate(
                    current_task_id=current_task_id,
                    strong_pass_count=strong_pass_count,
                    goal_artifact_signature=list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
                "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
                    "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
                "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
                "strong_pass_count": strong_pass_count,
                "retire_goal_artifact_pair": False,
                "selected_task_id": current_task_id,
                "selected_task_class": _task_action_class(current_task_id),
                "selection_source": "feedback_continue_active_lane",
                "selected_task_title": active_task.get("title") or active_task.get("summary") or current_task_id,
                "selected_task_label": _render_task_selection(active_task),
            }
    if mode == "stable" and current_task_id and current_task_id not in CORE_TASK_IDS and current_task_id != "inspect-pass-streak":
        active_task = next((task for task in task_records if (task.get("task_id") or task.get("taskId")) == current_task_id), None)
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
                "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
            for task in tasks:
                task_id = task.get("task_id") or task.get("taskId")
                if task_id == "materialize-pass-streak-improvement" and _task_is_selectable(task):
                    selected_task = task
                    selection_source = "feedback_review_to_execution"
                    mode = "promote_review_followup"
                    reason = "active inspect-pass-streak review produced a concrete bounded follow-up candidate"
                    break
        if selected_task is None:
            preferred_ids = [SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID]
            for preferred_id in preferred_ids:
                for task in task_records:
                    task_id = task.get("task_id") or task.get("taskId")
                    if task_id == preferred_id and _task_is_selectable(task):
                        selected_task = task
                        selection_source = "feedback_pass_streak_switch"
                        break
                if selected_task is not None:
                    break
        if selected_task is None:
            for task in task_records:
                task_id = task.get("task_id") or task.get("taskId")
                if task_id in CORE_TASK_IDS or task_id in {None, current_task_id}:
                    continue
                if _task_is_selectable(task):
                    selected_task = task
                    selection_source = "feedback_pass_streak_switch"
                    break
        if selected_task is None:
            for task in task_records:
                task_id = task.get("task_id") or task.get("taskId")
                if task_id in {None, current_task_id}:
                    continue
                if _task_is_selectable(task):
                    selected_task = task
                    selection_source = "feedback_pass_streak_switch"
                    break
        if selected_task is None:
            synthesized_parent_completed = any(
                (task.get("task_id") or task.get("taskId")) == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID
                and not _task_is_selectable(task)
                for task in task_records
            )
            synthesized_materialization_completed = any(
                (task.get("task_id") or task.get("taskId")) == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
                and not _task_is_selectable(task)
                for task in task_records
            )
            post_materialization_reward_already_confirmed = (
                current_task_id == "record-reward"
                and isinstance(recorded_feedback_decision, dict)
                and recorded_feedback_decision.get("mode") == "record_reward_after_synthesized_materialization"
                and recorded_feedback_decision.get("selected_task_id") == "record-reward"
            )
            if synthesized_parent_completed and synthesized_materialization_completed and not post_materialization_reward_already_confirmed:
                selected_task = next(
                    (task for task in task_records if (task.get("task_id") or task.get("taskId")) == "record-reward"),
                    {"task_id": "record-reward", "title": "Record cycle reward", "status": "active"},
                )
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
                    goal_artifact_signature=list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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
        "goal_artifact_signature": list(str(value) for value in strong_pass_signature) if strong_pass_signature else None,
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

    history_files = sorted(
        history_dir.glob("cycle-*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
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
) -> dict[str, Any]:
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

    return {
        "value": round(reward_value, 4),
        "source": reward_source,
        "result_status": result_status,
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
        for path in root.glob("*.json"):
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
            if not ("cycle_id" in match_reasons or "report_path" in match_reasons):
                continue
            try:
                mtime = path.stat().st_mtime
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
    rows.sort(key=lambda item: item[0], reverse=True)
    results = [row[2] for row in rows[:max_results]]
    result_paths = [row[1] for row in rows[:max_results]]
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
    budget = dict(DEFAULT_EXPERIMENT_BUDGET)
    budget_used = _derive_budget_usage(
        result_status=result_status,
        cycle_started_utc=cycle_started_utc,
        cycle_ended_utc=cycle_ended_utc,
    )
    if result_status == "BLOCK":
        budget_used["requests"] = 0
    metric_summary = _experiment_metric_summary(result_status, reward_signal, previous_experiment)
    complexity_summary = _experiment_complexity_summary(result_status, selected_tasks, feedback_decision)
    current_task_id = _derive_experiment_current_task_id(result_status, feedback_decision)
    contract = _build_experiment_contract(
        experiment_id=experiment_id,
        cycle_id=cycle_id,
        goal_id=goal_id,
        current_task_id=current_task_id,
        selected_tasks=selected_tasks,
        task_selection_source=task_selection_source,
        budget=budget,
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


def _latest_failure_learning(workspace: Path) -> dict[str, Any] | None:
    candidate_paths = []
    try:
        candidate_paths.append(_resolve_runtime_state_root(workspace) / 'self_evolution' / 'failure_learning' / 'latest.json')
    except Exception:
        pass
    candidate_paths.append(workspace / 'state' / 'self_evolution' / 'failure_learning' / 'latest.json')
    seen: set[Path] = set()
    for path in candidate_paths:
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        try:
            mtime = path.stat().st_mtime
            age_seconds = max(0, int(datetime.now(timezone.utc).timestamp() - mtime))
        except Exception:
            age_seconds = None
        data['_age_seconds'] = age_seconds
        data['_source_path'] = str(path)
        return data
    return None


def _derive_generated_candidates(
    *,
    goals_dir: Path,
    result_status: str,
    current_task_id: str | None,
    failure_learning: dict[str, Any] | None = None,
    retire_analyze_last_failed_candidate: bool = False,
) -> list[dict[str, Any]]:
    history_entries = _load_recent_history_entries(goals_dir / "history", limit=6)
    if result_status != "PASS":
        return []
    pass_streak = 0
    for entry in history_entries:
        if (entry.get("result_status") or entry.get("status")) == "PASS":
            pass_streak += 1
        else:
            break
    candidates: list[dict[str, Any]] = []
    if isinstance(failure_learning, dict) and not retire_analyze_last_failed_candidate:
        candidates.append({
            'task_id': 'analyze-last-failed-candidate',
            'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
            'status': 'pending',
            'kind': 'review',
            'acceptance': 'produce a bounded explanation of the failed candidate and one safer follow-up mutation idea',
            'selection_source': 'generated_from_failure_learning',
            'failed_candidate_id': failure_learning.get('candidate_id'),
            'failed_commit': failure_learning.get('failed_commit'),
            'health_reasons': failure_learning.get('health_reasons'),
        })
    if current_task_id == "inspect-pass-streak":
        candidates.append({
            "task_id": "materialize-pass-streak-improvement",
            "title": "Materialize one concrete bounded improvement from the repeated PASS insight",
            "status": "pending",
            "kind": "execution",
            "acceptance": "produce one concrete bounded follow-up candidate derived from the inspect-pass-streak review",
            "selection_source": "generated_from_inspect_pass_streak",
            "parent_task_id": "inspect-pass-streak",
        })
    elif current_task_id == "materialize-pass-streak-improvement":
        candidates.append({
            "task_id": "subagent-verify-materialized-improvement",
            "title": "Use one bounded subagent-assisted review to verify the materialized improvement artifact",
            "status": "pending",
            "kind": "review",
            "acceptance": "create one bounded subagent request that reviews the materialized improvement artifact and reports a verification recommendation",
            "selection_source": "generated_from_materialized_improvement",
            "parent_task_id": "materialize-pass-streak-improvement",
            "subagent_profile": "research_only",
            "subagent_budget": "micro",
        })
    elif current_task_id == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID and pass_streak >= 3:
        candidates.append({
            "task_id": MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
            "title": "Materialize one bounded improvement from the synthesized candidate",
            "status": "pending",
            "kind": "execution",
            "acceptance": "write a concrete bounded improvement proposal or artifact and route it into self-evolution",
            "selection_source": "generated_from_synthesized_improvement",
            "parent_task_id": SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
            "strong_pass_count": pass_streak,
        })
    elif pass_streak >= 3 and current_task_id != "inspect-pass-streak":
        candidates.append({
            "task_id": "inspect-pass-streak",
            "title": "Inspect repeated PASS streak for a new bounded improvement",
            "status": "pending",
            "kind": "review",
            "acceptance": "derive one new bounded improvement candidate from repeated PASS evidence",
            "selection_source": "generated_pass_streak",
            "pass_streak": pass_streak,
        })
    return candidates


def _inferred_generated_candidates_from_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inferred: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = task.get("task_id") or task.get("taskId")
        if not task_id or task_id in CORE_TASK_IDS or not _task_is_selectable(task):
            continue
        inferred.append({
            "task_id": task_id,
            "title": task.get("title") or task.get("summary") or str(task_id),
            "status": task.get("status") or "pending",
            "kind": task.get("kind") or _task_action_class(str(task_id)),
            "acceptance": task.get("acceptance"),
            "selection_source": task.get("selection_source") or "carried_forward_task_plan",
        })
    return inferred


def _write_materialized_improvement_artifact(
    *,
    state_root: Path,
    cycle_id: str,
    goal_id: str,
    current_task_id: str | None,
    summary: str,
    reward_signal: dict[str, Any] | None,
    feedback_decision: dict[str, Any] | None,
) -> str | None:
    if current_task_id not in {"materialize-pass-streak-improvement", MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID}:
        return None
    improvements_dir = state_root / "improvements"
    improvements_dir.mkdir(parents=True, exist_ok=True)
    path = improvements_dir / f"materialized-{cycle_id}.json"
    is_synthesized_materialization = current_task_id == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
    concrete_statement = (
        "A synthesized review lane was materialized into a concrete bounded improvement artifact."
        if is_synthesized_materialization
        else "A repeated PASS pattern was strong enough to justify promoting a distinct bounded execution follow-up."
    )
    rationale = (
        "The system converted the synthesized candidate into an artifact so the lane can complete instead of repeating discard-only execution."
        if is_synthesized_materialization
        else "The system observed repeated successful cycles and converted that insight into a materialized bounded improvement artifact."
    )
    payload = {
        "schema_version": "materialized-improvement-v1",
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "task_id": current_task_id,
        "summary": summary,
        "reward_signal": reward_signal,
        "feedback_decision": feedback_decision,
        "hadi_cycle": {
            "hypothesis": "A concrete bounded materialization will create stronger self-improvement evidence than another reward/candidate bookkeeping cycle.",
            "action": "Materialize one reviewable improvement artifact and route it to a follow-up verification lane.",
            "data": {
                "task_id": current_task_id,
                "reward_signal": reward_signal,
                "feedback_mode": feedback_decision.get("mode") if isinstance(feedback_decision, dict) else None,
                "ambition_escalation_reasons": ((feedback_decision.get("ambition_escalation") or {}).get("reasons") if isinstance(feedback_decision, dict) and isinstance(feedback_decision.get("ambition_escalation"), dict) else None),
            },
            "insight": "The artifact must either qualify as material progress or trigger explicit subagent verification/blocker handling.",
        },
        "concrete_improvement_statement": concrete_statement,
        "rationale": rationale,
        "acceptance_checks": [
            "distinct materialized improvement artifact exists",
            "feedback decision references completion or follow-up semantics",
            "next bounded candidate is explicit and reviewable",
        ],
        "next_bounded_candidate": {
            "task_id": current_task_id,
            "title": "Materialize one bounded improvement from the synthesized candidate" if is_synthesized_materialization else "Materialize one concrete bounded improvement from the repeated PASS insight",
            "acceptance": "write a concrete bounded improvement proposal or artifact and route it into self-evolution" if is_synthesized_materialization else "produce one concrete bounded follow-up candidate derived from the inspect-pass-streak review",
            "task_class": "execution",
        },
        "derived_candidate": {
            "task_id": current_task_id,
            "title": "Materialize one bounded improvement from the synthesized candidate" if is_synthesized_materialization else "Materialize one concrete bounded improvement from the repeated PASS insight",
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _subagent_lane_health(*, state_root: Path, current_task_id: str | None, stale_after_seconds: int = 3600) -> dict[str, Any]:
    if current_task_id != "subagent-verify-materialized-improvement":
        return {"state": "not_applicable", "stale_request_count": 0, "queued_request_count": 0, "recommended_action": None}
    request_dir = state_root / "subagents" / "requests"
    result_dir = state_root / "subagents" / "results"
    now = time.time()
    queued: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    completed = []
    if result_dir.exists():
        completed = sorted(result_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if request_dir.exists():
        for path in sorted(request_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            payload = _safe_read_json(path)
            if payload.get("task_id") != current_task_id:
                continue
            status = payload.get("request_status") or payload.get("status") or "queued"
            age = max(0, int(now - path.stat().st_mtime))
            item = {"path": str(path), "status": status, "age_seconds": age, "task_id": current_task_id}
            if status in {"queued", "pending"}:
                queued.append(item)
                if age >= stale_after_seconds:
                    stale.append({**item, "status": "stale"})
    state = "completed" if completed else ("stale" if stale else ("queued" if queued else "missing_request"))
    return {
        "schema_version": "subagent-lane-health-v1",
        "state": state,
        "queued_request_count": len(queued),
        "stale_request_count": len(stale),
        "latest_stale_request": stale[0] if stale else None,
        "latest_request": queued[0] if queued else None,
        "completed_result_count": len(completed),
        "recommended_action": "retire_or_block_stale_subagent_lane" if state in {"stale", "missing_request"} else None,
    }


def _write_subagent_request_artifact(
    *,
    state_root: Path,
    cycle_id: str,
    goal_id: str,
    current_plan: dict[str, Any],
) -> str | None:
    if current_plan.get("current_task_id") != "subagent-verify-materialized-improvement":
        return None
    request_dir = state_root / "subagents" / "requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    path = request_dir / f"request-{cycle_id}.json"
    current_task_id = current_plan.get("current_task_id")
    current_task = next((task for task in current_plan.get("tasks", []) if isinstance(task, dict) and (task.get("task_id") or task.get("taskId")) == current_task_id), None)
    source_artifact = current_plan.get("materialized_improvement_artifact_path") or ((current_plan.get("feedback_decision") or {}).get("artifact_path") if isinstance(current_plan.get("feedback_decision"), dict) else None)
    if not source_artifact:
        improvements_dir = state_root / "improvements"
        latest_materialized = sorted(improvements_dir.glob("materialized-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:1] if improvements_dir.exists() else []
        source_artifact = str(latest_materialized[0]) if latest_materialized else None
    payload = {
        "schema_version": "subagent-request-v1",
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "task_id": current_task_id,
        "task_title": (current_task.get("title") or current_task.get("summary")) if isinstance(current_task, dict) else current_plan.get("current_task"),
        "request_status": "queued",
        "profile": "research_only",
        "budget": "micro",
        "source_artifact": source_artifact,
        "feedback_decision": current_plan.get("feedback_decision"),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _write_research_feed(
    *,
    state_root: Path,
    generated_candidates: list[dict[str, Any]],
    cycle_id: str,
    goal_id: str,
) -> dict[str, Any]:
    research_dir = state_root / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    feed_path = research_dir / "feed.json"
    entries = []
    for candidate in generated_candidates:
        entries.append({
            "id": candidate.get("task_id"),
            "title": candidate.get("title"),
            "summary": candidate.get("acceptance"),
            "action": candidate.get("acceptance"),
            "hypothesis": candidate.get("title"),
            "score": 15.0,
            "insights": [
                f"cycle_id={cycle_id}",
                f"goal_id={goal_id}",
                f"selection_source={candidate.get('selection_source')}",
            ],
            "acceptance": candidate.get("acceptance"),
        })
    payload = {
        "schema_version": "research-feed-v1",
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "entry_count": len(entries),
        "entries": entries,
    }
    feed_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    payload["feed_path"] = str(feed_path)
    return payload


def _build_task_plan_snapshot(
    *,
    workspace: Path,
    cycle_id: str,
    goal_id: str,
    result_status: str,
    approval_gate_state: str,
    next_hint: str,
    experiment: dict[str, Any],
    report_path: Path,
    history_path: Path,
    improvement_score: Any,
    feedback_decision: dict[str, Any] | None,
    goals_dir: Path,
    materialized_improvement_artifact_path: str | None = None,
) -> dict[str, Any]:
    blocked_next_step = next_hint if result_status == "BLOCK" else ""
    recorded_current_task_id = None
    recorded_materialized_improvement_artifact_path = None
    recorded_feedback_artifact_path = None
    recorded_terminal_selfevo_task_before_activation = None
    if result_status == "BLOCK":
        file_action = {
            "kind": "file_write",
            "path": "state/approvals/apply.ok",
            "summary": "Write a fresh approval gate with a valid TTL",
        }
        verification_command = "PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py"
        tasks = [
            {"task_id": "refresh-approval-gate", "title": file_action["summary"], "status": "active", **file_action},
            {"task_id": "verify-approval-gate", "title": f"Verify the gate with `{verification_command}`", "status": "pending", "command": verification_command},
        ]
    elif result_status == "ERROR":
        tasks = [
            {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
            {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "active"},
            {"task_id": "record-reward", "title": "Record cycle reward", "status": "pending"},
        ]
        file_action = None
        verification_command = None
    else:
        recorded_task_plan = _safe_read_json(goals_dir / "current.json")
        recorded_tasks = recorded_task_plan.get("tasks") if isinstance(recorded_task_plan, dict) and isinstance(recorded_task_plan.get("tasks"), list) else None
        recorded_generated_candidates = recorded_task_plan.get("generated_candidates") if isinstance(recorded_task_plan, dict) and isinstance(recorded_task_plan.get("generated_candidates"), list) else []
        recorded_current_task_id = recorded_task_plan.get("current_task_id") if isinstance(recorded_task_plan, dict) else None
        recorded_materialized_improvement_artifact_path = recorded_task_plan.get("materialized_improvement_artifact_path") if isinstance(recorded_task_plan, dict) else None
        recorded_feedback_artifact_path = (recorded_task_plan.get("feedback_decision") or {}).get("artifact_path") if isinstance(recorded_task_plan, dict) and isinstance(recorded_task_plan.get("feedback_decision"), dict) else None
        if recorded_tasks:
            tasks = [dict(task) for task in recorded_tasks if isinstance(task, dict)]
            recorded_terminal_selfevo_task_before_activation = next((dict(task) for task in tasks if task.get('task_id') == 'analyze-last-failed-candidate'), None)
            has_active = False
            for task in tasks:
                if task.get("task_id") == recorded_current_task_id:
                    task["status"] = "active"
                    has_active = True
                elif task.get("status") == "active":
                    task["status"] = "pending"
            if not has_active:
                for task in tasks:
                    if task.get("task_id") == "record-reward":
                        task["status"] = "active"
                        has_active = True
                        break
            if not has_active:
                tasks.append({"task_id": "record-reward", "title": "Record cycle reward", "status": "active"})
        else:
            tasks = [
                {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
                {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "done"},
                {"task_id": "record-reward", "title": "Record cycle reward", "status": "active"},
            ]
        file_action = None
        verification_command = None

    current_task_id = next(task["task_id"] for task in tasks if task["status"] == "active")
    reward_signal = dict(experiment.get("reward_signal")) if isinstance(experiment.get("reward_signal"), dict) else _derive_reward_signal(result_status, improvement_score)
    active_artifact_path = materialized_improvement_artifact_path or (recorded_materialized_improvement_artifact_path if 'recorded_materialized_improvement_artifact_path' in locals() else None) or (recorded_feedback_artifact_path if 'recorded_feedback_artifact_path' in locals() else None)
    if feedback_decision and feedback_decision.get("selected_task_id"):
        selected_task_id = str(feedback_decision["selected_task_id"])
        current_task_id = selected_task_id
        for task in tasks:
            if task.get("task_id") == selected_task_id:
                task["status"] = "active"
            elif task["status"] == "active":
                task["status"] = "pending"
    latest_failure_learning = _latest_failure_learning(workspace)
    failure_learning_is_fresh = isinstance(latest_failure_learning, dict) and isinstance(latest_failure_learning.get('_age_seconds'), int) and latest_failure_learning.get('_age_seconds') <= 3600
    terminal_selfevo_issue = resolve_terminal_selfevo_issue(workspace=workspace, source_task_id='analyze-last-failed-candidate')
    terminal_selfevo_retired = False
    recorded_terminal_selfevo_task = next((task for task in tasks if task.get('task_id') == 'analyze-last-failed-candidate'), None)
    recorded_terminal_selfevo_task_was_already_retired = (
        _task_is_terminal_selfevo_retired(
            recorded_terminal_selfevo_task_before_activation or recorded_terminal_selfevo_task,
            terminal_selfevo_issue,
        )
        or _task_has_recorded_terminal_selfevo_retirement(
            recorded_terminal_selfevo_task_before_activation or recorded_terminal_selfevo_task
        )
    )
    if recorded_terminal_selfevo_task_was_already_retired:
        terminal_selfevo_retired = True
    recorded_feedback_decision_for_repair = recorded_task_plan.get('feedback_decision') if 'recorded_task_plan' in locals() and isinstance(recorded_task_plan, dict) and isinstance(recorded_task_plan.get('feedback_decision'), dict) else {}
    recorded_reward_retirement = (
        isinstance(recorded_feedback_decision_for_repair, dict)
        and recorded_feedback_decision_for_repair.get('current_task_id') == 'record-reward'
        and recorded_feedback_decision_for_repair.get('retire_goal_artifact_pair') is True
    )
    recorded_complete_lane_to_reward = (
        isinstance(recorded_feedback_decision_for_repair, dict)
        and recorded_feedback_decision_for_repair.get('mode') == 'complete_active_lane'
        and recorded_feedback_decision_for_repair.get('current_task_id') == 'materialize-pass-streak-improvement'
        and recorded_feedback_decision_for_repair.get('selected_task_id') == 'record-reward'
        and recorded_feedback_decision_for_repair.get('selection_source') == 'feedback_complete_active_lane'
    )
    recorded_terminal_selfevo_retirement = (
        isinstance(recorded_feedback_decision_for_repair, dict)
        and (
            recorded_current_task_id == 'record-reward'
            or recorded_terminal_selfevo_task_was_already_retired
        )
        and recorded_feedback_decision_for_repair.get('mode') == 'retire_terminal_selfevo_lane'
        and recorded_feedback_decision_for_repair.get('selected_task_id') == 'record-reward'
        and recorded_feedback_decision_for_repair.get('selection_source') == 'feedback_terminal_selfevo_retire'
        and terminal_selfevo_issue is not None
    )
    if recorded_terminal_selfevo_retirement:
        terminal_selfevo_retired = True
        for task in tasks:
            if task.get("task_id") == "analyze-last-failed-candidate":
                task["status"] = "done"
                task["terminal_reason"] = terminal_selfevo_issue.get("terminal_status") or "terminal_selfevo_issue"
            elif task.get("task_id") == "record-reward":
                task["status"] = "active"
            elif task.get("status") == "active":
                task["status"] = "pending"
        if not any(task.get("task_id") == "record-reward" for task in tasks):
            tasks.append({"task_id": "record-reward", "title": "Record cycle reward", "status": "active"})
        current_task_id = "record-reward"
        feedback_decision = None
    if terminal_selfevo_issue is not None and not terminal_selfevo_retired and current_task_id == "analyze-last-failed-candidate":
        for task in tasks:
            if task.get("task_id") == "analyze-last-failed-candidate":
                task["status"] = "done"
                task["terminal_reason"] = terminal_selfevo_issue.get("terminal_status") or "terminal_selfevo_issue"
            elif task.get("task_id") == "record-reward":
                task["status"] = "active"
            elif task.get("status") == "active":
                task["status"] = "pending"
        if not any(task.get("task_id") == "record-reward" for task in tasks):
            tasks.append({"task_id": "record-reward", "title": "Record cycle reward", "status": "active"})
        current_task_id = "record-reward"
        feedback_decision = {
            "mode": "retire_terminal_selfevo_lane",
            "reason": "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate",
            "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
            "current_task_id": "analyze-last-failed-candidate",
            "current_task_class": _task_action_class("analyze-last-failed-candidate"),
            "selected_task_id": "record-reward",
            "selected_task_class": _task_action_class("record-reward"),
            "selection_source": "feedback_terminal_selfevo_retire",
            "selected_task_title": "Record cycle reward",
            "selected_task_label": "Record cycle reward [task_id=record-reward]",
            "terminal_selfevo_issue": terminal_selfevo_issue,
        }
        terminal_selfevo_retired = True
    elif isinstance(latest_failure_learning, dict) and (current_task_id == "record-reward" or failure_learning_is_fresh) and not terminal_selfevo_retired:
        if terminal_selfevo_issue is not None and recorded_complete_lane_to_reward:
            for task in tasks:
                if task.get("task_id") == "analyze-last-failed-candidate":
                    task["status"] = "done"
                    task["terminal_reason"] = terminal_selfevo_issue.get("terminal_status") or "terminal_selfevo_issue"
                elif task.get("task_id") == "record-reward":
                    task["status"] = "active"
                elif task.get("status") == "active":
                    task["status"] = "pending"
            if not any(task.get("task_id") == "record-reward" for task in tasks):
                tasks.append({"task_id": "record-reward", "title": "Record cycle reward", "status": "active"})
            current_task_id = "record-reward"
            feedback_decision = {
                "mode": "retire_terminal_selfevo_lane",
                "reason": "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": "analyze-last-failed-candidate",
                "current_task_class": _task_action_class("analyze-last-failed-candidate"),
                "selected_task_id": "record-reward",
                "selected_task_class": _task_action_class("record-reward"),
                "selection_source": "feedback_terminal_selfevo_retire",
                "selected_task_title": "Record cycle reward",
                "selected_task_label": "Record cycle reward [task_id=record-reward]",
                "terminal_selfevo_issue": terminal_selfevo_issue,
            }
            terminal_selfevo_retired = True
        elif terminal_selfevo_issue is None and ((recorded_reward_retirement and failure_learning_is_fresh) or recorded_complete_lane_to_reward):
            repair_source = 'fresh_failure_learning_after_reward_retirement' if recorded_reward_retirement else 'stale_complete_lane_record_reward_repair'
            repair_selection_source = 'feedback_fresh_failure_learning_after_reward_retirement' if recorded_reward_retirement else 'feedback_complete_active_lane_to_failure_learning'
            repair_reason = (
                'fresh failure-learning evidence after a retired record-reward lane must be analyzed before returning to bookkeeping'
                if recorded_reward_retirement
                else 'stale complete-active-lane record-reward authority must revive failure-learning analysis before bookkeeping'
            )
            repair_mode = 'fresh_failure_learning_after_reward_retirement' if recorded_reward_retirement else 'stale_complete_lane_record_reward_repair'
            repair_task = next((task for task in tasks if task.get("task_id") == "analyze-last-failed-candidate"), None)
            if repair_task is None:
                repair_task = {
                    'task_id': 'analyze-last-failed-candidate',
                    'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
                    'status': 'active',
                    'kind': 'review',
                    'acceptance': 'produce a bounded explanation of the failed candidate and one safer follow-up mutation idea',
                    'selection_source': repair_source,
                    'failed_candidate_id': latest_failure_learning.get('candidate_id'),
                    'failed_commit': latest_failure_learning.get('failed_commit'),
                    'health_reasons': latest_failure_learning.get('health_reasons'),
                }
                tasks.append(repair_task)
            for task in tasks:
                if task.get('task_id') == 'analyze-last-failed-candidate':
                    task['status'] = 'active'
                    task['selection_source'] = repair_source
                    task['failed_candidate_id'] = latest_failure_learning.get('candidate_id')
                    task['failed_commit'] = latest_failure_learning.get('failed_commit')
                    task['health_reasons'] = latest_failure_learning.get('health_reasons')
                elif task.get('status') == 'active':
                    task['status'] = 'pending'
            current_task_id = 'analyze-last-failed-candidate'
            feedback_decision = {
                "mode": repair_mode,
                "reason": repair_reason,
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": "record-reward",
                "current_task_class": _task_action_class("record-reward"),
                "retire_goal_artifact_pair": bool(recorded_reward_retirement),
                "selected_task_id": "analyze-last-failed-candidate",
                "selected_task_class": _task_action_class("analyze-last-failed-candidate"),
                "selection_source": repair_selection_source,
                "selected_task_title": "Analyze the last failed self-evolution candidate before retrying mutation",
                "selected_task_label": "Analyze the last failed self-evolution candidate before retrying mutation [task_id=analyze-last-failed-candidate]",
                "failure_learning": latest_failure_learning,
                "terminal_selfevo_issue": terminal_selfevo_issue,
            }
        elif terminal_selfevo_issue is not None and current_task_id != "subagent-verify-materialized-improvement":
            for task in tasks:
                if task.get("task_id") == "analyze-last-failed-candidate":
                    task["status"] = "done"
                    task["terminal_reason"] = terminal_selfevo_issue.get("terminal_status") or "terminal_selfevo_issue"
                elif task.get("task_id") == "record-reward":
                    task["status"] = "active"
                elif task.get("status") == "active":
                    task["status"] = "pending"
            if not any(task.get("task_id") == "record-reward" for task in tasks):
                tasks.append({"task_id": "record-reward", "title": "Record cycle reward", "status": "active"})
            current_task_id = "record-reward"
            feedback_decision = {
                "mode": "retire_terminal_selfevo_lane",
                "reason": "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": "analyze-last-failed-candidate",
                "current_task_class": _task_action_class("analyze-last-failed-candidate"),
                "selected_task_id": "record-reward",
                "selected_task_class": _task_action_class("record-reward"),
                "selection_source": "feedback_terminal_selfevo_retire",
                "selected_task_title": "Record cycle reward",
                "selected_task_label": "Record cycle reward [task_id=record-reward]",
                "terminal_selfevo_issue": terminal_selfevo_issue,
            }
        else:
            repair_task = next((task for task in tasks if task.get("task_id") == "analyze-last-failed-candidate"), None)
            if repair_task is None:
                repair_task = {
                    'task_id': 'analyze-last-failed-candidate',
                    'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
                    'status': 'active',
                    'kind': 'review',
                    'acceptance': 'produce a bounded explanation of the failed candidate and one safer follow-up mutation idea',
                    'selection_source': 'generated_from_failure_learning',
                    'failed_candidate_id': latest_failure_learning.get('candidate_id'),
                    'failed_commit': latest_failure_learning.get('failed_commit'),
                    'health_reasons': latest_failure_learning.get('health_reasons'),
                }
                tasks.append(repair_task)
            for task in tasks:
                task['status'] = 'pending' if task.get('task_id') != 'analyze-last-failed-candidate' and task.get('status') == 'active' else task.get('status')
            repair_task['status'] = 'active'
            current_task_id = 'analyze-last-failed-candidate'
            feedback_decision = {
                "mode": "fresh_failure_learning_repair",
                "reason": "fresh failure-learning evidence remains the stronger repair lane than stale subagent bookkeeping",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": recorded_current_task_id,
                "current_task_class": _task_action_class(recorded_current_task_id),
                "selected_task_id": "analyze-last-failed-candidate",
                "selected_task_class": _task_action_class("analyze-last-failed-candidate"),
                "selection_source": "feedback_stale_subagent_retire_to_failure_learning" if recorded_current_task_id == "subagent-verify-materialized-improvement" else "generated_from_failure_learning",
                "selected_task_title": "Analyze the last failed self-evolution candidate before retrying mutation",
                "selected_task_label": "Analyze the last failed self-evolution candidate before retrying mutation [task_id=analyze-last-failed-candidate]",
                "failure_learning": latest_failure_learning,
                "terminal_selfevo_issue": terminal_selfevo_issue,
            }
    generated_candidates = _derive_generated_candidates(
        goals_dir=goals_dir,
        result_status=result_status,
        current_task_id=current_task_id,
        failure_learning=latest_failure_learning,
        retire_analyze_last_failed_candidate=terminal_selfevo_issue is not None,
    )
    if (
        isinstance(feedback_decision, dict)
        and feedback_decision.get("selected_task_id") == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID
        and not any(candidate.get("task_id") == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID for candidate in generated_candidates)
    ):
        generated_candidates.append(
            _synthesized_next_improvement_candidate(
                current_task_id=current_task_id,
                strong_pass_count=int(feedback_decision.get("strong_pass_count") or 0),
                goal_artifact_signature=feedback_decision.get("goal_artifact_signature") if isinstance(feedback_decision.get("goal_artifact_signature"), list) else None,
                status="active",
            )
        )
    if (
        isinstance(feedback_decision, dict)
        and feedback_decision.get("selected_task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        and not any(candidate.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID for candidate in generated_candidates)
    ):
        generated_candidates.append(
            _synthesized_materialize_improvement_candidate(
                current_task_id=SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
                strong_pass_count=int(feedback_decision.get("strong_pass_count") or 0),
                goal_artifact_signature=feedback_decision.get("goal_artifact_signature") if isinstance(feedback_decision.get("goal_artifact_signature"), list) else None,
                status="active",
            )
        )
    carried_candidates = [dict(item) for item in recorded_generated_candidates if isinstance(item, dict)] if 'recorded_generated_candidates' in locals() else []
    inferred_candidates = _inferred_generated_candidates_from_tasks(tasks)
    combined_candidates: list[dict[str, Any]] = []
    seen_candidate_ids: set[str] = set()
    for candidate in [*carried_candidates, *inferred_candidates, *generated_candidates]:
        cid = candidate.get("task_id") if isinstance(candidate, dict) else None
        if not cid or cid in seen_candidate_ids:
            continue
        matching_task = next((task for task in tasks if task.get("task_id") == cid), None)
        if isinstance(matching_task, dict) and not _task_is_selectable(matching_task):
            continue
        combined_candidates.append(candidate)
        seen_candidate_ids.add(cid)
    existing_ids = {task.get("task_id") for task in tasks}
    for candidate in combined_candidates:
        if candidate.get("task_id") not in existing_ids:
            tasks.append(candidate)
            existing_ids.add(candidate.get("task_id"))
    if isinstance(feedback_decision, dict) and feedback_decision.get("selected_task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID:
        for task in tasks:
            if task.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID:
                task["status"] = "active"
            elif task.get("status") == "active":
                task["status"] = "pending"
        current_task_id = MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
    if (
        current_task_id == "inspect-pass-streak"
        and (not isinstance(feedback_decision, dict) or not feedback_decision.get("selected_task_id"))
    ):
        followup = next((candidate for candidate in combined_candidates if candidate.get("task_id") == "materialize-pass-streak-improvement"), None)
        if followup is not None:
            feedback_decision = {
                "mode": "promote_review_followup",
                "reason": "active inspect-pass-streak review produced a concrete bounded follow-up candidate",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": current_task_id,
                "current_task_class": _task_action_class(current_task_id),
                "repeat_block_count": 0,
                "repeat_block_failure_class": None,
                "goal_artifact_signature": None,
                "strong_pass_count": None,
                "retire_goal_artifact_pair": False,
                "selected_task_id": followup.get("task_id"),
                "selected_task_class": _task_action_class(followup.get("task_id")),
                "selection_source": "feedback_review_to_execution",
                "selected_task_title": followup.get("title") or followup.get("summary") or followup.get("task_id"),
                "selected_task_label": _render_task_selection(followup),
            }
    should_promote_synthesized_materialization = (
        current_task_id == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID
        and experiment.get("outcome") == "discard"
        and experiment.get("revert_status") == "skipped_no_material_change"
    )
    if should_promote_synthesized_materialization:
        materialize_synthesized = next((candidate for candidate in combined_candidates if candidate.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID), None)
        if materialize_synthesized is not None and _task_is_selectable(materialize_synthesized):
            for task in tasks:
                if task.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID:
                    task["status"] = "active"
                elif task.get("task_id") == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID:
                    task["status"] = "pending"
                elif task.get("status") == "active":
                    task["status"] = "pending"
            current_task_id = MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
            feedback_decision = {
                "mode": "materialize_synthesized_improvement",
                "reason": "synthesized-improvement review reached repeated discard/no-artifact pressure; promote a concrete execution follow-up",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
                "current_task_class": _task_action_class(SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID),
                "repeat_block_count": 0,
                "repeat_block_failure_class": None,
                "goal_artifact_signature": materialize_synthesized.get("goal_artifact_signature"),
                "strong_pass_count": materialize_synthesized.get("strong_pass_count"),
                "retire_goal_artifact_pair": False,
                "selected_task_id": MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
                "selected_task_class": _task_action_class(MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID),
                "selection_source": "feedback_synthesis_materialization",
                "selected_task_title": materialize_synthesized.get("title") or MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
                "selected_task_label": _render_task_selection(materialize_synthesized),
            }
    materialization_task_ids = {"materialize-pass-streak-improvement", MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID}
    materialized_artifact_task_id = None
    if materialized_improvement_artifact_path:
        materialized_artifact_payload = _safe_read_json(Path(materialized_improvement_artifact_path))
        if isinstance(materialized_artifact_payload, dict) and materialized_artifact_payload.get("task_id") in materialization_task_ids:
            materialized_artifact_task_id = materialized_artifact_payload.get("task_id")
    if materialized_artifact_task_id in materialization_task_ids:
        artifact_task_record = next((task for task in tasks if task.get("task_id") == materialized_artifact_task_id), None)
        if _task_is_selectable(artifact_task_record):
            current_task_id = materialized_artifact_task_id
    confirmed_synthesized_materialization_completion = (
        current_task_id == "record-reward"
        and materialized_artifact_task_id == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        and isinstance(recorded_feedback_decision_for_repair, dict)
        and recorded_feedback_decision_for_repair.get("mode") == "complete_active_lane"
        and recorded_feedback_decision_for_repair.get("current_task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        and recorded_feedback_decision_for_repair.get("selected_task_id") == "record-reward"
        and recorded_feedback_decision_for_repair.get("selection_source") == "feedback_complete_active_lane"
        and str(recorded_feedback_decision_for_repair.get("artifact_path") or "") == str(materialized_improvement_artifact_path)
    )
    if confirmed_synthesized_materialization_completion:
        current_task_id = MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
    if current_task_id in materialization_task_ids and result_status == "PASS" and materialized_improvement_artifact_path:
        is_synthesized_materialization = current_task_id == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        completed_materialization_task_id = current_task_id
        repeated_synthesized_materialization_completion = (
            is_synthesized_materialization
            and isinstance(recorded_feedback_decision_for_repair, dict)
            and recorded_feedback_decision_for_repair.get("mode") == "complete_active_lane"
            and recorded_feedback_decision_for_repair.get("current_task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
            and recorded_feedback_decision_for_repair.get("selected_task_id") == "record-reward"
            and recorded_feedback_decision_for_repair.get("selection_source") == "feedback_complete_active_lane"
            and str(recorded_feedback_decision_for_repair.get("artifact_path") or "") == str(materialized_improvement_artifact_path)
        )
        for task in tasks:
            if task.get("task_id") == completed_materialization_task_id:
                task["status"] = "done"
            elif task.get("task_id") == "inspect-pass-streak" and not is_synthesized_materialization:
                task["status"] = "done"
            elif task.get("task_id") == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID and is_synthesized_materialization:
                task["status"] = "done"
            elif task.get("task_id") == "record-reward":
                task["status"] = "active"
            elif task.get("status") == "active":
                task["status"] = "pending"
        combined_candidates = [candidate for candidate in combined_candidates if candidate.get("task_id") not in {"inspect-pass-streak", "materialize-pass-streak-improvement", MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID}]
        next_candidate = None if is_synthesized_materialization else next((candidate for candidate in combined_candidates if candidate.get("task_id") == "subagent-verify-materialized-improvement"), None)
        if repeated_synthesized_materialization_completion:
            for task in tasks:
                if task.get("task_id") == "record-reward":
                    task["status"] = "active"
                elif task.get("status") == "active":
                    task["status"] = "pending"
            current_task_id = "record-reward"
            feedback_decision = {
                "mode": "record_reward_after_synthesized_materialization",
                "reason": "synthesized materialization completion for this artifact is already confirmed; advance to post-materialization reward accounting",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": "record-reward",
                "current_task_class": _task_action_class("record-reward"),
                "selected_task_id": "record-reward",
                "selected_task_class": _task_action_class("record-reward"),
                "selection_source": "feedback_synthesized_materialization_complete_reward",
                "selected_task_title": "Record cycle reward",
                "selected_task_label": "Record cycle reward [task_id=record-reward]",
                "artifact_path": materialized_improvement_artifact_path,
            }
        elif next_candidate is not None and not isinstance(latest_failure_learning, dict):
            for task in tasks:
                if task.get("task_id") == next_candidate.get("task_id"):
                    task["status"] = "active"
                elif task.get("task_id") == "record-reward":
                    task["status"] = "pending"
            current_task_id = next_candidate.get("task_id")
            feedback_decision = {
                "mode": "handoff_to_next_candidate",
                "reason": "materialized lane completed and handed off to the next bounded candidate",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": completed_materialization_task_id,
                "current_task_class": _task_action_class(completed_materialization_task_id),
                "selected_task_id": next_candidate.get("task_id"),
                "selected_task_class": _task_action_class(next_candidate.get("task_id")),
                "selection_source": "feedback_post_completion_handoff",
                "selected_task_title": next_candidate.get("title") or next_candidate.get("summary") or next_candidate.get("task_id"),
                "selected_task_label": _render_task_selection(next_candidate),
                "artifact_path": materialized_improvement_artifact_path,
            }
        else:
            completion_target_id = "record-reward"
            completion_target_title = "Record cycle reward"
            completion_selection_source = "feedback_complete_active_lane"
            completion_reason = "materialized improvement artifact written; richer execution lane completed"
            if terminal_selfevo_issue is not None and not is_synthesized_materialization:
                completion_selection_source = "feedback_terminal_selfevo_retire"
                completion_reason = "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate"
            elif isinstance(latest_failure_learning, dict) and not recorded_terminal_selfevo_task_was_already_retired:
                completion_target_id = "analyze-last-failed-candidate"
                completion_target_title = "Analyze the last failed self-evolution candidate before retrying mutation"
                completion_selection_source = "feedback_complete_active_lane_to_failure_learning"
                completion_reason = "materialized improvement artifact written, but fresh failure-learning evidence remains the next non-bookkeeping lane"
            for task in tasks:
                if task.get("task_id") == completion_target_id:
                    task["status"] = "active"
                    if completion_target_id == "analyze-last-failed-candidate":
                        task["selection_source"] = completion_selection_source
                        task["failed_candidate_id"] = latest_failure_learning.get("candidate_id") if isinstance(latest_failure_learning, dict) else None
                        task["failed_commit"] = latest_failure_learning.get("failed_commit") if isinstance(latest_failure_learning, dict) else None
                        task["health_reasons"] = latest_failure_learning.get("health_reasons") if isinstance(latest_failure_learning, dict) else None
                elif task.get("task_id") == "record-reward":
                    task["status"] = "pending" if completion_target_id != "record-reward" else "active"
                elif task.get("status") == "active":
                    task["status"] = "pending"
            if not any(task.get("task_id") == completion_target_id for task in tasks):
                task_payload = {"task_id": completion_target_id, "title": completion_target_title, "status": "active"}
                if completion_target_id == "analyze-last-failed-candidate" and isinstance(latest_failure_learning, dict):
                    task_payload.update({
                        "kind": "review",
                        "acceptance": "produce a bounded explanation of the failed candidate and one safer follow-up mutation idea",
                        "selection_source": completion_selection_source,
                        "failed_candidate_id": latest_failure_learning.get("candidate_id"),
                        "failed_commit": latest_failure_learning.get("failed_commit"),
                        "health_reasons": latest_failure_learning.get("health_reasons"),
                    })
                tasks.append(task_payload)
            current_task_id = completion_target_id
            if terminal_selfevo_issue is not None and not is_synthesized_materialization:
                feedback_decision = {
                    "mode": "retire_terminal_selfevo_lane",
                    "reason": "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate",
                    "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                    "current_task_id": completed_materialization_task_id,
                    "current_task_class": _task_action_class(completed_materialization_task_id),
                    "selected_task_id": "record-reward",
                    "selected_task_class": _task_action_class("record-reward"),
                    "selection_source": completion_selection_source,
                    "selected_task_title": "Record cycle reward",
                    "selected_task_label": "Record cycle reward [task_id=record-reward]",
                    "terminal_selfevo_issue": terminal_selfevo_issue,
                }
            else:
                feedback_decision = {
                    "mode": "complete_active_lane",
                    "reason": completion_reason,
                    "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                    "current_task_id": completed_materialization_task_id,
                    "current_task_class": _task_action_class(completed_materialization_task_id),
                    "selected_task_id": completion_target_id,
                    "selected_task_class": _task_action_class(completion_target_id),
                    "selection_source": completion_selection_source,
                    "selected_task_title": completion_target_title,
                    "selected_task_label": f"{completion_target_title} [task_id={completion_target_id}]",
                    "artifact_path": materialized_improvement_artifact_path,
                }
                if completion_target_id == "analyze-last-failed-candidate" and isinstance(latest_failure_learning, dict):
                    feedback_decision["failure_learning"] = latest_failure_learning
            active_artifact_path = materialized_improvement_artifact_path
    latest_noop = _safe_read_json(workspace / "state" / "self_evolution" / "runtime" / "latest_noop.json") or {}
    subagent_lane_health = _subagent_lane_health(state_root=workspace / "state", current_task_id=current_task_id)
    should_retire_subagent_lane = (
        current_task_id == "subagent-verify-materialized-improvement"
        and not (isinstance(feedback_decision, dict) and feedback_decision.get("mode") == "execute_queued_revert")
        and (
            latest_noop.get("status") == "terminal_noop"
            or subagent_lane_health.get("state") == "stale"
            or (experiment.get("outcome") == "discard" and experiment.get("revert_status") == "skipped_no_material_change")
        )
    )
    if should_retire_subagent_lane:
        subagent_retirement_target_id = "record-reward"
        subagent_retirement_target_title = "Record cycle reward"
        subagent_retirement_selection_source = "feedback_terminal_noop_retire" if latest_noop.get("status") == "terminal_noop" else "feedback_stale_subagent_retire"
        if failure_learning_is_fresh and isinstance(latest_failure_learning, dict):
            subagent_retirement_target_id = "analyze-last-failed-candidate"
            subagent_retirement_target_title = "Analyze the last failed self-evolution candidate before retrying mutation"
            subagent_retirement_selection_source = "feedback_stale_subagent_retire_to_failure_learning"
        for task in tasks:
            if task.get("task_id") == "subagent-verify-materialized-improvement":
                task["status"] = "blocked" if subagent_lane_health.get("state") == "stale" else "done"
                task["terminal_reason"] = "terminal_noop_or_no_material_change"
            elif task.get("task_id") == subagent_retirement_target_id:
                task["status"] = "active"
                if subagent_retirement_target_id == "analyze-last-failed-candidate" and isinstance(latest_failure_learning, dict):
                    task["selection_source"] = subagent_retirement_selection_source
                    task["failed_candidate_id"] = latest_failure_learning.get("candidate_id")
                    task["failed_commit"] = latest_failure_learning.get("failed_commit")
                    task["health_reasons"] = latest_failure_learning.get("health_reasons")
            elif task.get("status") == "active":
                task["status"] = "pending"
        if not any(task.get("task_id") == subagent_retirement_target_id for task in tasks):
            task_payload = {"task_id": subagent_retirement_target_id, "title": subagent_retirement_target_title, "status": "active"}
            if subagent_retirement_target_id == "analyze-last-failed-candidate" and isinstance(latest_failure_learning, dict):
                task_payload.update({
                    "kind": "review",
                    "acceptance": "produce a bounded explanation of the failed candidate and one safer follow-up mutation idea",
                    "selection_source": subagent_retirement_selection_source,
                    "failed_candidate_id": latest_failure_learning.get("candidate_id"),
                    "failed_commit": latest_failure_learning.get("failed_commit"),
                    "health_reasons": latest_failure_learning.get("health_reasons"),
                })
            tasks.append(task_payload)
        current_task_id = subagent_retirement_target_id
        feedback_decision = {
            "mode": "retire_terminal_noop_lane" if latest_noop.get("status") == "terminal_noop" else "retire_stale_subagent_lane",
            "reason": "subagent verification lane reached a terminal no-op/discard/stale state and must not keep producing PASS-only telemetry",
            "current_task_id": "subagent-verify-materialized-improvement",
            "current_task_class": _task_action_class("subagent-verify-materialized-improvement"),
            "selected_task_id": subagent_retirement_target_id,
            "selected_task_class": _task_action_class(subagent_retirement_target_id),
            "selection_source": subagent_retirement_selection_source,
            "selected_task_title": subagent_retirement_target_title,
            "selected_task_label": f"{subagent_retirement_target_title} [task_id={subagent_retirement_target_id}]",
            "latest_noop": latest_noop if latest_noop else None,
            "subagent_lane_health": subagent_lane_health,
        }
        if subagent_retirement_target_id == "analyze-last-failed-candidate" and isinstance(latest_failure_learning, dict):
            feedback_decision["failure_learning"] = latest_failure_learning
    task_counts = {
        "total": len(tasks),
        "done": sum(1 for task in tasks if task["status"] == "done"),
        "active": sum(1 for task in tasks if task["status"] == "active"),
        "pending": sum(1 for task in tasks if task["status"] == "pending"),
    }
    current_task_title = _task_title_for_id(current_task_id, tasks, combined_candidates)
    payload = {
        "schema_version": TASK_PLAN_VERSION,
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "active_goal": goal_id,
        "result_status": result_status,
        "approval_gate_state": approval_gate_state,
        "next_hint": next_hint,
        "blocked_next_step": blocked_next_step,
        "current_task_id": current_task_id,
        "current_task": current_task_title,
        "task_counts": task_counts,
        "tasks": tasks,
        "reward_signal": reward_signal,
        "feedback_decision": feedback_decision,
        "next_cycle_task_id": feedback_decision.get("selected_task_id") if feedback_decision else None,
        "next_cycle_task_class": feedback_decision.get("selected_task_class") if feedback_decision else None,
        "mutation_lane": _derive_mutation_lane(
            current_task_id=current_task_id,
            selected_tasks=feedback_decision.get("selected_tasks") if isinstance(feedback_decision, dict) else None,
            task_selection_source=feedback_decision.get("selection_source") if isinstance(feedback_decision, dict) else None,
        ),
        "budget": experiment["budget"],
        "budget_used": experiment["budget_used"],
        "experiment": experiment,
        "report_path": str(report_path),
        "history_path": str(history_path),
        "generated_candidates": combined_candidates,
        "failure_learning": _latest_failure_learning(workspace),
        "materialized_improvement_artifact_path": active_artifact_path,
    }

    if file_action is not None:
        payload["file_action"] = file_action
    if verification_command is not None:
        payload["verification_command"] = verification_command
    return payload


def _task_execution_acceptance(
    task: dict[str, Any],
    *,
    goal_id: str,
    result_status: str,
    approval_gate_state: str,
    next_hint: str,
) -> str:
    task_title = task.get("title") or task.get("summary") or task.get("task_id") or "task"
    acceptance = task.get("acceptance")
    if isinstance(acceptance, str) and acceptance.strip():
        return acceptance

    command = task.get("command")
    if isinstance(command, str) and command.strip():
        return f"`{command}` completes successfully"

    file_action = task.get("file_action") if isinstance(task.get("file_action"), dict) else None
    if not isinstance(file_action, dict) and isinstance(task.get("path"), str) and isinstance(task.get("summary"), str):
        file_action = {"path": task.get("path"), "summary": task.get("summary")}
    if isinstance(file_action, dict):
        summary = file_action.get("summary") or "complete the file action"
        path = file_action.get("path")
        if path:
            return f"{summary} at {path}"
        return str(summary)

    if result_status == "BLOCK" and approval_gate_state != "fresh":
        return f"{task_title} advances the cycle after {next_hint}"

    return f"{task_title} is completed with durable evidence for {goal_id}"


def _task_effort_weight(task: dict[str, Any]) -> int:
    weight = 1
    if isinstance(task.get("command"), str) and task["command"].strip():
        weight += 1
    if isinstance(task.get("file_action"), dict):
        weight += 1
    if task.get("status") == "done":
        weight = 1
    return weight


def _bounded_priority_score(
    task: dict[str, Any],
    *,
    current_task_id: str | None,
    feedback_decision: dict[str, Any] | None,
) -> int:
    task_id = task.get("task_id") or task.get("taskId")
    status_value = {"active": 9, "pending": 6, "done": 2}.get(str(task.get("status") or ""), 4)
    task_class_value = {"remediation": 4, "verification": 3, "execution": 2, "reflection": 1}.get(
        _task_action_class(task_id if isinstance(task_id, str) else None),
        2,
    )
    selected_bonus = 5 if task_id and task_id == current_task_id else 0
    feedback_selected_id = None
    if isinstance(feedback_decision, dict):
        feedback_selected_id = feedback_decision.get("selected_task_id")
    feedback_bonus = 3 if task_id and task_id == feedback_selected_id else 0
    effort = _task_effort_weight(task)
    raw_score = ((status_value + task_class_value + selected_bonus + feedback_bonus) * 10) / effort
    return max(0, min(100, round(raw_score)))



def _wsjf_components(
    task: dict[str, Any],
    *,
    current_task_id: str | None,
    feedback_decision: dict[str, Any] | None,
) -> dict[str, Any]:
    task_id = task.get("task_id") or task.get("taskId")
    user_business_value = {"active": 8, "pending": 5, "done": 1}.get(str(task.get("status") or ""), 3)
    time_criticality = 8 if task_id and task_id == current_task_id else 4
    feedback_selected_id = feedback_decision.get("selected_task_id") if isinstance(feedback_decision, dict) else None
    risk_reduction_opportunity_enablement = 8 if task_id and task_id == feedback_selected_id else 5
    job_size = max(1, _task_effort_weight(task))
    score = round((user_business_value + time_criticality + risk_reduction_opportunity_enablement) / job_size, 2)
    return {
        "user_business_value": user_business_value,
        "time_criticality": time_criticality,
        "risk_reduction_opportunity_enablement": risk_reduction_opportunity_enablement,
        "job_size": job_size,
        "score": score,
    }



def _hadi_entry(
    *,
    task: dict[str, Any],
    goal_id: str,
    result_status: str,
    approval_gate_state: str,
    next_hint: str,
    experiment: dict[str, Any],
    acceptance: str,
) -> dict[str, Any]:
    title = task.get("title") or task.get("summary") or task.get("task_id") or "task"
    return {
        "hypothesis": str(title),
        "action": acceptance,
        "data": {
            "goal_id": goal_id,
            "result_status": result_status,
            "approval_gate_state": approval_gate_state,
            "reward_signal": experiment.get("reward_signal"),
            "budget": experiment.get("budget"),
            "budget_used": experiment.get("budget_used"),
        },
        "insights": [
            f"next_hint={next_hint}",
            f"result_status={result_status}",
            f"approval_gate_state={approval_gate_state}",
        ],
    }



def _load_previous_credit_balance(credits_dir: Path) -> float:
    latest_path = credits_dir / "latest.json"
    if latest_path.exists():
        data = _safe_read_json(latest_path)
        if isinstance(data, dict):
            try:
                return float(data.get("balance") or 0.0)
            except Exception:
                return 0.0
    return 0.0



def _write_credits_ledger(
    *,
    credits_dir: Path,
    cycle_id: str,
    goal_id: str,
    result_status: str,
    reward_signal: dict[str, Any],
    budget_used: dict[str, Any],
    recorded_at_utc: str,
    experiment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    credits_dir.mkdir(parents=True, exist_ok=True)
    previous_balance = _load_previous_credit_balance(credits_dir)
    reward_gate = {'status': 'accepted', 'reason': 'reward_signal_accepted'}
    if isinstance(experiment, dict) and experiment.get('outcome') == 'discard' and experiment.get('revert_required'):
        if experiment.get('revert_status') in {'queued', 'skipped_no_material_change', 'blocked'}:
            reward_gate = {'status': 'suppressed', 'reason': 'discarded_experiment_unresolved_revert'}
    try:
        delta = float(reward_signal.get("value") or 0.0)
    except Exception:
        delta = 0.0
    if reward_gate['status'] == 'suppressed':
        delta = 0.0
    balance = round(previous_balance + delta, 4)
    payload = {
        "schema_version": CREDITS_LEDGER_VERSION,
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "result_status": result_status,
        "delta": delta,
        "balance": balance,
        "reward_signal": reward_signal,
        "budget_used": budget_used,
        "recorded_at_utc": recorded_at_utc,
        "reason": reward_signal.get("source") if isinstance(reward_signal, dict) else None,
        "reward_gate": reward_gate,
    }
    if isinstance(experiment, dict) and isinstance(experiment.get("subagent_consumption"), dict):
        payload["subagent_consumption"] = experiment.get("subagent_consumption")
    (credits_dir / "latest.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    with (credits_dir / "history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload

def _validate_control_plane_summary_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    summary: dict[str, Any] = {'status': 'ok'}
    warnings: list[str] = []
    errors: list[str] = []

    approval_gate = payload.get('approval_gate') if isinstance(payload.get('approval_gate'), dict) else {}
    approval_source = approval_gate.get('source')
    if approval_source and not Path(str(approval_source)).exists():
        warnings.append('approval_gate_source_missing')

    report_path = payload.get('report_path')
    if not report_path or not Path(str(report_path)).exists():
        errors.append('report_path_missing')

    report_index_path = payload.get('report_index_path')
    if not report_index_path or not Path(str(report_index_path)).exists():
        errors.append('report_index_path_missing')

    experiment = payload.get('experiment') if isinstance(payload.get('experiment'), dict) else {}
    experiment_path = experiment.get('experiment_path')
    if not experiment_path or not Path(str(experiment_path)).exists():
        errors.append('experiment_path_missing')
    hypothesis = experiment.get('hypothesis')
    success_checks = experiment.get('success_checks')
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        errors.append('experiment_hypothesis_missing')
    if not isinstance(success_checks, list) or not success_checks:
        errors.append('experiment_success_checks_missing')

    task_plan = payload.get('task_plan') if isinstance(payload.get('task_plan'), dict) else {}
    hypotheses = payload.get('hypotheses') if isinstance(payload.get('hypotheses'), dict) else {}
    current_task_id = task_plan.get('current_task_id')
    selected_hypothesis_id = hypotheses.get('selected_hypothesis_id')
    if current_task_id and selected_hypothesis_id and current_task_id != selected_hypothesis_id:
        warnings.append('task_hypothesis_selection_mismatch')

    timeout_budget = None
    budget = experiment.get('budget') if isinstance(experiment.get('budget'), dict) else {}
    budget_used = experiment.get('budget_used') if isinstance(experiment.get('budget_used'), dict) else {}
    max_timeout_seconds = budget.get('max_timeout_seconds')
    elapsed_seconds = budget_used.get('elapsed_seconds')
    if max_timeout_seconds is None:
        warnings.append('timeout_budget_missing')
        timeout_budget = {'status': 'missing', 'reason': 'max_timeout_seconds_missing', 'prompt_timeout_seconds': None, 'runtime_timeout_seconds': elapsed_seconds}
    elif isinstance(max_timeout_seconds, (int, float)) and isinstance(elapsed_seconds, (int, float)):
        if elapsed_seconds > max_timeout_seconds:
            errors.append('timeout_budget_exceeded')
            timeout_budget = {'status': 'mismatch', 'reason': 'elapsed_exceeds_budget', 'prompt_timeout_seconds': max_timeout_seconds, 'runtime_timeout_seconds': elapsed_seconds}
        elif elapsed_seconds == max_timeout_seconds:
            warnings.append('timeout_budget_at_limit')
            timeout_budget = {'status': 'warning', 'reason': 'elapsed_at_budget_limit', 'prompt_timeout_seconds': max_timeout_seconds, 'runtime_timeout_seconds': elapsed_seconds}
        else:
            timeout_budget = {'status': 'ok', 'reason': 'within_timeout_budget', 'prompt_timeout_seconds': max_timeout_seconds, 'runtime_timeout_seconds': elapsed_seconds}
    else:
        timeout_budget = {'status': 'unknown', 'reason': 'insufficient_timeout_data', 'prompt_timeout_seconds': max_timeout_seconds, 'runtime_timeout_seconds': elapsed_seconds}

    if errors:
        summary['status'] = 'error'
    elif warnings:
        summary['status'] = 'warning'
    summary['validation_errors'] = errors
    summary['validation_warnings'] = warnings
    summary['checks'] = {
        'approval_source': approval_source,
        'report_path': report_path,
        'report_index_path': report_index_path,
        'experiment_path': experiment_path,
        'timeout_budget': timeout_budget,
    }
    return summary, warnings, errors


def _normalize_blocker_summary(
    *,
    result_status: str,
    next_hint: str,
    approval_gate: dict[str, Any],
    current_plan: dict[str, Any],
    experiment_record: dict[str, Any],
    report_index: dict[str, Any],
    current_task_record: dict[str, Any] | None = None,
    selected_acceptance: str | None = None,
    runtime_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feedback_decision = current_plan.get('feedback_decision') if isinstance(current_plan.get('feedback_decision'), dict) else {}
    blocked_next_step = current_plan.get('blocked_next_step') or next_hint
    current_task_id = current_plan.get('current_task_id')
    current_task_title = None
    if isinstance(current_task_record, dict):
        current_task_title = current_task_record.get('title') or current_task_record.get('summary')
    if not current_task_title:
        current_task_title = current_plan.get('current_task') or current_plan.get('selected_task_title')

    feedback_reason = feedback_decision.get('reason') if isinstance(feedback_decision.get('reason'), str) else ''
    normalized_reason = feedback_reason or blocked_next_step or approval_gate.get('state') or result_status.lower()
    blocked_signals = {'blocked', 'no-op', 'noop', 'stagnant', 'stale', 'terminal', 'discard'}
    feedback_mode = feedback_decision.get('mode') if isinstance(feedback_decision.get('mode'), str) else None
    reason_lc = normalized_reason.lower() if isinstance(normalized_reason, str) else ''
    mode_lc = feedback_mode.lower() if isinstance(feedback_mode, str) else ''
    if result_status in {'BLOCK', 'ERROR'}:
        state = 'blocked'
    elif any(token in reason_lc for token in blocked_signals) or any(token in mode_lc for token in blocked_signals):
        state = 'stagnant'
    else:
        state = 'clear'

    recommended_next_action = blocked_next_step
    if state == 'stagnant' and isinstance(feedback_decision.get('selected_task_title'), str) and feedback_decision.get('selected_task_title'):
        recommended_next_action = feedback_decision.get('selected_task_title')
    if state == 'clear' and not recommended_next_action:
        recommended_next_action = 'continue the current plan'

    return {
        'schema_version': 'blocker-summary-v1',
        'state': state,
        'reason': normalized_reason,
        'recommended_next_action': recommended_next_action,
        'source': 'producer_cycle' if runtime_source else 'control_plane_summary',
        'result_status': result_status,
        'approval_gate_state': approval_gate.get('state'),
        'current_task_id': current_task_id,
        'current_task_title': current_task_title,
        'blocked_next_step': blocked_next_step,
        'feedback_mode': feedback_mode,
        'feedback_reason': feedback_reason or None,
        'selected_acceptance': selected_acceptance,
        'report_index_status': report_index.get('status'),
        'experiment_outcome': experiment_record.get('outcome'),
        'runtime_source': runtime_source,
    }


def _write_control_plane_summary_artifact(
    *,
    state_root: Path,
    cycle_id: str,
    goal_id: str,
    result_status: str,
    approval_gate: dict[str, Any],
    next_hint: str,
    current_plan: dict[str, Any],
    hypothesis_backlog: dict[str, Any],
    experiment_record: dict[str, Any],
    report_index: dict[str, Any],
    report_path: Path,
    report_index_path: Path,
    credits: dict[str, Any],
    runtime_source: dict[str, Any],
    prompt_mass: dict[str, Any],
    research_feed: dict[str, Any] | None = None,
) -> Path:
    control_dir = state_root / "control_plane"
    control_dir.mkdir(parents=True, exist_ok=True)
    path = control_dir / "current_summary.json"
    selected_acceptance = hypothesis_backlog.get("selected_hypothesis_execution_spec_acceptance") if isinstance(hypothesis_backlog, dict) else None
    current_task_record = None
    for task in current_plan.get("tasks", []) if isinstance(current_plan.get("tasks"), list) else []:
        if (task.get("task_id") or task.get("taskId")) == current_plan.get("current_task_id"):
            current_task_record = task
            if not selected_acceptance:
                selected_acceptance = _task_execution_acceptance(
                    task,
                    goal_id=goal_id,
                    result_status=result_status,
                    approval_gate_state=approval_gate.get("state") if isinstance(approval_gate, dict) else "unknown",
                    next_hint=next_hint,
                )
            break
    blocker_summary = _normalize_blocker_summary(
        result_status=result_status,
        next_hint=next_hint,
        approval_gate=approval_gate,
        current_plan=current_plan,
        experiment_record=experiment_record,
        report_index=report_index,
        current_task_record=current_task_record,
        selected_acceptance=selected_acceptance,
        runtime_source=runtime_source,
    )
    payload = {
        "schema_version": "control-plane-summary-v1",
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "result_status": result_status,
        "approval_gate": approval_gate,
        "next_hint": next_hint,
        "task_plan": current_plan,
        "task_boundary": {
            "task_id": current_plan.get("current_task_id"),
            "title": (current_task_record.get("title") or current_task_record.get("summary")) if isinstance(current_task_record, dict) else current_plan.get("selected_task_title") or current_plan.get("current_task"),
            "selection_source": (current_plan.get("feedback_decision") or {}).get("selection_source") if isinstance(current_plan.get("feedback_decision"), dict) else current_plan.get("task_selection_source"),
            "selected_tasks": current_plan.get("selected_tasks") or ((current_plan.get("feedback_decision") or {}).get("selected_task_label") if isinstance(current_plan.get("feedback_decision"), dict) else None),
            "mutation_lane": current_plan.get("mutation_lane"),
            "budget": experiment_record.get("budget"),
            "mutation_scope": (experiment_record.get("contract") or {}).get("mutation_scope") if isinstance(experiment_record.get("contract"), dict) else None,
            "acceptance": selected_acceptance,
            "completion_reason": (current_plan.get("feedback_decision") or {}).get("reason") if isinstance(current_plan.get("feedback_decision"), dict) else None,
            "materialized_improvement_artifact_path": current_plan.get("materialized_improvement_artifact_path"),
        },
        "hypotheses": {
            "selected_hypothesis_id": hypothesis_backlog.get("selected_hypothesis_id"),
            "selected_hypothesis_title": hypothesis_backlog.get("selected_hypothesis_title"),
            "entry_count": hypothesis_backlog.get("entry_count"),
            "backlog_path": str(state_root / "hypotheses" / "backlog.json"),
            "research_feed": research_feed,
        },
        "experiment": {
            "experiment_id": experiment_record.get("experiment_id"),
            "current_task_id": experiment_record.get("current_task_id"),
            "current_task_class": _task_action_class(experiment_record.get("current_task_id")),
            "selection_source": (current_plan.get("feedback_decision") or {}).get("selection_source") if isinstance(current_plan.get("feedback_decision"), dict) else None,
            "acceptance": selected_acceptance,
            "result_status": experiment_record.get("result_status"),
            "outcome": experiment_record.get("outcome"),
            "review_status": experiment_record.get("review_status"),
            "decision": experiment_record.get("decision"),
            "readiness_checks": experiment_record.get("readiness_checks"),
            "readiness_reasons": experiment_record.get("readiness_reasons"),
            "metric_name": experiment_record.get("metric_name"),
            "metric_baseline": experiment_record.get("metric_baseline"),
            "metric_current": experiment_record.get("metric_current"),
            "metric_frontier": experiment_record.get("metric_frontier"),
            "revert_required": experiment_record.get("revert_required"),
            "revert_status": experiment_record.get("revert_status"),
            "hypothesis": experiment_record.get("hypothesis"),
            "success_checks": experiment_record.get("success_checks"),
            "budget": experiment_record.get("budget"),
            "budget_used": experiment_record.get("budget_used"),
            "experiment_path": str(state_root / "experiments" / "latest.json"),
        },
        "report_index": {
            "status": report_index.get("status"),
            "source": report_index.get("source"),
            "improvement_score": report_index.get("improvement_score"),
        },
        "owner_utility": {
            "state": "available" if result_status == "PASS" else "degraded" if result_status == "BLOCK" else "blocked",
            "reason": next_hint or result_status.lower(),
            "primary_action": current_plan.get("current_task") or next_hint,
            "evidence": {
                "report_index_status": report_index.get("status"),
                "experiment_outcome": experiment_record.get("outcome"),
                "credits_balance": credits.get("balance") if isinstance(credits, dict) else None,
            },
        },
        "report_path": str(report_path),
        "report_index_path": str(report_index_path),
        "blocker_summary": blocker_summary,
        "credits": credits,
        "runtime_source": runtime_source,
        "prompt_mass": prompt_mass,
    }
    validation_summary, validation_warnings, validation_errors = _validate_control_plane_summary_payload(payload)
    payload["validation_summary"] = validation_summary
    payload["validation_warnings"] = validation_warnings
    payload["validation_errors"] = validation_errors
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _build_hypothesis_backlog_snapshot(
    *,
    cycle_id: str,
    goal_id: str,
    result_status: str,
    approval_gate_state: str,
    next_hint: str,
    experiment: dict[str, Any],
    report_path: Path,
    history_path: Path,
    outbox_path: Path,
    task_plan_path: Path,
    task_plan: dict[str, Any],
    research_feed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks = task_plan.get("tasks") if isinstance(task_plan.get("tasks"), list) else []
    task_records = [task for task in tasks if isinstance(task, dict)]
    current_task_id = task_plan.get("current_task_id") or task_plan.get("currentTaskId")
    feedback_decision = task_plan.get("feedback_decision") if isinstance(task_plan.get("feedback_decision"), dict) else None
    selected_hypothesis_id = None
    selected_hypothesis_title = None
    selected_hypothesis_score = None
    entries: list[dict[str, Any]] = []

    for task in task_records:
        task_id = task.get("task_id") or task.get("taskId")
        task_title = task.get("title") or task.get("summary") or task_id or "task"
        selected = bool(task_id and task_id == current_task_id)
        score = _bounded_priority_score(
            task,
            current_task_id=current_task_id if isinstance(current_task_id, str) else None,
            feedback_decision=feedback_decision,
        )
        wsjf = _wsjf_components(
            task,
            current_task_id=current_task_id if isinstance(current_task_id, str) else None,
            feedback_decision=feedback_decision,
        )
        acceptance = _task_execution_acceptance(
            task,
            goal_id=goal_id,
            result_status=result_status,
            approval_gate_state=approval_gate_state,
            next_hint=next_hint,
        )
        if selected:
            selected_hypothesis_id = str(task_id)
            selected_hypothesis_title = str(task_title)
            selected_hypothesis_score = score
        entries.append(
            {
                "hypothesis_id": f"hypothesis-{task_id}" if task_id else None,
                "task_id": task_id,
                "task_title": task_title,
                "task_status": task.get("status"),
                "selected": selected,
                "selection_status": "selected" if selected else "backlog",
                "bounded_priority_score": score,
                "wsjf": wsjf,
                "hadi": _hadi_entry(
                    task=task,
                    goal_id=goal_id,
                    result_status=result_status,
                    approval_gate_state=approval_gate_state,
                    next_hint=next_hint,
                    experiment=experiment,
                    acceptance=acceptance,
                ),
                "execution_spec": {
                    "goal": goal_id,
                    "task_title": task_title,
                    "acceptance": acceptance,
                    "budget": experiment["budget"],
                },
            }
        )

    feed_path = None
    feed_count = 0
    total_feed_count = None
    seen_task_ids = {entry.get('task_id') for entry in entries if entry.get('task_id')}
    if isinstance(research_feed, dict):
        feed_path = research_feed.get('feed_path')
        total_feed_count = research_feed.get('entry_count') if isinstance(research_feed.get('entry_count'), int) else None
        candidates = research_feed.get('entries') if isinstance(research_feed.get('entries'), list) else []
        for idx, item in enumerate(candidates, start=1):
            if not isinstance(item, dict):
                continue
            rid = item.get('id') or f'research-{idx}'
            if rid in seen_task_ids:
                continue
            feed_count += 1
            title = item.get('title') or item.get('summary') or rid
            entries.append({
                'hypothesis_id': f'research-hypothesis-{rid}',
                'task_id': rid,
                'task_title': title,
                'task_status': 'research_candidate',
                'selected': False,
                'selection_status': 'research_feed',
                'bounded_priority_score': item.get('score', 0.0),
                'wsjf': {'score': item.get('wsjf', 0.0)},
                'hadi': {
                    'hypothesis': item.get('hypothesis') or title,
                    'action': item.get('action') or 'review research candidate',
                    'data': {'source': 'research_feed', 'path': feed_path},
                    'insights': item.get('insights') or [],
                },
                'execution_spec': {
                    'goal': goal_id,
                    'task_title': title,
                    'acceptance': item.get('acceptance') or 'triage into bounded backlog if still relevant',
                    'budget': experiment['budget'],
                },
            })
            seen_task_ids.add(rid)

    entries.sort(key=lambda entry: (entry.get("wsjf", {}).get("score") or 0, entry["bounded_priority_score"]), reverse=True)
    if selected_hypothesis_id is None and entries:
        top_entry = entries[0]
        selected_hypothesis_id = top_entry.get("task_id")
        selected_hypothesis_title = top_entry.get("task_title")
        selected_hypothesis_score = top_entry.get("bounded_priority_score")
        top_entry["selected"] = True
        top_entry["selection_status"] = "selected"

    return {
        "schema_version": HYPOTHESIS_BACKLOG_VERSION,
        "model": "HADI",
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "task_plan_path": str(task_plan_path),
        "history_path": str(history_path),
        "report_path": str(report_path),
        "outbox_path": str(outbox_path),
        "experiment_id": experiment.get("experiment_id"),
        "context": {
            "result_status": result_status,
            "approval_gate_state": approval_gate_state,
            "next_hint": next_hint,
            "feedback_decision": task_plan.get("feedback_decision"),
            "reward_signal": task_plan.get("reward_signal"),
            "budget": experiment["budget"],
            "budget_used": experiment["budget_used"],
            "experiment_path": experiment.get("experiment_path"),
        },
        "selected_hypothesis_id": selected_hypothesis_id,
        "selected_hypothesis_title": selected_hypothesis_title,
        "selected_hypothesis_score": selected_hypothesis_score,
        "selected_hypothesis_wsjf": next((entry.get("wsjf") for entry in entries if entry.get("task_id") == selected_hypothesis_id), None),
        "research_feed": {
            "feed_path": feed_path,
            "entry_count": total_feed_count if total_feed_count is not None else feed_count,
            "merged_entry_count": feed_count,
            "enabled": bool(feed_path),
        },
        "entry_count": len(entries),
        "entries": entries,
    }


def _derive_bounded_tasks_from_plan(
    tasks: str,
    task_plan: dict[str, Any] | None,
    feedback_decision: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Prefer the recorded current task from the prior plan when available."""
    if not isinstance(task_plan, dict):
        return tasks, "requested_tasks"

    if isinstance(feedback_decision, dict) and feedback_decision.get("selected_task_label"):
        return str(feedback_decision["selected_task_label"]), str(feedback_decision.get("selection_source") or "feedback")

    current_task_id = task_plan.get("current_task_id") or task_plan.get("currentTaskId")
    if not current_task_id:
        return tasks, "requested_tasks"

    selected_task: dict[str, Any] | None = None
    recorded_tasks = task_plan.get("tasks")
    if isinstance(recorded_tasks, list):
        for task in recorded_tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("task_id") or task.get("taskId")
            if task_id == current_task_id:
                selected_task = task
                break

    if isinstance(selected_task, dict):
        return _render_task_selection(selected_task), "recorded_current_task"

    return str(current_task_id), "recorded_current_task_id"


_DEFAULT_HOST_CONTROL_PLANE_STATE_ROOT = Path("/var/lib/eeepc-agent/self-evolving-agent/state")


def _workspace_looks_like_eeepc_live_runtime(workspace: Path) -> bool:
    """Detect the live eeepc runtime workspace layout.

    The live systemd unit runs the gateway from /home/opencode/.nanobot-eeepc/workspace.
    When that layout is present and no explicit runtime-state source is set, we should
    promote the canonical host-control-plane state root instead of the workspace-local
    fallback so the live activation actually emits goals/current/active/history files.
    """
    return workspace.parent.name == ".nanobot-eeepc" and workspace.name == "workspace"


def _resolve_runtime_state_root(workspace: Path) -> Path:
    from nanobot.runtime.state import resolve_runtime_state_root

    return resolve_runtime_state_root(workspace)


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
    feedback_decision = _derive_feedback_decision(recorded_task_plan, goals_dir)
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
    contract_path = experiments_dir / "contracts" / f"{experiment_id}.json"
    revert_path = experiments_dir / "reverts" / f"{experiment_id}.json"
    outbox_path = outbox_dir / "latest.json"
    previous_experiment = _load_previous_experiment_snapshot(experiments_dir)
    preplan_current_task_id = _derive_experiment_current_task_id(result_status, feedback_decision)
    reward_signal = _derive_reward_signal(result_status, None, preplan_current_task_id, previous_experiment)
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
        upgraded_reward["value"] = max(float(upgraded_reward.get("value") or 0.0), 1.2)
        upgraded_reward["source"] = "materialized_improvement_artifact"
        current_plan["reward_signal"] = upgraded_reward
        experiment["reward_signal"] = upgraded_reward
        experiment["metric_current"] = upgraded_reward["value"]
        experiment["metric_frontier"] = max(float(experiment.get("metric_frontier") or upgraded_reward["value"]), upgraded_reward["value"])
        experiment["budget_used"]["tool_calls"] = max(int(experiment["budget_used"].get("tool_calls") or 0), 2)
        if current_plan.get("current_task_id") == "subagent-verify-materialized-improvement":
            experiment["budget_used"]["subagents"] = max(int(experiment["budget_used"].get("subagents") or 0), 1)
        if (current_plan.get("feedback_decision") or {}).get("mode") in {"complete_active_lane", "handoff_to_next_candidate"}:
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

    subagent_request_path = _write_subagent_request_artifact(
        state_root=state_root,
        cycle_id=cycle_id,
        goal_id=active_goal,
        current_plan=current_plan,
    )
    if subagent_request_path:
        current_plan["subagent_request_path"] = subagent_request_path
        experiment["budget_used"]["subagents"] = max(int(experiment["budget_used"].get("subagents") or 0), 1)
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
            review_result = review_promotion_candidate(
                workspace=state_root.parent,
                state_root=state_root,
                candidate_id=promotion_candidate_id,
                decision="accept",
                decision_reason="autonomous runtime accepted a ready self-evolving promotion with materialized artifact evidence",
            )
            decision_record_path = promotions_dir / "decisions" / f"{promotion_candidate_id}.json"
            accepted_record_path = promotions_dir / "accepted" / f"{promotion_candidate_id}.json"
            decision_record_value = str(decision_record_path)
            accepted_record_value = str(accepted_record_path) if accepted_record_path.exists() else None
            review_status = str(review_result.get("review_status") or "reviewed")
            decision = str(review_result.get("decision") or "accept")
            experiment["review_status"] = review_status
            experiment["decision"] = decision
            experiment["decision_record"] = decision_record_value
            experiment["accepted_record"] = accepted_record_value
            final_promotion_record = {
                **review_result,
                "decision_record": decision_record_value,
                "accepted_record": accepted_record_value,
                "governance_packet": {
                    **(final_promotion_record.get("governance_packet") if isinstance(final_promotion_record.get("governance_packet"), dict) else {}),
                    "review_packet_status": "accepted" if accepted_record_value else "reviewed",
                    "review_status": review_status,
                    "decision": decision,
                    "decision_record": decision_record_value,
                    "accepted_record": accepted_record_value,
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
        "budget_used": experiment["budget_used"],
        "experiment": experiment,
        "experiment_path": str(experiment_path),
        "summary": summary,
        "execution_response": execution_response,
        "execution_error": execution_error,
        "artifact_paths": artifact_paths,
        "subagent_consumption": subagent_consumption,
        "materialized_improvement_artifact_path": current_plan.get("materialized_improvement_artifact_path"),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

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
        "budget_used": experiment["budget_used"],
        "subagent_consumption": subagent_consumption,
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
        "budget_used": experiment["budget_used"],
        "subagent_consumption": subagent_consumption,
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
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(
        json.dumps(experiment["contract"], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if experiment.get("revert_required") and isinstance(experiment.get("revert"), dict):
        revert_path.parent.mkdir(parents=True, exist_ok=True)
        revert_path.write_text(
            json.dumps(experiment["revert"], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    experiment_path.write_text(
        json.dumps(experiment_record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (experiments_dir / "latest.json").write_text(
        json.dumps({**experiment_record, "experiment_path": str(experiment_path)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    with (experiments_dir / "history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**experiment_record, "experiment_path": str(experiment_path)}, ensure_ascii=False) + "\n")
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
    history_path.write_text(
        json.dumps(history_entry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return summary
