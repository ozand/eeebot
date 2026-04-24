#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
REQUEST_DIR = ROOT / 'control' / 'delegated_executor_requests'
DISPATCH_DIR = ROOT / 'control' / 'pi_dev_dispatches'
LATEST_REQUEST_PATH = ROOT / 'control' / 'delegated_executor_request.json'
SCRIPT_NAME = 'consume_delegated_executor_requests.py'
ELIGIBLE_STATUSES = {'pi_dev_dispatch_ready'}
REQUEST_STATUS = 'requested'
REQUESTED_EXECUTOR = 'hermes_subagent'
FALLBACK_REASON = 'Pi Dev invocation is blocked by a provider/model mismatch on this host; route the remediation slice through a Hermes/subagent executor path instead.'


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


def build_request_payload(
    task: dict[str, Any],
    request_path: Path,
    requested_at: str,
    source_dispatch: dict[str, Any],
    queue_task_index: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'created_at': requested_at,
        'created_by': SCRIPT_NAME,
        'status': REQUEST_STATUS,
        'requested_executor': REQUESTED_EXECUTOR,
        'delegated_executor_started_at': requested_at,
        'fallback_mode': 'pi_dev_provider_model_mismatch',
        'fallback_reason': FALLBACK_REASON,
        'queue_path': str(QUEUE_PATH),
        'queue_task_index': queue_task_index,
        'queue_task_key': task_key(task),
        'queue_task_status_before_request': task.get('status'),
        'source_pi_dev_dispatch_path': task.get('pi_dev_dispatch_path'),
        'source_pi_dev_dispatch_status_before_request': task.get('pi_dev_dispatch_status'),
        'source_pi_dev_request_path': task.get('pi_dev_request_path'),
        'source_pi_dev_bundle_path': task.get('pi_dev_bundle_path'),
        'source_queue_path': str(QUEUE_PATH),
        'source_queue_task': task,
        'source_dispatch_artifact': source_dispatch,
        'source_dispatch_prompt_path': task.get('pi_dev_dispatch_prompt_path'),
        'source_dispatch_script_path': task.get('pi_dev_dispatch_script_path'),
        'source_dispatch_runnable_command': source_dispatch.get('runnable_command') if isinstance(source_dispatch, dict) else None,
        'source_dispatch_command_path': source_dispatch.get('command_path') if isinstance(source_dispatch, dict) else None,
        'source_dispatch_prompt_text': source_dispatch.get('prompt_text') if isinstance(source_dispatch, dict) else None,
        'source_dispatch_status': source_dispatch.get('dispatch_status') if isinstance(source_dispatch, dict) else None,
        'source_dispatch_created_at': source_dispatch.get('dispatch_created_at') if isinstance(source_dispatch, dict) else None,
        'pi_dev_model_probe_error': '400 Invalid model name passed in model=coder-model from /chat/completions',
    }
    return payload


def main() -> None:
    queue = load_json(QUEUE_PATH, {'tasks': []})
    tasks = queue.get('tasks') if isinstance(queue, dict) else []
    if not isinstance(tasks, list) or not tasks:
        print(json.dumps({'consumed': False, 'reason': 'no_queue_task'}, ensure_ascii=False))
        return

    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        if task.get('status') not in ELIGIBLE_STATUSES:
            continue
        if task.get('delegated_executor_request_path') or task.get('delegated_executor_requested_at'):
            continue

        dispatch_path_value = task.get('pi_dev_dispatch_path')
        if not isinstance(dispatch_path_value, str) or not dispatch_path_value.strip():
            continue
        dispatch_path = Path(dispatch_path_value)
        if not dispatch_path.exists():
            continue

        source_dispatch = load_json(dispatch_path, {'dispatch_path': str(dispatch_path)})
        requested_at = now_utc()
        request_stamp = timestamp_slug(requested_at)
        request_path = REQUEST_DIR / f'{request_stamp}-{task_key(task)}.json'
        request_payload = build_request_payload(task, request_path, requested_at, source_dispatch, index)
        atomic_write_json(request_path, request_payload)
        atomic_write_json(LATEST_REQUEST_PATH, request_payload)

        updated_task = dict(task)
        updated_task['status'] = 'in_progress'
        updated_task['requested_executor'] = REQUESTED_EXECUTOR
        updated_task['delegated_executor_started_at'] = requested_at
        updated_task['delegated_executor_requested_at'] = requested_at
        updated_task['delegated_executor_request_path'] = str(request_path)
        updated_task['delegated_executor_request_status'] = REQUEST_STATUS
        updated_task['delegated_executor_request_created_by'] = SCRIPT_NAME
        updated_task['delegated_executor_request_mode'] = 'fallback_from_pi_dev_dispatch'
        updated_task['delegated_executor_request_reason'] = FALLBACK_REASON
        updated_task['delegated_executor_request_executor'] = REQUESTED_EXECUTOR
        tasks[index] = updated_task
        atomic_write_json(QUEUE_PATH, {'tasks': tasks})

        output = {
            'consumed': True,
            'status': 'in_progress',
            'fallback_mode': 'pi_dev_provider_model_mismatch',
            'queue_task_index': index,
            'queue_task_key': task_key(updated_task),
            'queue_task_status_before_request': task.get('status'),
            'requested_at': requested_at,
            'delegated_executor_request_path': str(request_path),
            'requested_executor': REQUESTED_EXECUTOR,
        }
        print(json.dumps(output, ensure_ascii=False))
        return

    print(json.dumps({'consumed': False, 'reason': 'no_eligible_pi_dev_dispatch_ready_task'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
