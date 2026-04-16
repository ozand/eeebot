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
LATEST_DISPATCH_PATH = ROOT / 'control' / 'execution_dispatch.json'
SCRIPT_NAME = 'consume_execution_queue.py'

NON_DISPATCHABLE_STATUSES = {'in_progress', 'requested_execution', 'handed_off', 'completed', 'cancelled'}


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


def write_dispatch_artifacts(task: dict[str, Any], dispatched_at: str) -> dict[str, str]:
    artifact_stamp = dispatched_at.replace('-', '').replace(':', '').replace('.', '')
    artifact_name = f"{artifact_stamp}-{task_key(task)}.json"
    artifact_path = DISPATCH_DIR / artifact_name
    payload = {
        'dispatched_at': dispatched_at,
        'dispatched_by': SCRIPT_NAME,
        'queue_path': str(QUEUE_PATH),
        'task': task,
    }
    atomic_write_json(artifact_path, payload)
    atomic_write_json(LATEST_DISPATCH_PATH, payload)
    return {
        'dispatch_artifact': str(artifact_path),
        'latest_dispatch_pointer': str(LATEST_DISPATCH_PATH),
    }


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
    if status == 'queued':
        dispatched_at = now_utc()
        updated_task = dict(first_task)
        updated_task['status'] = 'in_progress'
        updated_task['dispatched_at'] = dispatched_at
        updated_task['dispatch_state'] = 'dispatched'
        updated_task['dispatched_by'] = SCRIPT_NAME
        tasks[0] = updated_task
        atomic_write_json(QUEUE_PATH, {'tasks': tasks})
        artifact_paths = write_dispatch_artifacts(updated_task, dispatched_at)
        output = {
            'consumed': True,
            'status': 'in_progress',
            'task_index': 0,
            'task_key': task_key(updated_task),
            'dispatched_at': dispatched_at,
            **artifact_paths,
        }
        print(json.dumps(output, ensure_ascii=False))
        return

    if status in NON_DISPATCHABLE_STATUSES:
        print(
            json.dumps(
                {'consumed': False, 'reason': f'first_task_already_{status}', 'task_index': 0, 'task_status': status},
                ensure_ascii=False,
            )
        )
        return

    print(json.dumps({'consumed': False, 'reason': 'first_task_not_queued', 'task_index': 0, 'task_status': status}, ensure_ascii=False))


if __name__ == '__main__':
    main()
