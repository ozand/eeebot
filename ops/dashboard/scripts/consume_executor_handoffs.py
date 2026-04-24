#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
REQUEST_DIR = ROOT / 'control' / 'execution_requests'
HANDOFF_DIR = ROOT / 'control' / 'executor_handoffs'
SCRIPT_NAME = 'consume_executor_handoffs.py'

REQUEST_ELIGIBLE_STATUSES = {'requested'}
QUEUE_ELIGIBLE_STATUSES = {'requested_execution'}
HANDOFF_STATUS = 'handed_off'


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
    for key in ('dedupe_key', 'active_goal', 'report_source', 'diagnosis'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return slugify(value)
    return 'task'


def source_request_path_value(request: dict[str, Any]) -> str | None:
    value = request.get('source_execution_request_path')
    if isinstance(value, str) and value.strip():
        return value
    value = request.get('execution_request_path')
    if isinstance(value, str) and value.strip():
        return value
    return None


def build_handoff_artifact(
    request: dict[str, Any],
    request_path: Path,
    handoff_created_at: str,
    queue_task: dict[str, Any] | None,
) -> dict[str, Any]:
    artifact = {
        'handoff_created_at': handoff_created_at,
        'handoff_status': HANDOFF_STATUS,
        'handoff_by': SCRIPT_NAME,
        'source_execution_request_path': str(request_path),
        'requested_executor': request.get('requested_executor'),
        'diagnosis': request.get('diagnosis'),
        'active_goal': request.get('active_goal'),
        'failure_class': request.get('failure_class'),
        'remediation_class': request.get('remediation_class'),
        'recommended_remediation_action': request.get('recommended_remediation_action'),
        'severity': request.get('severity'),
        'blocked_next_step': request.get('blocked_next_step'),
        'source_execution_request_status_before_handoff': request.get('status'),
        'source_execution_request': request,
    }
    if queue_task is not None:
        artifact['source_queue_task'] = queue_task
        artifact['source_queue_task_status_before_handoff'] = queue_task.get('status')
        artifact['source_queue_path'] = str(QUEUE_PATH)
        artifact['queue_task_key'] = task_key(queue_task)
    return artifact


def queue_task_matches_request(task: dict[str, Any], request_path: Path) -> bool:
    candidate_paths = []
    for key in ('execution_request_path', 'source_execution_request_path'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            candidate_paths.append(value)
    request_path_str = str(request_path)
    return request_path_str in candidate_paths


def main() -> None:
    queue = load_json(QUEUE_PATH, {'tasks': []})
    tasks = queue.get('tasks') if isinstance(queue, dict) else []
    if not isinstance(tasks, list):
        tasks = []

    request_files = sorted(REQUEST_DIR.glob('*.json'), key=lambda path: path.name)
    if not request_files:
        print(json.dumps({'consumed': False, 'reason': 'no_execution_request'}, ensure_ascii=False))
        return

    for request_path in request_files:
        request = load_json(request_path, None)
        if not isinstance(request, dict):
            continue

        request_status = request.get('status')
        if request_status == HANDOFF_STATUS or request.get('executor_handoff_path'):
            continue

        if request_status not in REQUEST_ELIGIBLE_STATUSES:
            continue

        matched_index = None
        matched_task = None
        for index, task in enumerate(tasks):
            if isinstance(task, dict) and queue_task_matches_request(task, request_path):
                matched_index = index
                matched_task = task
                break

        if matched_task is not None:
            queue_status = matched_task.get('status')
            if queue_status not in QUEUE_ELIGIBLE_STATUSES:
                continue
        else:
            queue_status = None

        handoff_created_at = now_utc()
        handoff_path = HANDOFF_DIR / request_path.name
        handoff_payload = build_handoff_artifact(request, request_path, handoff_created_at, matched_task)
        atomic_write_json(handoff_path, handoff_payload)

        updated_request = dict(request)
        updated_request['status'] = HANDOFF_STATUS
        updated_request['executor_handoff_at'] = handoff_created_at
        updated_request['executor_handoff_path'] = str(handoff_path)
        updated_request['executor_handoff_status'] = HANDOFF_STATUS
        updated_request['handoff_created_at'] = handoff_created_at
        updated_request['handoff_status'] = HANDOFF_STATUS
        atomic_write_json(request_path, updated_request)

        if matched_index is not None and matched_task is not None:
            updated_task = dict(matched_task)
            updated_task['status'] = HANDOFF_STATUS
            updated_task['executor_handoff_at'] = handoff_created_at
            updated_task['executor_handoff_path'] = str(handoff_path)
            updated_task['execution_request_status'] = HANDOFF_STATUS
            updated_task['execution_handoff_status'] = HANDOFF_STATUS
            tasks[matched_index] = updated_task
            atomic_write_json(QUEUE_PATH, {'tasks': tasks})
        elif queue:
            atomic_write_json(QUEUE_PATH, {'tasks': tasks})

        output = {
            'consumed': True,
            'status': HANDOFF_STATUS,
            'execution_request_path': str(request_path),
            'executor_handoff_path': str(handoff_path),
            'handoff_created_at': handoff_created_at,
            'requested_executor': request.get('requested_executor'),
            'task_key': task_key(matched_task) if isinstance(matched_task, dict) else None,
            'queue_task_index': matched_index,
            'queue_task_status_before_handoff': queue_status,
        }
        print(json.dumps(output, ensure_ascii=False))
        return

    print(json.dumps({'consumed': False, 'reason': 'no_eligible_execution_request'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
