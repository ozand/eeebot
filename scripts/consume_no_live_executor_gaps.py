#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
ACTIVE_PROJECTS_PATH = ROOT / 'control' / 'active_projects.json'
ACTIVE_EXECUTION_PATH = ROOT / 'control' / 'active_execution.json'
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
INCIDENT_DIR = ROOT / 'control' / 'no_live_executor_incidents'
LATEST_INCIDENT_PATH = ROOT / 'control' / 'no_live_executor_incident.json'
SCRIPT_NAME = 'consume_no_live_executor_gaps.py'
WAITING_STATUS = 'waiting_for_dispatch'
WAITING_STAGE = 'waiting for bounded execution dispatch'
GAP_REASON = (
    'Active project exists but there is no live executor and no queued, in-progress, or waiting '
    'bounded execution slice. Treat this as a control-gap incident, not a neutral status.'
)
NEXT_ACTION_SUMMARY = (
    'Create or enqueue the next bounded execution slice immediately; until a bounded slice exists, '
    'keep the project in waiting_for_dispatch instead of claiming in_progress.'
)


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
    return slug or 'gap'


def timestamp_slug(timestamp: str) -> str:
    return timestamp.replace('-', '').replace(':', '').replace('.', '')


def queue_tasks(queue: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(queue, dict):
        return []
    maybe_tasks = queue.get('tasks')
    if not isinstance(maybe_tasks, list):
        return []
    return [task for task in maybe_tasks if isinstance(task, dict)]


def project_items(active_projects: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(active_projects, dict):
        return []
    maybe_projects = active_projects.get('projects')
    if not isinstance(maybe_projects, list):
        return []
    return [project for project in maybe_projects if isinstance(project, dict)]


def has_bounded_work(active_execution: dict[str, Any] | None) -> bool:
    if not isinstance(active_execution, dict):
        return False
    if bool(active_execution.get('has_actually_executing_task')):
        return True
    summary = active_execution.get('summary')
    if not isinstance(summary, dict):
        return False
    for key in ('queued', 'in_progress', 'waiting_for_dispatch', 'needs_redispatch'):
        value = summary.get(key, 0)
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int) and value > 0:
            return True
    return False


def in_progress_projects(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [project for project in projects if project.get('status') == 'in_progress']


def incident_path(incident_dir: Path, incident_created_at: str, affected_projects: list[dict[str, Any]]) -> Path:
    project_slug = slugify('-'.join(str(project.get('id') or 'project') for project in affected_projects))
    return incident_dir / f'{timestamp_slug(incident_created_at)}-{project_slug}.json'


def update_projects_for_gap(
    projects: list[dict[str, Any]],
    *,
    incident_created_at: str,
    incident_path_value: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    updated_projects: list[dict[str, Any]] = []
    affected_project_ids: list[str] = []
    for project in projects:
        updated_project = deepcopy(project)
        if updated_project.get('status') == 'in_progress':
            affected_project_ids.append(str(updated_project.get('id')))
            updated_project['previous_status'] = updated_project.get('status')
            updated_project['previous_current_stage'] = updated_project.get('current_stage')
            updated_project['status'] = WAITING_STATUS
            updated_project['current_stage'] = WAITING_STAGE
            updated_project['dispatch_gap_detected_at'] = incident_created_at
            updated_project['dispatch_gap_detected_by'] = SCRIPT_NAME
            updated_project['dispatch_gap_reason'] = GAP_REASON
            updated_project['dispatch_gap_incident_path'] = incident_path_value
            updated_project['dispatch_gap_next_action_summary'] = NEXT_ACTION_SUMMARY
        updated_projects.append(updated_project)
    return updated_projects, affected_project_ids


def build_incident_payload(
    *,
    incident_created_at: str,
    incident_path_value: str,
    active_projects: dict[str, Any],
    active_execution: dict[str, Any],
    queue: dict[str, Any],
    affected_project_ids: list[str],
) -> dict[str, Any]:
    return {
        'incident_created_at': incident_created_at,
        'incident_created_by': SCRIPT_NAME,
        'incident_type': 'no_live_executor_gap',
        'incident_state': WAITING_STATUS,
        'gap_reason': GAP_REASON,
        'next_action_summary': NEXT_ACTION_SUMMARY,
        'incident_artifact_path': incident_path_value,
        'source_active_projects_path': str(ACTIVE_PROJECTS_PATH),
        'source_active_execution_path': str(ACTIVE_EXECUTION_PATH),
        'source_queue_path': str(QUEUE_PATH),
        'active_project_ids': affected_project_ids,
        'active_projects_snapshot_before': deepcopy(active_projects),
        'active_execution_snapshot': deepcopy(active_execution),
        'queue_snapshot': deepcopy(queue),
    }


def consume_no_live_executor_gap(
    *,
    active_projects_path: Path = ACTIVE_PROJECTS_PATH,
    active_execution_path: Path = ACTIVE_EXECUTION_PATH,
    queue_path: Path = QUEUE_PATH,
    incident_dir: Path = INCIDENT_DIR,
    latest_incident_path: Path = LATEST_INCIDENT_PATH,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    active_projects = load_json(active_projects_path, {'projects': []})
    active_execution = load_json(active_execution_path, {'summary': {}})
    queue = load_json(queue_path, {'tasks': []})

    projects = project_items(active_projects)
    active_projects_in_progress = in_progress_projects(projects)
    if not active_projects_in_progress:
        return {'consumed': False, 'reason': 'no_in_progress_project'}

    if has_bounded_work(active_execution):
        return {'consumed': False, 'reason': 'bounded_work_already_exists'}

    incident_created_at = now_utc() if now is None else (now.isoformat().replace('+00:00', 'Z') if isinstance(now, datetime) else str(now))
    artifact_path = incident_path(incident_dir, incident_created_at, active_projects_in_progress)
    updated_projects, affected_project_ids = update_projects_for_gap(
        projects,
        incident_created_at=incident_created_at,
        incident_path_value=str(artifact_path),
    )

    updated_active_projects = deepcopy(active_projects) if isinstance(active_projects, dict) else {}
    updated_active_projects['projects'] = updated_projects
    updated_active_projects['updated_at'] = incident_created_at
    updated_active_projects['no_live_executor_incident_path'] = str(artifact_path)
    updated_active_projects['no_live_executor_incident_created_at'] = incident_created_at
    updated_active_projects['no_live_executor_incident_created_by'] = SCRIPT_NAME

    incident_payload = build_incident_payload(
        incident_created_at=incident_created_at,
        incident_path_value=str(artifact_path),
        active_projects=active_projects,
        active_execution=active_execution,
        queue=queue,
        affected_project_ids=affected_project_ids,
    )

    atomic_write_json(artifact_path, incident_payload)
    atomic_write_json(latest_incident_path, incident_payload)
    atomic_write_json(active_projects_path, updated_active_projects)

    return {
        'consumed': True,
        'incident_type': 'no_live_executor_gap',
        'project_status_action': WAITING_STATUS,
        'incident_path': str(artifact_path),
        'affected_project_ids': affected_project_ids,
        'next_action_summary': NEXT_ACTION_SUMMARY,
        'queue_task_count': len(queue_tasks(queue)),
    }


def main() -> None:
    print(json.dumps(consume_no_live_executor_gap(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
