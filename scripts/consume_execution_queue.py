#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import scripts.build_status_snapshot as status_snapshot

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
ACTIVE_PROJECTS_PATH = ROOT / 'control' / 'active_projects.json'
ACTIVE_EXECUTION_PATH = ROOT / 'control' / 'active_execution.json'
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
    for key in ('dedupe_key', 'project_id', 'active_goal', 'report_source', 'diagnosis'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return slugify(value)
    return 'task'


def artifact_task_key(task: dict[str, Any]) -> str:
    key = task_key(task)
    if len(key) <= 96:
        return key
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]
    return f'{key[:72].rstrip("-._")}-{digest}'


def write_dispatch_artifacts(task: dict[str, Any], dispatched_at: str) -> dict[str, str]:
    artifact_stamp = dispatched_at.replace('-', '').replace(':', '').replace('.', '')
    artifact_name = f"{artifact_stamp}-{artifact_task_key(task)}.json"
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


def queue_tasks(queue: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(queue, dict):
        return []
    maybe_tasks = queue.get('tasks')
    if not isinstance(maybe_tasks, list):
        return []
    return [task for task in maybe_tasks if isinstance(task, dict)]


def promote_project(projects: list[dict[str, Any]], task: dict[str, Any], dispatched_at: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    project_id = task.get('project_id')
    if not isinstance(project_id, str) or not project_id.strip():
        return projects, None

    updated_projects: list[dict[str, Any]] = []
    promoted_project: dict[str, Any] | None = None
    for project in projects:
        updated_project = dict(project)
        if str(updated_project.get('id') or '') == project_id.strip():
            current_status = updated_project.get('status')
            current_stage = updated_project.get('current_stage')
            updated_project['previous_status'] = current_status
            updated_project['previous_current_stage'] = current_stage
            updated_project['status'] = 'in_progress'
            updated_project['current_stage'] = 'live bounded execution active'
            updated_project['owner_loop_reactivated_at'] = dispatched_at
            updated_project['owner_loop_reactivated_by'] = SCRIPT_NAME
            updated_project['owner_loop_reactivation_reason'] = task.get('recommended_remediation_action') or task.get('operator_summary')
            updated_project['owner_loop_reactivation_task_key'] = task_key(task)
            updated_project['live_execution_task_key'] = task_key(task)
            updated_project['live_execution_dispatch_artifact'] = task.get('execution_dispatch_path')
            updated_project['live_execution_started_at'] = dispatched_at
            promoted_project = updated_project
        updated_projects.append(updated_project)
    return updated_projects, promoted_project


def main() -> None:
    queue = load_json(QUEUE_PATH, {'tasks': []})
    tasks = queue_tasks(queue)
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
        updated_task['queue_status'] = 'in_progress'
        updated_task['execution_state'] = 'in_progress'
        updated_task['dispatched_at'] = dispatched_at
        updated_task['dispatch_state'] = 'dispatched'
        updated_task['dispatched_by'] = SCRIPT_NAME
        tasks[0] = updated_task
        atomic_write_json(QUEUE_PATH, {'tasks': tasks})

        active_execution = status_snapshot.build_active_execution({'tasks': tasks}, dispatched_at)

        active_projects = load_json(ACTIVE_PROJECTS_PATH, {'projects': []})
        project_items = active_projects.get('projects') if isinstance(active_projects, dict) else []
        if not isinstance(project_items, list):
            project_items = []
        updated_projects, promoted_project = promote_project(project_items, updated_task, dispatched_at)
        if promoted_project is not None and isinstance(active_projects, dict):
            updated_active_projects = dict(active_projects)
            updated_active_projects['projects'] = updated_projects
            updated_active_projects['updated_at'] = dispatched_at
            updated_active_projects['owner_loop_reactivated_at'] = dispatched_at
            updated_active_projects['owner_loop_reactivated_by'] = SCRIPT_NAME
            updated_active_projects['owner_loop_reactivated_project_id'] = promoted_project.get('id')
            atomic_write_json(ACTIVE_PROJECTS_PATH, updated_active_projects)

        artifact_paths = write_dispatch_artifacts(updated_task, dispatched_at)
        output = {
            'consumed': True,
            'status': 'in_progress',
            'task_index': 0,
            'task_key': task_key(updated_task),
            'dispatched_at': dispatched_at,
            'active_execution_summary': active_execution.get('summary'),
            'promoted_project_id': promoted_project.get('id') if isinstance(promoted_project, dict) else None,
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
