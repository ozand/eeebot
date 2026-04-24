from __future__ import annotations

import json
from pathlib import Path

from scripts import consume_execution_queue as consume
from scripts import enqueue_active_remediation as enqueue
from scripts import build_status_snapshot as snapshot

REFERENCE_NOW = '2026-04-21T10:10:00Z'


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding='utf-8')


def _waiting_project() -> dict[str, object]:
    return {
        'id': 'project-nanobot-eeepc-owner-loop',
        'status': 'waiting_for_dispatch',
        'goal': 'Drive Nanobot eeepc to a stable autonomous owner/control loop and out of quality-blocker stagnation.',
        'current_stage': 'waiting for bounded execution dispatch',
        'previous_status': 'in_progress',
        'previous_current_stage': 'live autonomy control-chain hardening',
        'dispatch_gap_detected_at': REFERENCE_NOW,
        'dispatch_gap_detected_by': 'consume_no_live_executor_gaps.py',
        'dispatch_gap_reason': 'Active project exists but there is no live executor and no queued, in-progress, or waiting bounded execution slice.',
        'dispatch_gap_incident_path': '/tmp/no_live_executor_incident.json',
        'dispatch_gap_next_action_summary': 'Create or enqueue the next bounded execution slice immediately.',
    }


def _empty_active_execution() -> dict[str, object]:
    return {
        'updated_at': REFERENCE_NOW,
        'source_queue_path': '/tmp/execution_queue.json',
        'summary': {
            'total': 0,
            'active': 0,
            'queued': 0,
            'in_progress': 0,
            'waiting_for_dispatch': 0,
            'needs_redispatch': 0,
            'blocked': 0,
            'completed': 0,
            'live_execution_tasks': 0,
            'stale_execution_detected': False,
            'stale_execution_incidents': 0,
        },
        'has_actually_executing_task': False,
        'live_task': None,
        'stale_execution_detected': False,
        'stale_execution_threshold_minutes': 30,
        'stale_execution_task': None,
        'stale_execution_incident_task': None,
        'active_tasks': [],
        'terminal_tasks': [],
    }


def test_restore_project_owner_loop_enqueues_and_dispatches_live_slice(tmp_path: Path, monkeypatch) -> None:
    active_projects_path = tmp_path / 'control' / 'active_projects.json'
    active_execution_path = tmp_path / 'control' / 'active_execution.json'
    queue_path = tmp_path / 'control' / 'execution_queue.json'
    dispatch_dir = tmp_path / 'control' / 'dispatched'
    latest_dispatch_path = tmp_path / 'control' / 'execution_dispatch.json'
    no_live_incident_path = tmp_path / 'control' / 'no_live_executor_incident.json'

    _write_json(active_projects_path, {'updated_at': REFERENCE_NOW, 'projects': [_waiting_project()]})
    _write_json(active_execution_path, _empty_active_execution())
    _write_json(queue_path, {'tasks': []})
    _write_json(no_live_incident_path, {'incident_created_at': REFERENCE_NOW})

    monkeypatch.setattr(enqueue, 'ACTIVE_PROJECTS_PATH', active_projects_path)
    monkeypatch.setattr(enqueue, 'ACTIVE_EXECUTION_PATH', active_execution_path)
    monkeypatch.setattr(enqueue, 'NO_LIVE_INCIDENT_PATH', no_live_incident_path)
    monkeypatch.setattr(enqueue, 'QUEUE_PATH', queue_path)

    task = enqueue.build_project_restore_task(_waiting_project())
    assert task['project_id'] == 'project-nanobot-eeepc-owner-loop'
    enqueue_result = enqueue.enqueue_task(task)
    assert enqueue_result['enqueued'] is True

    monkeypatch.setattr(consume, 'QUEUE_PATH', queue_path)
    monkeypatch.setattr(consume, 'ACTIVE_PROJECTS_PATH', active_projects_path)
    monkeypatch.setattr(consume, 'ACTIVE_EXECUTION_PATH', active_execution_path)
    monkeypatch.setattr(consume, 'DISPATCH_DIR', dispatch_dir)
    monkeypatch.setattr(consume, 'LATEST_DISPATCH_PATH', latest_dispatch_path)
    monkeypatch.setattr(snapshot, 'ACTIVE_PROJECTS', active_projects_path)
    monkeypatch.setattr(snapshot, 'ACTIVE_EXECUTION', active_execution_path)
    monkeypatch.setattr(snapshot, 'QUEUE', queue_path)

    consume.main()

    queue_after = json.loads(queue_path.read_text(encoding='utf-8'))
    task_after = queue_after['tasks'][0]
    assert task_after['status'] == 'in_progress'
    assert task_after['execution_state'] == 'in_progress'
    assert task_after['project_id'] == 'project-nanobot-eeepc-owner-loop'

    active_projects_after = json.loads(active_projects_path.read_text(encoding='utf-8'))
    project_after = active_projects_after['projects'][0]
    assert project_after['status'] == 'in_progress'
    assert project_after['current_stage'] == 'live bounded execution active'
    assert project_after['owner_loop_reactivated_by'] == 'consume_execution_queue.py'
    assert project_after['live_execution_task_key']

    active_execution_after = json.loads(active_execution_path.read_text(encoding='utf-8'))
    assert active_execution_after['has_actually_executing_task'] is True
    assert active_execution_after['live_task'] is not None
    assert active_execution_after['summary']['in_progress'] == 1
