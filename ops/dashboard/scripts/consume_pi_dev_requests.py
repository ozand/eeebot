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
PI_DEV_REQUEST_DIR = ROOT / 'control' / 'pi_dev_requests'
SCRIPT_NAME = 'consume_pi_dev_requests.py'
REQUESTED_EXECUTOR = 'pi_dev'
REQUEST_STATUS = 'requested'
HANDOFF_ELIGIBLE_STATUSES = {'handed_off'}


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
    for key in ('queue_task_key', 'dedupe_key', 'active_goal', 'report_source', 'diagnosis', 'failure_class', 'remediation_class'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return slugify(value)
    return 'task'


def timestamp_slug(timestamp: str) -> str:
    return timestamp.replace('-', '').replace(':', '').replace('.', '')


def source_execution_request_path_value(handoff: dict[str, Any]) -> str | None:
    value = handoff.get('source_execution_request_path')
    if isinstance(value, str) and value.strip():
        return value
    value = handoff.get('execution_request_path')
    if isinstance(value, str) and value.strip():
        return value
    return None


def matching_queue_task(tasks: list[Any], handoff: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None]:
    request_path = source_execution_request_path_value(handoff)
    if not request_path:
        return None, None

    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        candidate_paths = []
        for key in ('execution_request_path', 'source_execution_request_path'):
            value = task.get(key)
            if isinstance(value, str) and value.strip():
                candidate_paths.append(value)
        if request_path in candidate_paths:
            return index, task
    return None, None


def handoff_lifecycle_status(handoff: dict[str, Any]) -> str | None:
    value = handoff.get('status')
    if isinstance(value, str) and value.strip():
        return value
    value = handoff.get('handoff_status')
    if isinstance(value, str) and value.strip():
        return value
    return None


def build_pi_dev_request_payload(
    handoff: dict[str, Any],
    handoff_path: Path,
    request_created_at: str,
    source_request_path: str | None,
    queue_task: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'created_at': request_created_at,
        'created_by': SCRIPT_NAME,
        'status': REQUEST_STATUS,
        'requested_executor': REQUESTED_EXECUTOR,
        'active_goal': handoff.get('active_goal'),
        'diagnosis': handoff.get('diagnosis'),
        'failure_class': handoff.get('failure_class'),
        'remediation_class': handoff.get('remediation_class'),
        'recommended_remediation_action': handoff.get('recommended_remediation_action'),
        'source_executor_handoff_path': str(handoff_path),
        'source_execution_request_path': source_request_path,
        'source_executor_handoff_status_before_request': handoff_lifecycle_status(handoff),
        'source_executor_handoff': handoff,
    }
    if queue_task is not None:
        payload['source_queue_task'] = queue_task
        payload['source_queue_task_status_before_request'] = queue_task.get('status')
        payload['queue_task_key'] = task_key(queue_task)
        payload['queue_path'] = str(QUEUE_PATH)
    return payload


def main() -> None:
    handoff_files = sorted(HANDOFF_DIR.glob('*.json'), key=lambda path: path.name)
    if not handoff_files:
        print(json.dumps({'consumed': False, 'reason': 'no_executor_handoff'}, ensure_ascii=False))
        return

    queue = load_json(QUEUE_PATH, {'tasks': []})
    tasks = queue.get('tasks') if isinstance(queue, dict) else []
    if not isinstance(tasks, list):
        tasks = []

    for handoff_path in handoff_files:
        handoff = load_json(handoff_path, None)
        if not isinstance(handoff, dict):
            continue

        handoff_status = handoff_lifecycle_status(handoff)
        if handoff_status not in HANDOFF_ELIGIBLE_STATUSES:
            continue
        if handoff.get('pi_dev_requested_at') or handoff.get('pi_dev_request_path') or handoff.get('pi_dev_request_status'):
            continue

        matched_index, matched_task = matching_queue_task(tasks, handoff)
        if matched_task is not None and matched_task.get('pi_dev_request_path'):
            continue

        requested_at = now_utc()
        request_path = PI_DEV_REQUEST_DIR / f"{timestamp_slug(requested_at)}-{task_key(handoff)}.json"
        request_payload = build_pi_dev_request_payload(
            handoff,
            handoff_path,
            requested_at,
            source_execution_request_path_value(handoff),
            matched_task,
        )
        atomic_write_json(request_path, request_payload)

        updated_handoff = dict(handoff)
        updated_handoff['status'] = 'pi_dev_requested'
        updated_handoff['pi_dev_requested_at'] = requested_at
        updated_handoff['pi_dev_request_path'] = str(request_path)
        updated_handoff['pi_dev_request_status'] = REQUEST_STATUS
        updated_handoff['pi_dev_request_created_by'] = SCRIPT_NAME
        atomic_write_json(handoff_path, updated_handoff)

        if source_execution_request_path_value(handoff):
            source_request_path = Path(source_execution_request_path_value(handoff))
            if source_request_path.exists():
                source_request = load_json(source_request_path, None)
                if isinstance(source_request, dict):
                    updated_request = dict(source_request)
                    updated_request['status'] = 'pi_dev_requested'
                    updated_request['pi_dev_requested_at'] = requested_at
                    updated_request['pi_dev_request_path'] = str(request_path)
                    updated_request['pi_dev_request_status'] = REQUEST_STATUS
                    updated_request['pi_dev_request_created_by'] = SCRIPT_NAME
                    atomic_write_json(source_request_path, updated_request)

        if matched_index is not None and matched_task is not None:
            updated_task = dict(matched_task)
            updated_task['pi_dev_requested_at'] = requested_at
            updated_task['pi_dev_request_path'] = str(request_path)
            updated_task['pi_dev_request_status'] = REQUEST_STATUS
            updated_task['pi_dev_request_created_by'] = SCRIPT_NAME
            tasks[matched_index] = updated_task
            atomic_write_json(QUEUE_PATH, {'tasks': tasks})
        elif queue:
            atomic_write_json(QUEUE_PATH, {'tasks': tasks})

        output = {
            'consumed': True,
            'status': 'pi_dev_requested',
            'requested_executor': REQUESTED_EXECUTOR,
            'executor_handoff_path': str(handoff_path),
            'pi_dev_request_path': str(request_path),
            'pi_dev_requested_at': requested_at,
            'task_key': task_key(matched_task) if isinstance(matched_task, dict) else task_key(handoff),
            'queue_task_index': matched_index,
            'queue_task_status_before_request': matched_task.get('status') if isinstance(matched_task, dict) else None,
        }
        print(json.dumps(output, ensure_ascii=False))
        return

    print(json.dumps({'consumed': False, 'reason': 'no_eligible_executor_handoff'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
