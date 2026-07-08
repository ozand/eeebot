"""Cycle Persist phase: reports/state artifact writes for the self-evolving cycle.

Extracted from coordinator.py (issue #600). Holds credits-ledger, control-
plane-summary, and hypothesis-backlog-snapshot writers plus the
stalled-lane/revert-detection helpers run_self_evolving_cycle's Persist
phase relies on. Depends on nanobot.runtime.cycle_observe for shared task
predicates and constants. No behavior change from the move.
"""

from __future__ import annotations

import heapq
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime._io import read_json_safe as _safe_read_json
from nanobot.runtime.cycle_observe import (
    _BACKLOG_PROGRESSION_IDS,
    CREDITS_LEDGER_VERSION,
    HYPOTHESIS_BACKLOG_VERSION,
    _git_output,
    _render_task_selection,
    _task_action_class,
    _task_is_selectable,
)
from nanobot.runtime.stop_guards import pick_alternative_task, should_switch_lane


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
                    "budget_policy": experiment.get("budget_policy"),
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

    # Use heapq.nlargest for O(n log k) instead of sorted() O(n log n)
    # when only the top entry is needed for selection.
    top_entries = heapq.nlargest(
        1,
        entries,
        key=lambda entry: (entry.get("wsjf", {}).get("score") or 0, entry["bounded_priority_score"]),
    )
    if selected_hypothesis_id is None and top_entries:
        top_entry = top_entries[0]
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
            "budget_policy": experiment.get("budget_policy"),
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


