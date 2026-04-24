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

import scripts.build_status_snapshot as status_snapshot

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
ACTIVE_EXECUTION_PATH = ROOT / 'control' / 'active_execution.json'
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
ASSIGNMENT_DIR = ROOT / 'control' / 'execution_assignments'
LATEST_ASSIGNMENT_PATH = ROOT / 'control' / 'execution_assignment.json'
SCRIPT_NAME = 'consume_queued_redispatch_assignments.py'
ELIGIBLE_STATUSES = {'queued'}


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


def timestamp_slug(timestamp: str) -> str:
    return timestamp.replace('-', '').replace(':', '').replace('.', '')


def started_at_value(task: dict[str, Any]) -> str | None:
    for key in ('delegated_executor_started_at', 'execution_requested_at', 'executor_handoff_at', 'dispatched_at', 'created_at', 'started_at'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def queue_tasks(queue: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(queue, dict):
        return []
    maybe_tasks = queue.get('tasks')
    if not isinstance(maybe_tasks, list):
        return []
    return [task for task in maybe_tasks if isinstance(task, dict)]


def eligible_redispatch_task(task: dict[str, Any]) -> bool:
    if task.get('status') not in ELIGIBLE_STATUSES:
        return False
    return any(
        task.get(key)
        for key in (
            'stale_execution_redispatch_artifact_path',
            'stale_execution_redispatch_source_next_action_path',
            'stale_execution_redispatch_source_incident_path',
            'stale_execution_next_action_path',
        )
    )


def refresh_active_execution(active_execution_path: Path, queue_path: Path, tasks: list[dict[str, Any]], updated_at: str) -> dict[str, Any]:
    previous_active_execution_path = status_snapshot.ACTIVE_EXECUTION
    previous_queue_path = status_snapshot.QUEUE
    try:
        status_snapshot.ACTIVE_EXECUTION = active_execution_path
        status_snapshot.QUEUE = queue_path
        return status_snapshot.build_active_execution({'tasks': tasks}, updated_at)
    finally:
        status_snapshot.ACTIVE_EXECUTION = previous_active_execution_path
        status_snapshot.QUEUE = previous_queue_path


def assignment_path(assignment_dir: Path, task: dict[str, Any], assignment_created_at: str) -> Path:
    return assignment_dir / f'{timestamp_slug(assignment_created_at)}-{artifact_task_key(task)}.json'


def find_existing_assignment(
    assignment_dir: Path,
    task: dict[str, Any],
) -> tuple[Path | None, dict[str, Any] | None]:
    if not assignment_dir.exists():
        return None, None

    target_task_key = task_key(task)
    target_redispatch_path = task.get('stale_execution_redispatch_artifact_path')
    target_incident_path = task.get('stale_execution_redispatch_source_incident_path') or task.get('stale_execution_incident_path')
    target_next_action_path = task.get('stale_execution_redispatch_source_next_action_path') or task.get('stale_execution_next_action_path')

    for path in sorted(assignment_dir.glob('*.json')):
        payload = load_json(path, None)
        if not isinstance(payload, dict):
            continue
        if payload.get('task_key') != target_task_key:
            continue
        if target_redispatch_path and payload.get('source_stale_execution_redispatch_path') != target_redispatch_path:
            continue
        if target_incident_path and payload.get('source_stale_execution_incident_path') != target_incident_path:
            continue
        if target_next_action_path and payload.get('source_stale_execution_next_action_path') != target_next_action_path:
            continue
        return path, payload

    return None, None


def build_assignment_payload(
    *,
    queue_task: dict[str, Any],
    queue_task_index: int,
    assignment_created_at: str,
    assignment_path_value: Path,
    existing_assignment: dict[str, Any] | None,
) -> dict[str, Any]:
    redispatch_snapshot = None
    redispatch_path_value = queue_task.get('stale_execution_redispatch_artifact_path')
    if isinstance(redispatch_path_value, str) and redispatch_path_value.strip():
        redispatch_snapshot = load_json(Path(redispatch_path_value.strip()), None)
    return {
        'execution_assignment_created_at': assignment_created_at,
        'execution_assignment_created_by': SCRIPT_NAME,
        'execution_assignment_type': 'queued_redispatch_execution_assignment',
        'execution_assignment_state': 'in_progress',
        'task_key': task_key(queue_task),
        'queue_task_index': queue_task_index,
        'queue_task_status_before_assignment': queue_task.get('status'),
        'queue_task_execution_state_before_assignment': queue_task.get('execution_state'),
        'queue_task_previous_started_at': started_at_value(queue_task),
        'source_queue_path': str(QUEUE_PATH),
        'source_queue_task_snapshot': deepcopy(queue_task),
        'source_stale_execution_incident_path': queue_task.get('stale_execution_redispatch_source_incident_path') or queue_task.get('stale_execution_incident_path'),
        'source_stale_execution_next_action_path': queue_task.get('stale_execution_redispatch_source_next_action_path') or queue_task.get('stale_execution_next_action_path'),
        'source_stale_execution_redispatch_path': queue_task.get('stale_execution_redispatch_artifact_path'),
        'source_stale_execution_redispatch_snapshot': deepcopy(redispatch_snapshot) if isinstance(redispatch_snapshot, dict) else None,
        'source_stale_execution_redispatch_summary': queue_task.get('stale_execution_redispatch_next_action_summary') or queue_task.get('stale_execution_next_action_summary'),
        'source_stale_execution_redispatch_candidate': deepcopy(queue_task.get('stale_execution_redispatch_candidate')) if isinstance(queue_task.get('stale_execution_redispatch_candidate'), dict) else None,
        'source_stale_execution_redispatch_previous_task_snapshot': deepcopy(queue_task.get('stale_execution_previous_task_snapshot')) if isinstance(queue_task.get('stale_execution_previous_task_snapshot'), dict) else None,
        'delegated_executor_requested_at': assignment_created_at,
        'delegated_executor_started_at': assignment_created_at,
        'delegated_executor_request_path': queue_task.get('delegated_executor_request_path'),
        'delegated_executor_request_status': queue_task.get('delegated_executor_request_status'),
        'delegated_executor_request_executor': queue_task.get('delegated_executor_request_executor'),
        'delegated_executor_request_reason': queue_task.get('delegated_executor_request_reason'),
        'assignment_artifact_path': str(assignment_path_value),
        'assignment_reason': queue_task.get('stale_execution_redispatch_next_action_summary')
        or queue_task.get('stale_execution_next_action_summary')
        or queue_task.get('stale_execution_recommended_next_action'),
        'assignment_is_recovery': existing_assignment is not None,
        'assignment_previous_artifact_path': existing_assignment.get('assignment_artifact_path') if isinstance(existing_assignment, dict) else None,
    }


def apply_assignment_to_queue(
    task: dict[str, Any],
    assignment_created_at: str,
    assignment_path_value: Path,
) -> dict[str, Any]:
    updated_task = deepcopy(task)
    updated_task['status'] = 'in_progress'
    updated_task['queue_status'] = 'in_progress'
    updated_task['execution_state'] = 'in_progress'
    updated_task['execution_assignment_path'] = str(assignment_path_value)
    updated_task['execution_assignment_created_at'] = assignment_created_at
    updated_task['execution_assignment_created_by'] = SCRIPT_NAME
    updated_task['execution_assignment_type'] = 'queued_redispatch_execution_assignment'
    updated_task['execution_assignment_state'] = 'in_progress'
    updated_task['execution_assignment_previous_status'] = task.get('status')
    updated_task['execution_assignment_previous_execution_state'] = task.get('execution_state')
    updated_task['execution_assignment_previous_queue_status'] = task.get('queue_status') or task.get('status')
    updated_task['execution_assignment_previous_started_at'] = started_at_value(task)
    updated_task['execution_assignment_source_redispatch_path'] = task.get('stale_execution_redispatch_artifact_path')
    updated_task['execution_assignment_source_incident_path'] = task.get('stale_execution_redispatch_source_incident_path') or task.get('stale_execution_incident_path')
    updated_task['execution_assignment_source_next_action_path'] = task.get('stale_execution_redispatch_source_next_action_path') or task.get('stale_execution_next_action_path')
    updated_task['execution_assignment_source_task_snapshot'] = deepcopy(task)
    updated_task['delegated_executor_requested_at'] = assignment_created_at
    updated_task['delegated_executor_started_at'] = assignment_created_at
    return updated_task


def consume_queued_redispatch_assignment(
    *,
    active_execution_path: Path = ACTIVE_EXECUTION_PATH,
    queue_path: Path = QUEUE_PATH,
    assignment_dir: Path = ASSIGNMENT_DIR,
    latest_assignment_path: Path = LATEST_ASSIGNMENT_PATH,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    queue = load_json(queue_path, {'tasks': []})
    tasks = queue_tasks(queue)
    if not tasks:
        return {'consumed': False, 'reason': 'no_queue_task'}

    for index, task in enumerate(tasks):
        existing_assignment_path_value = task.get('execution_assignment_path')
        if isinstance(existing_assignment_path_value, str) and existing_assignment_path_value.strip() and task.get('status') == 'in_progress':
            existing_assignment_path = Path(existing_assignment_path_value.strip())
            existing_assignment = load_json(existing_assignment_path, None) if existing_assignment_path.exists() else None
            if isinstance(existing_assignment, dict):
                active_execution = refresh_active_execution(active_execution_path, queue_path, tasks, now_utc())
                return {
                    'consumed': False,
                    'reason': 'already_recorded',
                    'task_index': index,
                    'task_key': task_key(task),
                    'assignment_path': str(existing_assignment_path),
                    'status': task.get('status'),
                    'execution_state': task.get('execution_state'),
                    'has_live_delegated_execution': active_execution.get('has_actually_executing_task'),
                }

        if not eligible_redispatch_task(task):
            continue

        if isinstance(existing_assignment_path_value, str) and existing_assignment_path_value.strip():
            existing_assignment_path = Path(existing_assignment_path_value.strip())
            existing_assignment = load_json(existing_assignment_path, None) if existing_assignment_path.exists() else None
            if task.get('status') == 'queued' and isinstance(existing_assignment, dict):
                assignment_created_at = existing_assignment.get('execution_assignment_created_at')
                if not isinstance(assignment_created_at, str) or not assignment_created_at.strip():
                    assignment_created_at = now_utc() if now is None else (now.isoformat().replace('+00:00', 'Z') if isinstance(now, datetime) else str(now))
                updated_task = apply_assignment_to_queue(task, assignment_created_at, existing_assignment_path)
                tasks[index] = updated_task
                atomic_write_json(queue_path, {'tasks': tasks})
                atomic_write_json(latest_assignment_path, existing_assignment)
                refresh_active_execution(active_execution_path, queue_path, tasks, assignment_created_at)
                return {
                    'consumed': True,
                    'reason': 'recovered_existing_assignment',
                    'task_index': index,
                    'task_key': task_key(updated_task),
                    'assignment_path': str(existing_assignment_path),
                    'assignment_created_at': assignment_created_at,
                    'status': updated_task['status'],
                    'execution_state': updated_task['execution_state'],
                    'has_live_delegated_execution': True,
                }

        if task.get('status') != 'queued':
            continue

        assignment_created_at = now_utc() if now is None else (now.isoformat().replace('+00:00', 'Z') if isinstance(now, datetime) else str(now))
        existing_path, existing_assignment = find_existing_assignment(assignment_dir, task)
        if existing_path is not None and isinstance(existing_assignment, dict):
            assignment_path_value = existing_path
            assignment_payload = existing_assignment
            assignment_created_at = existing_assignment.get('execution_assignment_created_at') if isinstance(existing_assignment.get('execution_assignment_created_at'), str) else assignment_created_at
            updated_task = apply_assignment_to_queue(task, assignment_created_at, assignment_path_value)
            tasks[index] = updated_task
            atomic_write_json(queue_path, {'tasks': tasks})
            atomic_write_json(latest_assignment_path, assignment_payload)
            refresh_active_execution(active_execution_path, queue_path, tasks, assignment_created_at)
            return {
                'consumed': True,
                'reason': 'recovered_existing_assignment',
                'task_index': index,
                'task_key': task_key(updated_task),
                'assignment_path': str(assignment_path_value),
                'assignment_created_at': assignment_created_at,
                'status': updated_task['status'],
                'execution_state': updated_task['execution_state'],
                'has_live_delegated_execution': True,
            }

        assignment_path_value = assignment_path(assignment_dir, task, assignment_created_at)
        assignment_payload = build_assignment_payload(
            queue_task=task,
            queue_task_index=index,
            assignment_created_at=assignment_created_at,
            assignment_path_value=assignment_path_value,
            existing_assignment=None,
        )
        atomic_write_json(assignment_path_value, assignment_payload)
        atomic_write_json(latest_assignment_path, assignment_payload)

        updated_task = apply_assignment_to_queue(task, assignment_created_at, assignment_path_value)
        tasks[index] = updated_task
        atomic_write_json(queue_path, {'tasks': tasks})
        active_execution = refresh_active_execution(active_execution_path, queue_path, tasks, assignment_created_at)

        return {
            'consumed': True,
            'status': updated_task['status'],
            'execution_state': updated_task['execution_state'],
            'task_index': index,
            'task_key': task_key(updated_task),
            'assignment_created_at': assignment_created_at,
            'assignment_path': str(assignment_path_value),
            'source_redispatch_path': updated_task.get('execution_assignment_source_redispatch_path'),
            'source_incident_path': updated_task.get('execution_assignment_source_incident_path'),
            'source_next_action_path': updated_task.get('execution_assignment_source_next_action_path'),
            'has_live_delegated_execution': active_execution.get('has_actually_executing_task'),
        }

    return {'consumed': False, 'reason': 'no_eligible_queued_redispatch_task'}


def main() -> None:
    parser = argparse.ArgumentParser(description='Convert one queued stale redispatch line into a fresh live delegated execution assignment.')
    parser.add_argument('--active-execution-path', type=Path, default=ACTIVE_EXECUTION_PATH, help='Path to active_execution.json.')
    parser.add_argument('--queue-path', type=Path, default=QUEUE_PATH, help='Path to execution_queue.json.')
    parser.add_argument('--assignment-dir', type=Path, default=ASSIGNMENT_DIR, help='Directory for durable execution assignment artifacts.')
    parser.add_argument('--latest-assignment-path', type=Path, default=LATEST_ASSIGNMENT_PATH, help='Pointer to the latest execution assignment artifact.')
    args = parser.parse_args()

    result = consume_queued_redispatch_assignment(
        active_execution_path=args.active_execution_path,
        queue_path=args.queue_path,
        assignment_dir=args.assignment_dir,
        latest_assignment_path=args.latest_assignment_path,
    )
    result['active_execution_path'] = str(args.active_execution_path)
    result['queue_path'] = str(args.queue_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
