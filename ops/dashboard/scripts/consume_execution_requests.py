#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
DISPATCH_DIR = ROOT / 'control' / 'dispatched'
REQUEST_DIR = ROOT / 'control' / 'execution_requests'
LATEST_DISPATCH_PATH = ROOT / 'control' / 'execution_dispatch.json'
SCRIPT_NAME = 'consume_execution_requests.py'
REQUESTED_EXECUTOR = 'pi_dev'

ELIGIBLE_STATUSES = {'in_progress', 'dispatched'}
HANDOFF_STATUSES = {'requested_execution'}


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


def timestamp_slug(timestamp: str) -> str:
    return timestamp.replace('-', '').replace(':', '').replace('.', '')


def dispatch_artifact_path(task: dict[str, Any]) -> Path | None:
    dispatched_at = task.get('dispatched_at')
    if not isinstance(dispatched_at, str) or not dispatched_at.strip():
        return None
    candidate = DISPATCH_DIR / f"{timestamp_slug(dispatched_at)}-{task_key(task)}.json"
    if candidate.exists():
        return candidate
    return None


def build_request_payload(task: dict[str, Any], requested_at: str, source_dispatch_path: str | None) -> dict[str, Any]:
    payload = {
        'requested_at': requested_at,
        'requested_by': SCRIPT_NAME,
        'status': 'requested',
        'requested_executor': REQUESTED_EXECUTOR,
        'diagnosis': task.get('diagnosis'),
        'severity': task.get('severity'),
        'active_goal': task.get('active_goal'),
        'report_source': task.get('report_source'),
        'failure_class': task.get('failure_class'),
        'remediation_class': task.get('remediation_class'),
        'recommended_remediation_action': task.get('recommended_remediation_action'),
        'blocked_next_step': task.get('blocked_next_step'),
        'queue_path': str(QUEUE_PATH),
        'queue_task_status_before_handoff': task.get('status'),
        'queue_task_key': task_key(task),
        'source_dispatch_artifact_path': source_dispatch_path,
        'source_dispatch_pointer_path': str(LATEST_DISPATCH_PATH) if LATEST_DISPATCH_PATH.exists() else None,
        'source_queue_task': task,
    }
    return payload


def main() -> None:
    queue = load_json(QUEUE_PATH, {'tasks': []})
    tasks = queue.get('tasks') if isinstance(queue, dict) else []
    if not isinstance(tasks, list) or not tasks:
        print(json.dumps({'consumed': False, 'reason': 'no_queued_task'}, ensure_ascii=False))
        return

    first_task = tasks[0]
    if not isinstance(first_task, dict):
        print(json.dumps({'consumed': False, 'reason': 'first_task_not_object'}, ensure_ascii=False))
        return

    status = first_task.get('status')
    if status in HANDOFF_STATUSES or first_task.get('executor_handoff_path'):
        print(
            json.dumps(
                {
                    'consumed': False,
                    'reason': 'first_task_already_handed_off',
                    'task_index': 0,
                    'task_status': status,
                    'executor_handoff_path': first_task.get('executor_handoff_path'),
                },
                ensure_ascii=False,
            )
        )
        return

    if status not in ELIGIBLE_STATUSES:
        print(
            json.dumps(
                {
                    'consumed': False,
                    'reason': 'first_task_not_eligible',
                    'task_index': 0,
                    'task_status': status,
                },
                ensure_ascii=False,
            )
        )
        return

    source_dispatch = dispatch_artifact_path(first_task)
    requested_at = now_utc()
    request_payload = build_request_payload(first_task, requested_at, str(source_dispatch) if source_dispatch else None)
    request_stamp = timestamp_slug(requested_at)
    request_path = REQUEST_DIR / f"{request_stamp}-{task_key(first_task)}.json"
    atomic_write_json(request_path, request_payload)

    updated_task = dict(first_task)
    updated_task['status'] = 'requested_execution'
    updated_task['execution_requested_at'] = requested_at
    updated_task['execution_request_path'] = str(request_path)
    updated_task['requested_executor'] = REQUESTED_EXECUTOR
    updated_task['execution_request_status'] = 'requested'
    if source_dispatch:
        updated_task['execution_dispatch_path'] = str(source_dispatch)
    tasks[0] = updated_task
    atomic_write_json(QUEUE_PATH, {'tasks': tasks})

    output = {
        'consumed': True,
        'status': 'requested_execution',
        'task_index': 0,
        'task_key': task_key(updated_task),
        'execution_requested_at': requested_at,
        'execution_request_path': str(request_path),
        'requested_executor': REQUESTED_EXECUTOR,
        'source_dispatch_artifact_path': str(source_dispatch) if source_dispatch else None,
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
