#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from scripts.build_status_snapshot import build_active_execution
from scripts.stale_execution_watchdog import DEFAULT_THRESHOLD_MINUTES, detect_stale_execution

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
ACTIVE_EXECUTION_PATH = ROOT / 'control' / 'active_execution.json'
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
INCIDENT_DIR = ROOT / 'control' / 'stale_execution_incidents'
NEXT_ACTION_DIR = ROOT / 'control' / 'stale_execution_next_actions'
LATEST_INCIDENT_PATH = ROOT / 'control' / 'stale_execution_incident.json'
LATEST_NEXT_ACTION_PATH = ROOT / 'control' / 'stale_execution_next_action.json'
SCRIPT_NAME = 'consume_stale_execution_incidents.py'
STALE_BLOCKED_STATUS = 'stale_blocked'
NEEDS_REDISPATCH_STATE = 'needs_redispatch'


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_json(path: Path, default: Any) -> Any:
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


def slugify(value: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', value.strip())
    slug = re.sub(r'-{2,}', '-', slug).strip('-._')
    return slug or 'task'


def task_key(task: dict[str, Any]) -> str:
    for key in ('task_key', 'dedupe_key', 'active_goal', 'report_source', 'diagnosis', 'failure_class', 'remediation_class'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return 'task'


def artifact_task_key(task: dict[str, Any]) -> str:
    return slugify(task_key(task))


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_slug(timestamp: str) -> str:
    return timestamp.replace('-', '').replace(':', '').replace('.', '')


def started_at_value(task: dict[str, Any]) -> str | None:
    for key in ('delegated_executor_started_at', 'execution_requested_at', 'executor_handoff_at', 'dispatched_at', 'created_at', 'started_at'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def started_at_slug(task: dict[str, Any]) -> str:
    started_at = started_at_value(task)
    if started_at is None:
        return 'unknown-start'
    parsed = parse_timestamp(started_at)
    if parsed is None:
        return slugify(started_at)
    return timestamp_slug(parsed.isoformat().replace('+00:00', 'Z'))


def incident_artifact_path(incident_dir: Path, task: dict[str, Any]) -> Path:
    return incident_dir / f'{started_at_slug(task)}-{artifact_task_key(task)}.json'


def next_action_artifact_path(next_action_dir: Path, task: dict[str, Any]) -> Path:
    return next_action_dir / f'{started_at_slug(task)}-{artifact_task_key(task)}.json'


def queue_tasks(queue: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(queue, dict):
        return []
    maybe_tasks = queue.get('tasks')
    if not isinstance(maybe_tasks, list):
        return []
    return [task for task in maybe_tasks if isinstance(task, dict)]


def matching_queue_index(tasks: list[dict[str, Any]], stale_result: dict[str, Any]) -> int | None:
    target_key = stale_result.get('task_key')
    target_started_at = stale_result.get('started_at')
    target_executor = stale_result.get('executor')

    for index, task in enumerate(tasks):
        if task_key(task) != target_key:
            continue
        if target_started_at is not None:
            task_started = started_at_value(task)
            if task_started != target_started_at:
                continue
        if target_executor is not None:
            executor = task.get('requested_executor') or task.get('delegated_executor_request_executor') or task.get('executor')
            if isinstance(executor, str) and executor.strip() and executor.strip() != target_executor:
                continue
        return index

    for index, task in enumerate(tasks):
        if task_key(task) == target_key and task.get('status') in {'in_progress', STALE_BLOCKED_STATUS, NEEDS_REDISPATCH_STATE}:
            return index
    return None


def build_next_action_summary(stale_result: dict[str, Any], task: dict[str, Any]) -> str:
    goal = task.get('active_goal') or 'the active goal'
    diagnosis = task.get('diagnosis') or 'the current blocker'
    failure_class = task.get('failure_class') or 'the current failure class'
    remediation_class = task.get('remediation_class') or 'the current remediation class'
    recommendation = task.get('recommended_remediation_action') or 'the smallest safe bounded follow-up'
    recommendation = recommendation.rstrip('.')
    age = stale_result.get('age') or 'an unknown duration'
    return (
        f'Re-dispatch one bounded slice for {goal} after preserving the stale incident evidence. '
        f'The live task is stale ({age}) with {diagnosis} / {failure_class} under {remediation_class}. '
        f'Use this next action: {recommendation}. '
        'Do not claim completion; if the executor is still active, stop and record the blocker truthfully.'
    )


def build_incident_payload(
    task: dict[str, Any],
    queue_index: int,
    stale_result: dict[str, Any],
    active_execution: dict[str, Any] | None,
    incident_created_at: str,
    incident_path: Path,
    next_action_path: Path,
) -> dict[str, Any]:
    next_action_summary = build_next_action_summary(stale_result, task)
    return {
        'incident_created_at': incident_created_at,
        'incident_created_by': SCRIPT_NAME,
        'incident_type': 'stale_execution',
        'incident_state': STALE_BLOCKED_STATUS,
        'task_key': task_key(task),
        'queue_task_index': queue_index,
        'queue_task_status_before_incident': task.get('status'),
        'queue_task_execution_state_before_incident': task.get('execution_state'),
        'source_queue_path': str(QUEUE_PATH),
        'source_active_execution_path': str(ACTIVE_EXECUTION_PATH),
        'source_queue_task_snapshot': deepcopy(task),
        'source_active_execution_snapshot': deepcopy(active_execution) if isinstance(active_execution, dict) else None,
        'watchdog_result': deepcopy(stale_result),
        'stale_execution_age_seconds': stale_result.get('age_seconds'),
        'stale_execution_age': stale_result.get('age'),
        'stale_execution_threshold_minutes': stale_result.get('threshold_minutes'),
        'stale_execution_policy_summary': stale_result.get('policy_summary'),
        'stale_execution_started_at': stale_result.get('started_at'),
        'stale_execution_executor': stale_result.get('executor'),
        'stale_execution_inspection_source': stale_result.get('inspection_source'),
        'stale_execution_recommended_next_action': stale_result.get('recommended_next_action'),
        'incident_artifact_path': str(incident_path),
        'next_action_artifact_path': str(next_action_path),
        'next_action_summary': next_action_summary,
        'next_action_mode': NEEDS_REDISPATCH_STATE,
        'bounded_redispatch_candidate': {
            'status': NEEDS_REDISPATCH_STATE,
            'execution_state': NEEDS_REDISPATCH_STATE,
            'task_key': task_key(task),
            'active_goal': task.get('active_goal'),
            'diagnosis': task.get('diagnosis'),
            'severity': task.get('severity'),
            'report_source': task.get('report_source'),
            'failure_class': task.get('failure_class'),
            'remediation_class': task.get('remediation_class'),
            'recommended_remediation_action': task.get('recommended_remediation_action'),
            'blocked_next_step': task.get('blocked_next_step'),
            'requested_executor': task.get('requested_executor'),
            'reason': 'The prior delegated execution exceeded the stale threshold; preserve the stale evidence and only re-dispatch one bounded slice if work truly needs to continue.',
            'source_stale_execution_incident_path': str(incident_path),
            'source_queue_task_index': queue_index,
        },
    }


def build_next_action_payload(
    task: dict[str, Any],
    queue_index: int,
    stale_result: dict[str, Any],
    incident_path: Path,
    next_action_path: Path,
    incident_created_at: str,
) -> dict[str, Any]:
    next_action_summary = build_next_action_summary(stale_result, task)
    return {
        'next_action_created_at': incident_created_at,
        'next_action_created_by': SCRIPT_NAME,
        'next_action_type': 'incident_next_action',
        'next_action_mode': NEEDS_REDISPATCH_STATE,
        'task_key': task_key(task),
        'queue_task_index': queue_index,
        'source_stale_execution_incident_path': str(incident_path),
        'source_queue_path': str(QUEUE_PATH),
        'source_active_execution_path': str(ACTIVE_EXECUTION_PATH),
        'source_queue_task_snapshot': deepcopy(task),
        'watchdog_result': deepcopy(stale_result),
        'stale_execution_policy_summary': stale_result.get('policy_summary'),
        'next_action_summary': next_action_summary,
        'next_action_artifact_path': str(next_action_path),
        'incident_artifact_path': str(incident_path),
        'bounded_redispatch_candidate': {
            'status': NEEDS_REDISPATCH_STATE,
            'execution_state': NEEDS_REDISPATCH_STATE,
            'task_key': task_key(task),
            'active_goal': task.get('active_goal'),
            'diagnosis': task.get('diagnosis'),
            'severity': task.get('severity'),
            'report_source': task.get('report_source'),
            'failure_class': task.get('failure_class'),
            'remediation_class': task.get('remediation_class'),
            'recommended_remediation_action': task.get('recommended_remediation_action'),
            'blocked_next_step': task.get('blocked_next_step'),
            'requested_executor': task.get('requested_executor'),
            'reason': 'Redispatch is bounded to one fresh candidate only after the stale execution has been truthfully recorded.',
        },
    }


def mark_queue_task_stale(
    queue: dict[str, Any],
    queue_index: int,
    incident_path: Path,
    next_action_path: Path,
    incident_created_at: str,
    stale_result: dict[str, Any],
) -> dict[str, Any]:
    tasks = queue_tasks(queue)
    updated_task = deepcopy(tasks[queue_index])
    updated_task['status'] = STALE_BLOCKED_STATUS
    updated_task['execution_state'] = NEEDS_REDISPATCH_STATE
    updated_task['queue_status'] = STALE_BLOCKED_STATUS
    updated_task['stale_execution_detected'] = True
    updated_task['stale_execution_detected_at'] = incident_created_at
    updated_task['stale_execution_incident_path'] = str(incident_path)
    updated_task['stale_execution_next_action_path'] = str(next_action_path)
    updated_task['stale_execution_previous_status'] = tasks[queue_index].get('status')
    updated_task['stale_execution_previous_execution_state'] = tasks[queue_index].get('execution_state')
    updated_task['stale_execution_previous_started_at'] = started_at_value(tasks[queue_index])
    updated_task['stale_execution_threshold_minutes'] = stale_result.get('threshold_minutes')
    updated_task['stale_execution_age_seconds'] = stale_result.get('age_seconds')
    updated_task['stale_execution_age'] = stale_result.get('age')
    updated_task['stale_execution_recommended_next_action'] = stale_result.get('recommended_next_action')
    updated_task['stale_execution_next_action_summary'] = build_next_action_summary(stale_result, tasks[queue_index])
    updated_task['dispatch_state'] = STALE_BLOCKED_STATUS
    updated_task['blocked_next_step'] = tasks[queue_index].get('blocked_next_step')
    tasks[queue_index] = updated_task

    updated_queue = deepcopy(queue) if isinstance(queue, dict) else {}
    updated_queue['tasks'] = tasks
    updated_queue['stale_execution_incident_path'] = str(incident_path)
    updated_queue['stale_execution_next_action_path'] = str(next_action_path)
    updated_queue['stale_execution_incident_created_at'] = incident_created_at
    updated_queue['stale_execution_incident_created_by'] = SCRIPT_NAME
    return updated_queue


def already_recorded_task(tasks: list[dict[str, Any]]) -> tuple[int | None, dict[str, Any] | None]:
    for index, task in enumerate(tasks):
        if task.get('status') not in {STALE_BLOCKED_STATUS, NEEDS_REDISPATCH_STATE}:
            continue
        if task.get('stale_execution_incident_path') or task.get('stale_execution_next_action_path'):
            return index, task
    return None, None


def consume_stale_execution_incident(
    *,
    active_execution_path: Path = ACTIVE_EXECUTION_PATH,
    queue_path: Path = QUEUE_PATH,
    incident_dir: Path = INCIDENT_DIR,
    next_action_dir: Path = NEXT_ACTION_DIR,
    latest_incident_path: Path = LATEST_INCIDENT_PATH,
    latest_next_action_path: Path = LATEST_NEXT_ACTION_PATH,
    threshold_minutes: int = DEFAULT_THRESHOLD_MINUTES,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    active_execution = load_json(active_execution_path, {})
    queue = load_json(queue_path, {'tasks': []})
    tasks = queue_tasks(queue)

    if not tasks:
        return {'consumed': False, 'reason': 'no_queue_task'}

    recorded_index, recorded_task = already_recorded_task(tasks)
    if recorded_task is not None:
        return {
            'consumed': False,
            'reason': 'already_recorded',
            'task_index': recorded_index,
            'task_key': task_key(recorded_task),
            'incident_path': recorded_task.get('stale_execution_incident_path'),
            'next_action_path': recorded_task.get('stale_execution_next_action_path'),
            'task_status': recorded_task.get('status'),
        }

    stale_result = detect_stale_execution(
        active_execution=active_execution if isinstance(active_execution, dict) else None,
        queue=queue if isinstance(queue, dict) else None,
        threshold_minutes=threshold_minutes,
        now=now,
    )
    if not stale_result['stale_detected']:
        return {
            'consumed': False,
            'reason': 'no_stale_execution_detected',
            'task_key': stale_result.get('task_key'),
            'observed_in_progress_candidates': stale_result.get('observed_in_progress_candidates'),
        }

    queue_index = stale_result.get('task_index')
    target_task: dict[str, Any] | None = None
    if isinstance(queue_index, int) and 0 <= queue_index < len(tasks):
        candidate = tasks[queue_index]
        if task_key(candidate) == stale_result.get('task_key'):
            target_task = candidate

    if target_task is None:
        queue_index = matching_queue_index(tasks, stale_result)
        if queue_index is not None:
            target_task = tasks[queue_index]

    if target_task is None or queue_index is None:
        return {
            'consumed': False,
            'reason': 'no_matching_queue_task',
            'task_key': stale_result.get('task_key'),
            'observed_in_progress_candidates': stale_result.get('observed_in_progress_candidates'),
        }

    incident_created_at = now_utc() if now is None else (now.isoformat().replace('+00:00', 'Z') if isinstance(now, datetime) else str(now))
    incident_path = incident_artifact_path(incident_dir, target_task)
    next_action_path = next_action_artifact_path(next_action_dir, target_task)

    incident_payload = build_incident_payload(
        target_task,
        queue_index,
        stale_result,
        active_execution if isinstance(active_execution, dict) else None,
        incident_created_at,
        incident_path,
        next_action_path,
    )
    next_action_payload = build_next_action_payload(
        target_task,
        queue_index,
        stale_result,
        incident_path,
        next_action_path,
        incident_created_at,
    )

    atomic_write_json(incident_path, incident_payload)
    atomic_write_json(next_action_path, next_action_payload)
    atomic_write_json(latest_incident_path, incident_payload)
    atomic_write_json(latest_next_action_path, next_action_payload)

    updated_queue = mark_queue_task_stale(
        queue if isinstance(queue, dict) else {'tasks': tasks},
        queue_index,
        incident_path,
        next_action_path,
        incident_created_at,
        stale_result,
    )
    atomic_write_json(queue_path, updated_queue)
    build_active_execution(updated_queue, incident_created_at)

    return {
        'consumed': True,
        'status': STALE_BLOCKED_STATUS,
        'execution_state': NEEDS_REDISPATCH_STATE,
        'task_index': queue_index,
        'task_key': task_key(target_task),
        'task_status_before_incident': target_task.get('status'),
        'task_status_after_incident': STALE_BLOCKED_STATUS,
        'incident_created_at': incident_created_at,
        'incident_path': str(incident_path),
        'next_action_path': str(next_action_path),
        'stale_detected': True,
        'stale_age': stale_result.get('age'),
        'stale_age_seconds': stale_result.get('age_seconds'),
        'recommended_next_action': next_action_payload['next_action_summary'],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Convert a stale in_progress execution into a durable stale incident and redispatch candidate under the 30-minute investigation rule.')
    parser.add_argument('--threshold-minutes', type=int, default=DEFAULT_THRESHOLD_MINUTES, help='Stale-investigation threshold in minutes (default: 30).')
    parser.add_argument('--active-execution-path', type=Path, default=ACTIVE_EXECUTION_PATH, help='Path to active_execution.json.')
    parser.add_argument('--queue-path', type=Path, default=QUEUE_PATH, help='Path to execution_queue.json.')
    parser.add_argument('--incident-dir', type=Path, default=INCIDENT_DIR, help='Directory for stale incident artifacts.')
    parser.add_argument('--next-action-dir', type=Path, default=NEXT_ACTION_DIR, help='Directory for stale next-action artifacts.')
    parser.add_argument('--latest-incident-path', type=Path, default=LATEST_INCIDENT_PATH, help='Pointer to the latest stale incident artifact.')
    parser.add_argument('--latest-next-action-path', type=Path, default=LATEST_NEXT_ACTION_PATH, help='Pointer to the latest stale next-action artifact.')
    args = parser.parse_args()

    result = consume_stale_execution_incident(
        active_execution_path=args.active_execution_path,
        queue_path=args.queue_path,
        incident_dir=args.incident_dir,
        next_action_dir=args.next_action_dir,
        latest_incident_path=args.latest_incident_path,
        latest_next_action_path=args.latest_next_action_path,
        threshold_minutes=args.threshold_minutes,
    )
    result['active_execution_path'] = str(args.active_execution_path)
    result['queue_path'] = str(args.queue_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
