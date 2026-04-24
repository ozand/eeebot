from __future__ import annotations

import json
from pathlib import Path

from scripts import consume_no_live_executor_gaps as controller

REFERENCE_NOW = '2026-04-17T12:00:00Z'


def _active_projects() -> dict[str, object]:
    return {
        'updated_at': '2026-04-16T00:00:00Z',
        'projects': [
            {
                'id': 'project-nanobot-eeepc-owner-loop',
                'status': 'in_progress',
                'goal': 'Drive Nanobot eeepc to a stable autonomous owner/control loop and out of quality-blocker stagnation.',
                'current_stage': 'live autonomy control-chain hardening',
            },
            {
                'id': 'project-dashboard-operator-console',
                'status': 'pending',
                'goal': 'Continue improving the Nanobot ops dashboard as an operator console.',
                'current_stage': 'waiting behind higher-priority autonomy work',
            },
        ],
    }


def _active_execution_without_live_task() -> dict[str, object]:
    return {
        'updated_at': REFERENCE_NOW,
        'source_queue_path': '/tmp/execution_queue.json',
        'summary': {
            'total': 1,
            'active': 0,
            'queued': 0,
            'in_progress': 0,
            'waiting_for_dispatch': 0,
            'needs_redispatch': 0,
            'blocked': 0,
            'completed': 1,
            'live_execution_tasks': 0,
            'stale_execution_detected': False,
            'stale_execution_incidents': 0,
        },
        'has_actually_executing_task': False,
        'live_task': None,
        'active_tasks': [],
        'terminal_tasks': [
            {
                'task_key': 'completed-task',
                'queue_status': 'completed',
                'execution_state': 'completed',
            }
        ],
    }


def _queue_without_live_work() -> dict[str, object]:
    return {
        'tasks': [
            {
                'created_at': '2026-04-16T07:30:50.705406Z',
                'status': 'completed',
                'source': 'hermes-autonomy-controller',
                'diagnosis': 'stagnating_on_quality_blocker',
                'severity': 'critical',
                'active_goal': 'goal-44e50921129bf475',
                'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json',
                'failure_class': 'no_concrete_change',
                'remediation_class': 'planner_hardening',
            }
        ]
    }


def _active_execution_with_waiting_work() -> dict[str, object]:
    payload = _active_execution_without_live_task()
    payload['summary'] = dict(payload['summary'])
    payload['summary']['active'] = 1
    payload['summary']['waiting_for_dispatch'] = 1
    payload['active_tasks'] = [
        {
            'task_key': 'queued-task',
            'queue_status': 'requested_execution',
            'execution_state': 'waiting_for_dispatch',
        }
    ]
    return payload


def test_consume_no_live_executor_gap_marks_projects_waiting_for_dispatch(tmp_path: Path) -> None:
    active_projects_path = tmp_path / 'active_projects.json'
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    incident_dir = tmp_path / 'no_live_executor_incidents'
    latest_incident_path = tmp_path / 'no_live_executor_incident.json'

    active_projects_path.write_text(json.dumps(_active_projects(), indent=2), encoding='utf-8')
    active_execution_path.write_text(json.dumps(_active_execution_without_live_task(), indent=2), encoding='utf-8')
    queue_path.write_text(json.dumps(_queue_without_live_work(), indent=2), encoding='utf-8')

    result = controller.consume_no_live_executor_gap(
        active_projects_path=active_projects_path,
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        incident_dir=incident_dir,
        latest_incident_path=latest_incident_path,
        now=REFERENCE_NOW,
    )

    assert result['consumed'] is True
    assert result['incident_type'] == 'no_live_executor_gap'
    assert result['project_status_action'] == 'waiting_for_dispatch'
    assert result['affected_project_ids'] == ['project-nanobot-eeepc-owner-loop']
    assert Path(result['incident_path']).exists()

    active_projects_after = json.loads(active_projects_path.read_text(encoding='utf-8'))
    updated_project = active_projects_after['projects'][0]
    assert updated_project['status'] == 'waiting_for_dispatch'
    assert updated_project['current_stage'] == 'waiting for bounded execution dispatch'
    assert updated_project['dispatch_gap_detected_at'] == REFERENCE_NOW
    assert updated_project['dispatch_gap_detected_by'] == controller.SCRIPT_NAME
    assert updated_project['dispatch_gap_incident_path'] == result['incident_path']
    assert 'no live executor' in updated_project['dispatch_gap_reason'].lower()

    incident_payload = json.loads(Path(result['incident_path']).read_text(encoding='utf-8'))
    assert incident_payload['incident_type'] == 'no_live_executor_gap'
    assert incident_payload['active_project_ids'] == ['project-nanobot-eeepc-owner-loop']
    assert incident_payload['next_action_summary'] == result['next_action_summary']

    latest_payload = json.loads(latest_incident_path.read_text(encoding='utf-8'))
    assert latest_payload['incident_artifact_path'] == result['incident_path']


def test_consume_no_live_executor_gap_is_noop_when_waiting_work_already_exists(tmp_path: Path) -> None:
    active_projects_path = tmp_path / 'active_projects.json'
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    incident_dir = tmp_path / 'no_live_executor_incidents'
    latest_incident_path = tmp_path / 'no_live_executor_incident.json'

    active_projects_path.write_text(json.dumps(_active_projects(), indent=2), encoding='utf-8')
    active_execution_path.write_text(json.dumps(_active_execution_with_waiting_work(), indent=2), encoding='utf-8')
    queue_path.write_text(json.dumps(_queue_without_live_work(), indent=2), encoding='utf-8')

    result = controller.consume_no_live_executor_gap(
        active_projects_path=active_projects_path,
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        incident_dir=incident_dir,
        latest_incident_path=latest_incident_path,
        now=REFERENCE_NOW,
    )

    assert result['consumed'] is False
    assert result['reason'] == 'bounded_work_already_exists'
    assert not incident_dir.exists()


def test_consume_no_live_executor_gap_is_idempotent_after_status_transition(tmp_path: Path) -> None:
    active_projects_path = tmp_path / 'active_projects.json'
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    incident_dir = tmp_path / 'no_live_executor_incidents'
    latest_incident_path = tmp_path / 'no_live_executor_incident.json'

    active_projects_path.write_text(json.dumps(_active_projects(), indent=2), encoding='utf-8')
    active_execution_path.write_text(json.dumps(_active_execution_without_live_task(), indent=2), encoding='utf-8')
    queue_path.write_text(json.dumps(_queue_without_live_work(), indent=2), encoding='utf-8')

    first = controller.consume_no_live_executor_gap(
        active_projects_path=active_projects_path,
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        incident_dir=incident_dir,
        latest_incident_path=latest_incident_path,
        now=REFERENCE_NOW,
    )
    second = controller.consume_no_live_executor_gap(
        active_projects_path=active_projects_path,
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        incident_dir=incident_dir,
        latest_incident_path=latest_incident_path,
        now=REFERENCE_NOW,
    )

    assert first['consumed'] is True
    assert second['consumed'] is False
    assert second['reason'] == 'no_in_progress_project'
