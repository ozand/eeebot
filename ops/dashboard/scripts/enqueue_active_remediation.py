#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
ANALYZER = ROOT / 'scripts' / 'analyze_active_remediation.py'
ACTIVE_PROJECTS_PATH = ROOT / 'control' / 'active_projects.json'
ACTIVE_EXECUTION_PATH = ROOT / 'control' / 'active_execution.json'
NO_LIVE_INCIDENT_PATH = ROOT / 'control' / 'no_live_executor_incident.json'
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
ACTIVE_STATUSES = {'queued', 'in_progress', 'requested_execution', 'handed_off'}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_json(path: Path, default: Any):
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


def run_analyzer() -> dict[str, Any]:
    proc = subprocess.run(['python3', str(ANALYZER)], capture_output=True, text=True, check=True, timeout=60)
    parsed = json.loads(proc.stdout)
    return parsed if isinstance(parsed, dict) else {}


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


def waiting_projects(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [project for project in projects if project.get('status') == 'waiting_for_dispatch']


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


def slugify(value: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', value.strip())
    slug = re.sub(r'-{2,}', '-', slug).strip('-._')
    return slug or 'task'


def task_key(task: dict[str, Any]) -> str:
    for key in ('dedupe_key', 'project_id', 'active_goal', 'report_source', 'diagnosis', 'failure_class', 'remediation_class'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return slugify(value)
    return 'task'


def build_project_restore_task(project: dict[str, Any]) -> dict[str, Any]:
    project_id = str(project.get('id') or 'project')
    incident_path = project.get('dispatch_gap_incident_path') or str(NO_LIVE_INCIDENT_PATH)
    project_goal = str(project.get('goal') or project_id)
    project_stage = project.get('current_stage')
    created_at = now_utc()
    remediation = {
        'created_at': created_at,
        'status': 'queued',
        'source': 'dashboard-control-restoration',
        'diagnosis': 'no_live_executor_gap_recovery',
        'severity': 'critical',
        'project_id': project_id,
        'active_goal': project_goal,
        'project_previous_status': project.get('status'),
        'project_previous_current_stage': project_stage,
        'report_source': incident_path,
        'failure_class': 'no_live_executor_gap',
        'remediation_class': 'owner_loop_reactivation',
        'recommended_remediation_action': (
            'Create the next truthful bounded execution slice, dispatch it, and promote the project owner loop back to in_progress.'
        ),
        'blocked_next_step': 'Do not mark the project in_progress until a live execution assignment exists.',
        'operator_summary': f'{project_id} is waiting_for_dispatch with no live executor; restore the owner loop with one bounded execution slice.',
        'dedupe_key': f'{project_id}|{incident_path}|no_live_executor_gap|owner_loop_reactivation',
        'current_stage_before_restore': project_stage,
    }
    return remediation


def build_analyzer_task(analysis: dict[str, Any]) -> dict[str, Any]:
    diagnosis = analysis.get('diagnosis')
    active_goal = analysis.get('active_goal')
    if isinstance(active_goal, dict):
        active_goal_value = active_goal.get('goal_id') or active_goal.get('text')
    else:
        active_goal_value = active_goal

    remediation = {
        'created_at': now_utc(),
        'status': 'queued',
        'source': 'hermes-autonomy-controller',
        'diagnosis': diagnosis,
        'severity': analysis.get('severity'),
        'active_goal': active_goal_value,
        'report_source': analysis.get('report_source'),
        'failure_class': analysis.get('failure_class'),
        'blocked_next_step': analysis.get('blocked_next_step'),
        'remediation_class': analysis.get('remediation_class'),
        'recommended_remediation_action': analysis.get('recommended_remediation_action'),
        'operator_summary': analysis.get('operator_summary'),
    }

    dedupe_key = '|'.join(
        [
            str(remediation.get('diagnosis') or ''),
            str(remediation.get('active_goal') or ''),
            str(remediation.get('report_source') or ''),
            str(remediation.get('failure_class') or ''),
            str(remediation.get('remediation_class') or ''),
        ]
    )
    remediation['dedupe_key'] = dedupe_key
    return remediation


def enqueue_task(task: dict[str, Any]) -> dict[str, Any]:
    queue = load_json(QUEUE_PATH, {'tasks': []})
    tasks = queue_tasks(queue)
    existing = next((t for t in tasks if t.get('dedupe_key') == task.get('dedupe_key') and t.get('status') in ACTIVE_STATUSES), None)
    if existing:
        return {'enqueued': False, 'reason': 'duplicate_existing_task', 'existing_task': existing, 'queue_size': len(tasks)}

    tasks.insert(0, task)
    atomic_write_json(QUEUE_PATH, {'tasks': tasks})
    return {'enqueued': True, 'task': task, 'queue_size': len(tasks)}


def main() -> None:
    active_projects = load_json(ACTIVE_PROJECTS_PATH, {'projects': []})
    active_execution = load_json(ACTIVE_EXECUTION_PATH, {'summary': {}})
    queue = load_json(QUEUE_PATH, {'tasks': []})

    projects = project_items(active_projects)
    waiting = waiting_projects(projects)
    if waiting and not has_bounded_work(active_execution):
        task = build_project_restore_task(waiting[0])
        output = enqueue_task(task)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    analysis = run_analyzer()
    diagnosis = analysis.get('diagnosis')
    if diagnosis != 'stagnating_on_quality_blocker':
        output = {
            'enqueued': False,
            'reason': 'diagnosis_not_actionable',
            'analysis': analysis,
            'queue_size': len(queue_tasks(queue)),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    task = build_analyzer_task(analysis)
    output = enqueue_task(task)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
