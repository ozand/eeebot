#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.stale_execution_watchdog import DEFAULT_THRESHOLD_MINUTES, detect_stale_execution

ACTIVE_PROJECTS = ROOT / 'control' / 'active_projects.json'
ACTIVE_EXECUTION = ROOT / 'control' / 'active_execution.json'
QUEUE = ROOT / 'control' / 'execution_queue.json'

LIVE_STATUSES = {'in_progress'}
QUEUED_STATUSES = {'queued'}
WAITING_STATUSES = {
    'requested_execution',
    'dispatched',
    'handed_off',
    'pi_dev_requested',
    'bundled',
    'pi_dev_bundled',
    'pi_dev_dispatch_ready',
}
TERMINAL_STATUSES = {'completed', 'cancelled'}
STALE_BLOCKED_STATUSES = {'stale_blocked'}
NEEDS_REDISPATCH_STATUSES = {'needs_redispatch'}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f'.{path.name}.tmp')
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp_path.replace(path)


def task_key(task: dict[str, Any]) -> str:
    for key in ('dedupe_key', 'active_goal', 'report_source', 'diagnosis'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return 'task'


def classify_task(task: dict[str, Any], index: int) -> dict[str, Any]:
    queue_status = task.get('status')
    if queue_status in TERMINAL_STATUSES:
        execution_state = 'completed'
    elif queue_status in STALE_BLOCKED_STATUSES or queue_status in NEEDS_REDISPATCH_STATUSES:
        execution_state = 'needs_redispatch'
    elif queue_status in LIVE_STATUSES:
        execution_state = 'in_progress'
    elif queue_status in QUEUED_STATUSES:
        execution_state = 'queued'
    elif queue_status in WAITING_STATUSES:
        execution_state = 'waiting_for_dispatch'
    elif task.get('blocked_next_step'):
        execution_state = 'blocked'
    else:
        execution_state = 'waiting_for_dispatch'

    blocked = (
        execution_state in {'blocked', 'needs_redispatch'}
        or (bool(task.get('blocked_next_step')) and execution_state not in {'completed', 'in_progress'})
    )
    snapshot = {
        'task_index': index,
        'task_key': task_key(task),
        'queue_status': queue_status,
        'execution_state': execution_state,
        'is_live_execution': execution_state == 'in_progress',
        'is_blocked': blocked,
        'is_terminal': execution_state == 'completed',
        'source': task.get('source'),
        'diagnosis': task.get('diagnosis'),
        'severity': task.get('severity'),
        'active_goal': task.get('active_goal'),
        'report_source': task.get('report_source'),
        'failure_class': task.get('failure_class'),
        'remediation_class': task.get('remediation_class'),
        'recommended_remediation_action': task.get('recommended_remediation_action'),
        'blocked_next_step': task.get('blocked_next_step'),
        'requested_executor': task.get('requested_executor'),
        'execution_request_path': task.get('execution_request_path'),
        'executor_handoff_path': task.get('executor_handoff_path'),
        'pi_dev_request_path': task.get('pi_dev_request_path'),
        'pi_dev_bundle_path': task.get('pi_dev_bundle_path'),
        'pi_dev_dispatch_path': task.get('pi_dev_dispatch_path'),
        'delegated_executor_request_path': task.get('delegated_executor_request_path'),
        'delegated_executor_started_at': task.get('delegated_executor_started_at'),
        'delegated_executor_requested_at': task.get('delegated_executor_requested_at'),
        'delegated_executor_request_status': task.get('delegated_executor_request_status'),
        'dispatched_at': task.get('dispatched_at'),
        'execution_requested_at': task.get('execution_requested_at'),
        'executor_handoff_at': task.get('executor_handoff_at'),
        'pi_dev_requested_at': task.get('pi_dev_requested_at'),
        'pi_dev_bundled_at': task.get('pi_dev_bundled_at'),
        'pi_dev_dispatch_created_at': task.get('pi_dev_dispatch_created_at'),
        'stale_execution_detected': bool(task.get('stale_execution_detected')),
        'stale_execution_detected_at': task.get('stale_execution_detected_at'),
        'stale_execution_incident_path': task.get('stale_execution_incident_path'),
        'stale_execution_next_action_path': task.get('stale_execution_next_action_path'),
        'stale_execution_redispatch_artifact_path': task.get('stale_execution_redispatch_artifact_path'),
        'stale_execution_redispatch_created_at': task.get('stale_execution_redispatch_created_at'),
        'stale_execution_redispatch_created_by': task.get('stale_execution_redispatch_created_by'),
        'stale_execution_redispatch_source_next_action_path': task.get('stale_execution_redispatch_source_next_action_path'),
        'stale_execution_redispatch_source_incident_path': task.get('stale_execution_redispatch_source_incident_path'),
        'stale_execution_age_seconds': task.get('stale_execution_age_seconds'),
        'stale_execution_age': task.get('stale_execution_age'),
        'stale_execution_threshold_minutes': task.get('stale_execution_threshold_minutes'),
        'stale_execution_policy_summary': task.get('stale_execution_policy_summary'),
        'stale_execution_recommended_next_action': task.get('stale_execution_recommended_next_action'),
        'stale_execution_next_action_summary': task.get('stale_execution_next_action_summary'),
        'stale_execution_previous_status': task.get('stale_execution_previous_status'),
        'stale_execution_previous_execution_state': task.get('stale_execution_previous_execution_state'),
        'stale_execution_previous_started_at': task.get('stale_execution_previous_started_at'),
        'execution_completion_path': task.get('execution_completion_path'),
        'execution_completion_status': task.get('execution_completion_status'),
        'execution_completed_at': task.get('execution_completed_at'),
        'execution_completed_by': task.get('execution_completed_by'),
        'execution_completion_commit': task.get('execution_completion_commit'),
        'execution_completion_verification_method': task.get('execution_completion_verification_method'),
        'execution_completion_verification_status': task.get('execution_completion_verification_status'),
        'execution_completion_summary': task.get('execution_completion_summary'),
        'dispatch_state': task.get('dispatch_state'),
    }
    return snapshot


def normalize_historical_stale_metadata(snapshot: dict[str, Any], threshold_minutes: int, policy_summary: str) -> dict[str, Any]:
    if snapshot.get('stale_execution_detected'):
        snapshot['stale_execution_threshold_minutes'] = threshold_minutes
        snapshot['stale_execution_policy_summary'] = policy_summary
    return snapshot


def build_active_execution(queue: dict[str, Any], updated_at: str) -> dict[str, Any]:
    tasks = queue.get('tasks') if isinstance(queue, dict) else []
    if not isinstance(tasks, list):
        tasks = []

    active_tasks: list[dict[str, Any]] = []
    terminal_tasks: list[dict[str, Any]] = []
    live_task: dict[str, Any] | None = None

    current_threshold_minutes = DEFAULT_THRESHOLD_MINUTES
    current_policy_summary = f'in_progress tasks older than {current_threshold_minutes} minutes must be investigated or escalated.'

    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        snapshot = classify_task(task, index)
        if snapshot['is_terminal']:
            terminal_tasks.append(snapshot)
            continue
        active_tasks.append(snapshot)
        if live_task is None and snapshot['is_live_execution']:
            live_task = snapshot

    stale_watch = detect_stale_execution(active_execution={'active_tasks': active_tasks}, queue=queue, threshold_minutes=current_threshold_minutes, now=updated_at)
    stale_incident_tasks = [
        task
        for task in active_tasks
        if task.get('stale_execution_detected')
        or task.get('stale_execution_incident_path')
        or task.get('status') == 'stale_blocked'
        or task.get('execution_state') == 'needs_redispatch'
    ]
    stale_incident_task = stale_incident_tasks[0] if stale_incident_tasks else None
    stale_detected = bool(stale_watch['stale_detected'] or stale_incident_task is not None)

    summary = {
        'total': len(tasks),
        'active': len(active_tasks),
        'queued': sum(1 for task in active_tasks if task['execution_state'] == 'queued'),
        'in_progress': sum(1 for task in active_tasks if task['execution_state'] == 'in_progress'),
        'waiting_for_dispatch': sum(1 for task in active_tasks if task['execution_state'] == 'waiting_for_dispatch'),
        'needs_redispatch': sum(1 for task in active_tasks if task['execution_state'] == 'needs_redispatch'),
        'blocked': sum(1 for task in active_tasks if task['is_blocked']),
        'completed': len(terminal_tasks),
        'live_execution_tasks': sum(1 for task in active_tasks if task['is_live_execution']),
        'stale_execution_detected': stale_detected,
        'stale_execution_incidents': len(stale_incident_tasks),
    }

    normalized_active_tasks = [normalize_historical_stale_metadata(task, current_threshold_minutes, current_policy_summary) for task in active_tasks]
    normalized_terminal_tasks = [normalize_historical_stale_metadata(task, current_threshold_minutes, current_policy_summary) for task in terminal_tasks]
    normalized_live_task = normalize_historical_stale_metadata(live_task, current_threshold_minutes, current_policy_summary) if live_task is not None else None
    normalized_stale_watch = dict(stale_watch)
    if normalized_stale_watch.get('stale_detected'):
        normalized_stale_watch['threshold_minutes'] = current_threshold_minutes
        normalized_stale_watch['policy_summary'] = current_policy_summary

    registry = {
        'updated_at': updated_at,
        'source_queue_path': str(QUEUE),
        'summary': summary,
        'has_actually_executing_task': live_task is not None,
        'live_task': normalized_live_task,
        'stale_execution_detected': stale_detected,
        'stale_execution_threshold_minutes': current_threshold_minutes,
        'stale_execution_policy_summary': current_policy_summary,
        'stale_execution_task': normalized_stale_watch if normalized_stale_watch['stale_detected'] else None,
        'stale_execution_incident_task': normalize_historical_stale_metadata(stale_incident_task, current_threshold_minutes, current_policy_summary) if stale_incident_task is not None else None,
        'active_tasks': normalized_active_tasks,
        'terminal_tasks': normalized_terminal_tasks,
    }
    atomic_write_json(ACTIVE_EXECUTION, registry)
    return registry


def main() -> None:
    updated_at = now_utc()
    active_projects = load(ACTIVE_PROJECTS, {'projects': []})
    queue = load(QUEUE, {'tasks': []})
    active_execution = build_active_execution(queue, updated_at)

    project_items = active_projects.get('projects', []) if isinstance(active_projects, dict) else []
    if not isinstance(project_items, list):
        project_items = []

    print(
        json.dumps(
            {
                'updated_at': updated_at,
                'active_projects': project_items,
                'active_execution_tasks': active_execution['active_tasks'],
                'active_execution_summary': active_execution['summary'],
                'active_execution': active_execution,
                'truthful_execution_status': {
                    'has_live_delegated_execution': active_execution['has_actually_executing_task'],
                    'live_delegated_execution_task': active_execution['live_task'],
                    'stale_execution_detected': active_execution['stale_execution_detected'],
                    'stale_execution_task': active_execution['stale_execution_task'],
                    'stale_execution_incident_task': active_execution['stale_execution_incident_task'],
                    'stale_execution_policy_summary': active_execution['stale_execution_policy_summary'],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