def _switch_off_stalled_lane(
    feedback_decision: dict[str, Any] | None,
    task_plan: dict[str, Any] | None,
    previous_experiment: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """R11 enforcement: move off a lane the previous cycle stalled out on.

    When the previous snapshot tripped the no-progress stop, re-point the
    feedback decision at a different available task so the next cycle does not
    re-run the stalled lane. If there is no distinct alternative, the decision is
    left unchanged (the stall is still recorded in durable state).

    The stalled lane is the *previous* experiment's lane
    (``previous_experiment["current_task_id"]``), not whatever the incoming
    feedback decision selected. If the decision already selects a different,
    truthy task (e.g. a forward move synthesized by the state machine that is
    not present in the persisted task list), it has already escaped the
    stalled lane — return it unchanged instead of re-trapping the coordinator
    on stale bookkeeping (#586).
    """
    if not should_switch_lane(previous_experiment):
        return feedback_decision
    if not isinstance(task_plan, dict):
        return feedback_decision
    # Issue #697: R11 is scoped down to CORE bookkeeping lanes only
    # (approval-gate/force-remediation, decide_next_lane steps 1-2). A
    # decision produced by the live generation_phase driver (steps 3-8:
    # the idle backstop or any synthesize->materialize->verify progression
    # step, tagged lane_category="generation") is never a stale same-task
    # confirmation R11 needs to protect against — it is recomputed from live
    # subagent state every cycle — so it must never be overridden here. This
    # closes the #695/#697 failure class where R11 erased a generation
    # restart before it could land.
    if isinstance(feedback_decision, dict) and feedback_decision.get("lane_category") == "generation":
        return feedback_decision
    stalled_lane_id = None
    if isinstance(previous_experiment, dict):
        stalled_lane_id = previous_experiment.get("current_task_id") or previous_experiment.get("currentTaskId")
    if not stalled_lane_id:
        stalled_lane_id = task_plan.get("current_task_id") or task_plan.get("currentTaskId")
    if isinstance(feedback_decision, dict):
        decision_selected_id = feedback_decision.get("selected_task_id")
        if decision_selected_id and decision_selected_id != stalled_lane_id:
            # Decision already moves off the stalled lane — leave it alone.
            return feedback_decision
    current_id = stalled_lane_id
    tasks = task_plan.get("tasks") if isinstance(task_plan.get("tasks"), list) else []
    # Issue #580 follow-up: pick_alternative_task has no status/orphan awareness —
    # it returns the first task with a different id, even if that task is already
    # done or is an orphaned task_id left behind by removed code. Filter to
    # selectable tasks first so the stall-switch can never land on a dead lane.
    selectable_tasks = [t for t in tasks if isinstance(t, dict) and _task_is_selectable(t)]
    # Issue #568: prefer backlog-progression tasks over pure bookkeeping when switching
    # off a stalled lane, so a stall is more likely to advance real dispatch than bounce
    # back to bookkeeping. pick_alternative_task's contract is unchanged — only order.
    sorted_tasks = sorted(
        selectable_tasks,
        key=lambda t: 0
        if isinstance(t, dict) and (t.get("task_id") or t.get("taskId")) in _BACKLOG_PROGRESSION_IDS
        else 1,
    )
    alt = pick_alternative_task(current_id if isinstance(current_id, str) else None, sorted_tasks)
    if alt is None:
        return feedback_decision
    alt_id = alt.get("task_id") or alt.get("taskId")
    decision = dict(feedback_decision) if isinstance(feedback_decision, dict) else {}
    decision["selected_task_id"] = alt_id
    decision["selected_task_label"] = _render_task_selection(alt)
    decision["selection_source"] = "switch_stalled_lane"
    decision["mode"] = "switch_stalled_lane"
    decision["reason"] = f"previous lane stalled (no_progress); switched to {alt_id}"
    return decision


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


def _workspace_looks_like_eeepc_live_runtime(workspace: Path) -> bool:
    """Detect the live eeepc runtime workspace layout.

    The live systemd unit runs the gateway from /home/opencode/.nanobot-eeepc/workspace.
    When that layout is present and no explicit runtime-state source is set, we should
    promote the canonical host-control-plane state root instead of the workspace-local
    fallback so the live activation actually emits goals/current/active/history files.
    """
    return workspace.parent.name == ".nanobot-eeepc" and workspace.name == "workspace"


def _commit_at_or_after_cycle_start(commit_iso: str | None, cycle_started_utc: str | None) -> bool:
    """Return True unless we can positively prove the commit predates the cycle.

    Best-effort, cheap timestamp comparison: if either timestamp is missing or
    fails to parse, we don't reject the commit on this basis alone (the
    fail-closed git-probe-error guard elsewhere already covers the "we cannot
    verify anything" case).
    """
    if not commit_iso or not cycle_started_utc:
        return True
    try:
        commit_dt = datetime.fromisoformat(commit_iso.strip().replace("Z", "+00:00"))
        cycle_dt = datetime.fromisoformat(cycle_started_utc.strip().replace("Z", "+00:00"))
    except ValueError:
        return True
    if commit_dt.tzinfo is None:
        commit_dt = commit_dt.replace(tzinfo=timezone.utc)
    if cycle_dt.tzinfo is None:
        cycle_dt = cycle_dt.replace(tzinfo=timezone.utc)
    return commit_dt >= cycle_dt


def _has_concrete_changes(
    workspace: Path,
    state_root: Path | None = None,
    cycle_started_utc: str | None = None,
) -> bool:
    """Return True only if a real, verified source-code change exists in
    workspace or eeebot-self-evolving.

    In the two-repository topology, subagents commit to eeebot-self-evolving
    (``state_root.parent / "eeebot-self-evolving"``), not to the canonical workspace.
    When ``state_root`` is provided we check that repo for recent commits (last 15 minutes,
    or since ``cycle_started_utc`` when provided) in addition to the canonical workspace
    worktree.

    Fails CLOSED: any git-probe error, or inability to determine git state, is treated
    as "no concrete change" (return False) — never as "change present". This is
    deliberate: reward and promotion-candidate creation depend on this function, and
    an ambiguous/erroring probe must not be able to fake evidence of real work
    (issue #565 — reward-hacking guard).
    """
    # ── Part A: check eeebot-self-evolving for recent subagent commits ──────────
    if state_root is not None:
        selfevo_path = state_root.parent / "eeebot-self-evolving"
        if selfevo_path.is_dir():
            selfevo_git = _git_output(
                ['git', 'rev-parse', '--is-inside-work-tree'], selfevo_path
            )
            if selfevo_git and selfevo_git.strip().lower() == "true":
                # A commit since cycle start (or, absent that, in the last 15
                # minutes) counts as a concrete change.
                since_arg = cycle_started_utc or "15 minutes ago"
                recent = _git_output(
                    ['git', 'log', f'--since={since_arg}', '--oneline'], selfevo_path
                )
                if recent and recent.strip():
                    return True

    # ── Part B: check canonical workspace (original logic) ───────────────────────
    is_git = _git_output(['git', 'rev-parse', '--is-inside-work-tree'], workspace)
    if not is_git or is_git.strip().lower() != "true":
        # Fail closed: either the git probe errored, or this workspace is not a
        # git worktree — either way we cannot verify a real change, so treat it
        # as absent rather than assuming the best case.
        return False

    # 1. Check unstaged/staged changes in the worktree
    status_output = _git_output(['git', 'status', '--porcelain', '-u'], workspace)
    changed_files = []
    if status_output:
        for line in status_output.splitlines():
            if len(line) > 3:
                # git status --porcelain format: "XY path"
                changed_files.append(line[3:].strip())

    # 2. Check the latest commit if it was created by autoevolve in this cycle
    commit_msg = _git_output(['git', 'log', '-1', '--pretty=%B'], workspace)
    if commit_msg and "autoevolve" in commit_msg.lower():
        commit_time = _git_output(['git', 'log', '-1', '--pretty=%cI'], workspace)
        if _commit_at_or_after_cycle_start(commit_time, cycle_started_utc):
            commit_files = _git_output(['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'], workspace)
            if commit_files:
                changed_files.extend(commit_files.splitlines())

    # Filter changes: ignore state, subagents, logs, config, history, templates, and doc files
    ignored_patterns = [
        "state/",
        ".nanobot/",
        "memory/",
        ".pi/",
        "docs/",
        "tests/",
        "README.md",
        "AGENTS.md",
        "lessons/"
    ]

    real_changes = []
    for f in changed_files:
        f = f.strip()
        if not f:
            continue
        # Skip ignored paths
        if any(f.startswith(pat) or f"/{pat}" in f for pat in ignored_patterns):
            continue
        # Check source extensions
        ext = Path(f).suffix.lower()
        if ext in {".py", ".sh", ".yaml", ".yml", ".toml", ".ts", ".js", ".json"}:
            real_changes.append(f)

    return len(real_changes) > 0
