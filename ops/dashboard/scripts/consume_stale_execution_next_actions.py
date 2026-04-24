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

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
ACTIVE_EXECUTION_PATH = ROOT / 'control' / 'active_execution.json'
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
NEXT_ACTION_DIR = ROOT / 'control' / 'stale_execution_next_actions'
REDISPATCH_DIR = ROOT / 'control' / 'stale_execution_redispatches'
LATEST_REDISPATCH_PATH = ROOT / 'control' / 'stale_execution_redispatch.json'
SCRIPT_NAME = 'consume_stale_execution_next_actions.py'

REDISPATCH_STATUS = 'queued'
REDISPATCH_EXECUTION_STATE = 'queued'


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


def started_at_value(task: dict[str, Any]) -> str | None:
    for key in ('delegated_executor_started_at', 'execution_requested_at', 'executor_handoff_at', 'dispatched_at', 'created_at', 'started_at'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def timestamp_slug(timestamp: str) -> str:
    return timestamp.replace('-', '').replace(':', '').replace('.', '')


def artifact_path(redispatch_dir: Path, task: dict[str, Any], redispatch_created_at: str) -> Path:
    return redispatch_dir / f'{timestamp_slug(redispatch_created_at)}-{artifact_task_key(task)}.json'


def queue_tasks(queue: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(queue, dict):
        return []
    maybe_tasks = queue.get('tasks')
    if not isinstance(maybe_tasks, list):
        return []
    return [task for task in maybe_tasks if isinstance(task, dict)]


def load_next_action_candidates(next_action_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not next_action_dir.exists():
        return []
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(next_action_dir.glob('*.json')):
        payload = load_json(path, None)
        if not isinstance(payload, dict):
            continue
        if payload.get('next_action_mode') != 'needs_redispatch':
            continue
        if payload.get('next_action_type') != 'incident_next_action':
            continue
        if not isinstance(payload.get('bounded_redispatch_candidate'), dict):
            continue
        candidates.append((path, payload))
    return candidates


def redispatch_already_recorded(tasks: list[dict[str, Any]], next_action_path: Path) -> tuple[int | None, dict[str, Any] | None]:
    next_action_path_text = str(next_action_path)
    for index, task in enumerate(tasks):
        if task.get('stale_execution_redispatch_artifact_path'):
            if task.get('stale_execution_redispatch_source_next_action_path') == next_action_path_text:
                return index, task
        if task.get('stale_execution_redispatch_source_next_action_path') == next_action_path_text:
            return index, task
        if task.get('stale_execution_redispatch_created_by') == SCRIPT_NAME and task.get('stale_execution_redispatch_source_next_action_path') == next_action_path_text:
            return index, task
    return None, None


def locate_target_task(tasks: list[dict[str, Any]], next_action_payload: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None]:
    candidate_index = next_action_payload.get('queue_task_index')
    target_key = next_action_payload.get('task_key')
    source_incident_path = next_action_payload.get('source_stale_execution_incident_path')

    if isinstance(candidate_index, int) and 0 <= candidate_index < len(tasks):
        candidate = tasks[candidate_index]
        if isinstance(target_key, str) and target_key.strip() and task_key(candidate) == target_key.strip():
            return candidate_index, candidate

    for index, task in enumerate(tasks):
        if isinstance(target_key, str) and target_key.strip() and task_key(task) != target_key.strip():
            continue
        if isinstance(source_incident_path, str) and source_incident_path.strip():
            if task.get('stale_execution_incident_path') != source_incident_path.strip():
                continue
        return index, task

    if tasks:
        return 0, tasks[0]
    return None, None


def build_redispatch_task(
    *,
    queue_task: dict[str, Any],
    queue_task_index: int,
    next_action_payload: dict[str, Any],
    next_action_path: Path,
    redispatch_created_at: str,
    redispatch_path: Path,
) -> dict[str, Any]:
    redispatched_task = deepcopy(queue_task)
    redispatched_task['status'] = REDISPATCH_STATUS
    redispatched_task['execution_state'] = REDISPATCH_EXECUTION_STATE
    redispatched_task['queue_status'] = REDISPATCH_STATUS
    redispatched_task['dispatch_state'] = REDISPATCH_STATUS
    redispatched_task['redispatch_created_at'] = redispatch_created_at
    redispatched_task['redispatch_created_by'] = SCRIPT_NAME
    redispatched_task['redispatch_state'] = REDISPATCH_STATUS
    redispatched_task['redispatch_source'] = 'stale_execution_next_action'
    redispatched_task['stale_execution_redispatch_artifact_path'] = str(redispatch_path)
    redispatched_task['stale_execution_redispatch_created_at'] = redispatch_created_at
    redispatched_task['stale_execution_redispatch_created_by'] = SCRIPT_NAME
    redispatched_task['stale_execution_redispatch_source_next_action_path'] = str(next_action_path)
    redispatched_task['stale_execution_redispatch_source_incident_path'] = next_action_payload.get('source_stale_execution_incident_path')
    redispatched_task['stale_execution_redispatch_source_queue_path'] = next_action_payload.get('source_queue_path')
    redispatched_task['stale_execution_redispatch_source_task_index'] = queue_task_index
    redispatched_task['stale_execution_redispatch_previous_status'] = queue_task.get('status')
    redispatched_task['stale_execution_redispatch_previous_execution_state'] = queue_task.get('execution_state')
    redispatched_task['stale_execution_redispatch_previous_queue_status'] = queue_task.get('queue_status') or queue_task.get('status')
    redispatched_task['stale_execution_redispatch_previous_started_at'] = started_at_value(queue_task)
    redispatched_task['stale_execution_redispatch_next_action_summary'] = next_action_payload.get('next_action_summary')
    redispatched_task['stale_execution_redispatch_candidate'] = deepcopy(next_action_payload.get('bounded_redispatch_candidate'))
    redispatched_task['stale_execution_redispatch_linked_incident_path'] = next_action_payload.get('incident_artifact_path')
    redispatched_task['stale_execution_redispatch_linked_next_action_path'] = str(next_action_path)
    redispatched_task['stale_execution_redispatch_linked_summary'] = next_action_payload.get('next_action_summary')
    redispatched_task['stale_execution_detected'] = bool(queue_task.get('stale_execution_detected', True))
    redispatched_task['stale_execution_detected_at'] = queue_task.get('stale_execution_detected_at')
    redispatched_task['stale_execution_incident_path'] = queue_task.get('stale_execution_incident_path') or next_action_payload.get('source_stale_execution_incident_path')
    redispatched_task['stale_execution_next_action_path'] = str(next_action_path)
    redispatched_task['stale_execution_previous_task_snapshot'] = deepcopy(queue_task)
    return redispatched_task


def consume_stale_execution_next_action(
    *,
    active_execution_path: Path = ACTIVE_EXECUTION_PATH,
    queue_path: Path = QUEUE_PATH,
    next_action_dir: Path = NEXT_ACTION_DIR,
    redispatch_dir: Path = REDISPATCH_DIR,
    latest_redispatch_path: Path = LATEST_REDISPATCH_PATH,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    queue = load_json(queue_path, {'tasks': []})
    tasks = queue_tasks(queue)
    if not tasks:
        return {'consumed': False, 'reason': 'no_queue_task'}

    candidates = load_next_action_candidates(next_action_dir)
    if not candidates:
        return {'consumed': False, 'reason': 'no_eligible_next_action'}

    next_action_path, next_action_payload = candidates[0]
    recorded_index, recorded_task = redispatch_already_recorded(tasks, next_action_path)
    if recorded_task is not None:
        return {
            'consumed': False,
            'reason': 'already_recorded',
            'task_index': recorded_index,
            'task_key': task_key(recorded_task),
            'redispatch_path': recorded_task.get('stale_execution_redispatch_artifact_path'),
            'next_action_path': str(next_action_path),
            'task_status': recorded_task.get('status'),
        }

    queue_index, target_task = locate_target_task(tasks, next_action_payload)
    if target_task is None or queue_index is None:
        return {
            'consumed': False,
            'reason': 'no_matching_queue_task',
            'next_action_path': str(next_action_path),
            'task_key': next_action_payload.get('task_key'),
        }

    redispatch_created_at = now_utc() if now is None else (now.isoformat().replace('+00:00', 'Z') if isinstance(now, datetime) else str(now))
    redispatch_path = artifact_path(redispatch_dir, target_task, redispatch_created_at)
    redispatched_task = build_redispatch_task(
        queue_task=target_task,
        queue_task_index=queue_index,
        next_action_payload=next_action_payload,
        next_action_path=next_action_path,
        redispatch_created_at=redispatch_created_at,
        redispatch_path=redispatch_path,
    )

    updated_tasks = deepcopy(tasks)
    updated_tasks[queue_index] = redispatched_task
    updated_queue = deepcopy(queue) if isinstance(queue, dict) else {}
    updated_queue['tasks'] = updated_tasks
    updated_queue['stale_execution_redispatch_path'] = str(redispatch_path)
    updated_queue['stale_execution_redispatch_created_at'] = redispatch_created_at
    updated_queue['stale_execution_redispatch_created_by'] = SCRIPT_NAME
    updated_queue['stale_execution_redispatch_source_next_action_path'] = str(next_action_path)
    updated_queue['stale_execution_redispatch_source_incident_path'] = next_action_payload.get('source_stale_execution_incident_path')

    redispatch_artifact = {
        'redispatch_created_at': redispatch_created_at,
        'redispatch_created_by': SCRIPT_NAME,
        'redispatch_type': 'stale_next_action_redispatch',
        'redispatch_state': REDISPATCH_STATUS,
        'task_key': task_key(redispatched_task),
        'queue_task_index': queue_index,
        'source_queue_path': str(queue_path),
        'source_active_execution_path': str(active_execution_path),
        'source_stale_execution_next_action_path': str(next_action_path),
        'source_stale_execution_incident_path': next_action_payload.get('source_stale_execution_incident_path'),
        'source_queue_task_snapshot': deepcopy(target_task),
        'redispatched_queue_task_snapshot': deepcopy(redispatched_task),
        'next_action_summary': next_action_payload.get('next_action_summary'),
        'next_action_payload': deepcopy(next_action_payload),
        'redispatch_artifact_path': str(redispatch_path),
        'redispatch_linked_incident_path': next_action_payload.get('incident_artifact_path'),
        'redispatch_linked_next_action_path': str(next_action_path),
    }

    atomic_write_json(redispatch_path, redispatch_artifact)
    atomic_write_json(latest_redispatch_path, redispatch_artifact)
    atomic_write_json(queue_path, updated_queue)
    build_active_execution(updated_queue, redispatch_created_at)

    return {
        'consumed': True,
        'status': redispatched_task['status'],
        'execution_state': redispatched_task['execution_state'],
        'task_index': queue_index,
        'task_key': task_key(redispatched_task),
        'redispatch_created_at': redispatch_created_at,
        'redispatch_path': str(redispatch_path),
        'next_action_path': str(next_action_path),
        'source_incident_path': next_action_payload.get('source_stale_execution_incident_path'),
        'task_status_before_redispatch': target_task.get('status'),
        'task_status_after_redispatch': redispatched_task['status'],
        'task_execution_state_after_redispatch': redispatched_task['execution_state'],
        'redispatch_state': REDISPATCH_STATUS,
        'next_action_summary': next_action_payload.get('next_action_summary'),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Convert one stale next-action artifact into a fresh bounded redispatch queue task.')
    parser.add_argument('--active-execution-path', type=Path, default=ACTIVE_EXECUTION_PATH, help='Path to active_execution.json.')
    parser.add_argument('--queue-path', type=Path, default=QUEUE_PATH, help='Path to execution_queue.json.')
    parser.add_argument('--next-action-dir', type=Path, default=NEXT_ACTION_DIR, help='Directory containing stale next-action artifacts.')
    parser.add_argument('--redispatch-dir', type=Path, default=REDISPATCH_DIR, help='Directory for durable redispatch artifacts.')
    parser.add_argument('--latest-redispatch-path', type=Path, default=LATEST_REDISPATCH_PATH, help='Pointer to the latest redispatch artifact.')
    args = parser.parse_args()

    result = consume_stale_execution_next_action(
        active_execution_path=args.active_execution_path,
        queue_path=args.queue_path,
        next_action_dir=args.next_action_dir,
        redispatch_dir=args.redispatch_dir,
        latest_redispatch_path=args.latest_redispatch_path,
    )
    result['active_execution_path'] = str(args.active_execution_path)
    result['queue_path'] = str(args.queue_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
