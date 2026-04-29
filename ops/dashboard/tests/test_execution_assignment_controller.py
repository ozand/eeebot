from __future__ import annotations

import json
from pathlib import Path

from scripts import consume_queued_redispatch_assignments as controller

REFERENCE_NOW = '2026-04-17T03:00:00Z'
STALE_STARTED_AT = '2026-04-16T11:40:49.015519Z'


def _redispatch_task() -> dict[str, object]:
    return {
        'created_at': '2026-04-16T07:30:50.705406Z',
        'status': 'queued',
        'source': 'hermes-autonomy-controller',
        'diagnosis': 'stagnating_on_quality_blocker',
        'severity': 'critical',
        'active_goal': 'goal-44e50921129bf475',
        'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json',
        'failure_class': 'no_concrete_change',
        'blocked_next_step': 'Rewrite the cycle around one file-level action or an explicit blocked next step.',
        'remediation_class': 'planner_hardening',
        'recommended_remediation_action': 'Tighten the next-cycle planner so it must emit exactly one file-level action plus one verification command and an explicit blocked-next-step fallback.',
        'operator_summary': 'stagnating_on_quality_blocker at severity critical: the active goal is stuck behind no_concrete_change with repeated BLOCK results.',
        'dedupe_key': 'stagnating_on_quality_blocker|goal-44e50921129bf475|/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json|no_concrete_change|planner_hardening',
        'dispatched_at': '2026-04-16T07:42:30.781890Z',
        'dispatch_state': 'queued',
        'dispatched_by': 'consume_execution_queue.py',
        'execution_requested_at': '2026-04-16T08:10:03.072371Z',
        'requested_executor': 'hermes_subagent',
        'execution_request_path': '/home/ozand/herkoot/Projects/nanobot-ops-dashboard/control/execution_requests/example.json',
        'executor_handoff_at': '2026-04-16T08:12:46.643769Z',
        'pi_dev_requested_at': '2026-04-16T09:52:41.738173Z',
        'pi_dev_bundled_at': '2026-04-16T10:13:40.324240Z',
        'pi_dev_dispatch_created_at': '2026-04-16T10:48:21.473591Z',
        'delegated_executor_started_at': STALE_STARTED_AT,
        'delegated_executor_requested_at': STALE_STARTED_AT,
        'delegated_executor_request_status': 'requested',
        'stale_execution_detected': True,
        'stale_execution_detected_at': '2026-04-17T02:10:48.663183Z',
        'stale_execution_incident_path': '/tmp/stale_execution_incident.json',
        'stale_execution_next_action_path': '/tmp/stale_execution_next_action.json',
        'stale_execution_redispatch_artifact_path': '/tmp/stale_execution_redispatch.json',
        'stale_execution_redispatch_created_at': '2026-04-17T02:25:53.513622Z',
        'stale_execution_redispatch_created_by': 'consume_stale_execution_next_actions.py',
        'stale_execution_redispatch_source_next_action_path': '/tmp/stale_execution_next_action.json',
        'stale_execution_redispatch_source_incident_path': '/tmp/stale_execution_incident.json',
        'stale_execution_redispatch_source_queue_path': '/tmp/execution_queue.json',
        'stale_execution_redispatch_source_task_index': 0,
        'stale_execution_redispatch_previous_status': 'stale_blocked',
        'stale_execution_redispatch_previous_execution_state': 'needs_redispatch',
        'stale_execution_redispatch_previous_queue_status': 'stale_blocked',
        'stale_execution_redispatch_previous_started_at': STALE_STARTED_AT,
        'stale_execution_redispatch_next_action_summary': 'Re-dispatch one bounded slice for the goal after preserving the stale incident evidence.',
        'stale_execution_redispatch_candidate': {
            'status': 'needs_redispatch',
            'execution_state': 'needs_redispatch',
            'task_key': 'stagnating_on_quality_blocker|goal-44e50921129bf475|/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json|no_concrete_change|planner_hardening',
            'active_goal': 'goal-44e50921129bf475',
            'diagnosis': 'stagnating_on_quality_blocker',
            'severity': 'critical',
            'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json',
            'failure_class': 'no_concrete_change',
            'remediation_class': 'planner_hardening',
            'recommended_remediation_action': 'Tighten the next-cycle planner so it must emit exactly one file-level action plus one verification command and an explicit blocked-next-step fallback.',
            'blocked_next_step': 'Rewrite the cycle around one file-level action or an explicit blocked next step.',
            'requested_executor': 'hermes_subagent',
            'reason': 'Redispatch is bounded to one fresh candidate only after the stale execution has been truthfully recorded.',
        },
        'stale_execution_detected_at': '2026-04-17T02:10:48.663183Z',
        'stale_execution_age_seconds': 52199.647579,
        'stale_execution_age': '14h29m59s',
        'stale_execution_threshold_minutes': 30,
        'stale_execution_recommended_next_action': 'Treat this as a stale-execution incident under the 30-minute investigation rule: check the live executor, confirm whether the task is still running, and either record the terminal result or re-dispatch the bounded slice.',
        'stale_execution_next_action_summary': 'Re-dispatch one bounded slice for goal-44e50921129bf475 after preserving the stale incident evidence.',
    }


def _queue_payload(task: dict[str, object]) -> dict[str, object]:
    return {'tasks': [dict(task)]}


def _live_active_execution() -> dict[str, object]:
    return {
        'updated_at': REFERENCE_NOW,
        'source_queue_path': '/tmp/execution_queue.json',
        'summary': {
            'total': 1,
            'active': 1,
            'queued': 0,
            'in_progress': 1,
            'waiting_for_dispatch': 0,
            'blocked': 0,
            'completed': 0,
            'live_execution_tasks': 1,
            'stale_execution_detected': True,
            'stale_execution_incidents': 1,
        },
        'has_actually_executing_task': True,
        'live_task': {'task_key': 'stagnating_on_quality_blocker|goal-44e50921129bf475|/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json|no_concrete_change|planner_hardening', 'task_index': 0, 'execution_state': 'in_progress'},
        'stale_execution_detected': True,
        'stale_execution_threshold_minutes': 30,
        'stale_execution_task': None,
        'stale_execution_incident_task': {'task_index': 0, 'task_key': 'stagnating_on_quality_blocker|goal-44e50921129bf475|/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json|no_concrete_change|planner_hardening', 'queue_status': 'in_progress', 'execution_state': 'in_progress'},
        'active_tasks': [],
        'terminal_tasks': [],
    }


def test_consume_queued_redispatch_assignment_restores_live_execution(tmp_path: Path) -> None:
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    assignment_dir = tmp_path / 'execution_assignments'
    latest_assignment_path = tmp_path / 'execution_assignment.json'
    task = _redispatch_task()

    queue_path.write_text(json.dumps(_queue_payload(task), indent=2), encoding='utf-8')
    snapshot_payload = _live_active_execution()
    active_execution_path.write_text(json.dumps(snapshot_payload, indent=2), encoding='utf-8')

    result = controller.consume_queued_redispatch_assignment(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        assignment_dir=assignment_dir,
        latest_assignment_path=latest_assignment_path,
    )

    assert result['consumed'] is True
    assert result['status'] == 'in_progress'
    assert result['execution_state'] == 'in_progress'
    assert result['has_live_delegated_execution'] is True
    assert result['assignment_path'].endswith('.json')
    assert result['source_incident_path'] == '/tmp/stale_execution_incident.json'
    assert result['source_next_action_path'] == '/tmp/stale_execution_next_action.json'
    assert result['source_redispatch_path'] == '/tmp/stale_execution_redispatch.json'

    queue_after = json.loads(queue_path.read_text(encoding='utf-8'))
    updated_task = queue_after['tasks'][0]
    assert updated_task['status'] == 'in_progress'
    assert updated_task['execution_state'] == 'in_progress'
    assert updated_task['queue_status'] == 'in_progress'
    assert updated_task['execution_assignment_path'] == result['assignment_path']
    assert updated_task['delegated_executor_requested_at'] == updated_task['delegated_executor_started_at']
    assert updated_task['stale_execution_redispatch_artifact_path'] == '/tmp/stale_execution_redispatch.json'
    assert updated_task['stale_execution_redispatch_source_incident_path'] == '/tmp/stale_execution_incident.json'

    assignment_payload = json.loads(Path(result['assignment_path']).read_text(encoding='utf-8'))
    assert assignment_payload['execution_assignment_type'] == 'queued_redispatch_execution_assignment'
    assert assignment_payload['execution_assignment_state'] == 'in_progress'
    assert assignment_payload['source_stale_execution_redispatch_path'] == '/tmp/stale_execution_redispatch.json'
    assert assignment_payload['assignment_artifact_path'] == result['assignment_path']

    latest_assignment = json.loads(latest_assignment_path.read_text(encoding='utf-8'))
    assert latest_assignment['assignment_artifact_path'] == result['assignment_path']

    refreshed = json.loads(active_execution_path.read_text(encoding='utf-8'))
    assert refreshed['has_actually_executing_task'] is True
    assert refreshed['live_task'] is not None


def test_consume_queued_redispatch_assignment_blocks_external_already_recorded_assignment(tmp_path: Path) -> None:
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    assignment_dir = tmp_path / 'execution_assignments'
    external_assignment_dir = tmp_path / 'external_repo' / 'execution_assignments'
    latest_assignment_path = tmp_path / 'execution_assignment.json'
    task = _redispatch_task()
    external_assignment_path = external_assignment_dir / '20260417T024647743230Z-stagnating_on_quality_blocker.json'
    external_assignment_dir.mkdir(parents=True, exist_ok=True)
    external_assignment_path.write_text(
        json.dumps(
            {
                'execution_assignment_created_at': REFERENCE_NOW,
                'execution_assignment_created_by': 'consume_queued_redispatch_assignments.py',
                'execution_assignment_type': 'queued_redispatch_execution_assignment',
                'execution_assignment_state': 'in_progress',
                'assignment_artifact_path': str(external_assignment_path),
                'task_key': controller.task_key(task),
            },
            indent=2,
        ),
        encoding='utf-8',
    )
    task['status'] = 'in_progress'
    task['execution_state'] = 'in_progress'
    task['execution_assignment_path'] = str(external_assignment_path)
    task['stale_execution_detected'] = True

    queue_path.write_text(json.dumps(_queue_payload(task), indent=2), encoding='utf-8')
    active_execution_path.write_text(json.dumps(_live_active_execution(), indent=2), encoding='utf-8')

    result = controller.consume_queued_redispatch_assignment(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        assignment_dir=assignment_dir,
        latest_assignment_path=latest_assignment_path,
    )

    assert result['consumed'] is False
    assert result['reason'] == 'external_assignment_path_not_canonical'
    assert result['status'] == 'stale_blocked'
    assert result['execution_state'] == 'needs_redispatch'
    assert result['has_live_delegated_execution'] is False
    assert result['assignment_path'] == str(external_assignment_path)

    queue_after = json.loads(queue_path.read_text(encoding='utf-8'))
    updated_task = queue_after['tasks'][0]
    assert updated_task['status'] == 'stale_blocked'
    assert updated_task['execution_state'] == 'needs_redispatch'
    assert updated_task['queue_status'] == 'stale_blocked'

    refreshed = json.loads(active_execution_path.read_text(encoding='utf-8'))
    assert refreshed['has_actually_executing_task'] is False
    assert refreshed['summary']['needs_redispatch'] == 1
    assert refreshed['stale_execution_incident_task'] is not None


def test_consume_queued_redispatch_assignment_replaces_external_queued_assignment_with_canonical_assignment(tmp_path: Path) -> None:
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    assignment_dir = tmp_path / 'execution_assignments'
    external_assignment_dir = tmp_path / 'external_repo' / 'execution_assignments'
    latest_assignment_path = tmp_path / 'execution_assignment.json'
    task = _redispatch_task()
    external_assignment_path = external_assignment_dir / '20260417T024647743230Z-stagnating_on_quality_blocker.json'
    external_assignment_dir.mkdir(parents=True, exist_ok=True)
    external_assignment_path.write_text(
        json.dumps(
            {
                'execution_assignment_created_at': REFERENCE_NOW,
                'execution_assignment_created_by': 'consume_queued_redispatch_assignments.py',
                'execution_assignment_type': 'queued_redispatch_execution_assignment',
                'execution_assignment_state': 'in_progress',
                'assignment_artifact_path': str(external_assignment_path),
                'task_key': controller.task_key(task),
            },
            indent=2,
        ),
        encoding='utf-8',
    )
    task['execution_assignment_path'] = str(external_assignment_path)

    queue_path.write_text(json.dumps(_queue_payload(task), indent=2), encoding='utf-8')
    active_execution_path.write_text(json.dumps(_live_active_execution(), indent=2), encoding='utf-8')

    result = controller.consume_queued_redispatch_assignment(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        assignment_dir=assignment_dir,
        latest_assignment_path=latest_assignment_path,
        now=REFERENCE_NOW,
    )

    assert result['consumed'] is True
    assert result['reason'] == 'assigned'
    assert result['status'] == 'in_progress'
    assert result['execution_state'] == 'in_progress'
    assert result['has_live_delegated_execution'] is True
    assert result['assignment_path'] != str(external_assignment_path)
    assert Path(result['assignment_path']).parent == assignment_dir

    queue_after = json.loads(queue_path.read_text(encoding='utf-8'))
    updated_task = queue_after['tasks'][0]
    assert updated_task['execution_assignment_path'] == result['assignment_path']
    assert updated_task['execution_assignment_path'] != str(external_assignment_path)



def test_consume_queued_redispatch_assignment_is_idempotent(tmp_path: Path) -> None:
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    assignment_dir = tmp_path / 'execution_assignments'
    latest_assignment_path = tmp_path / 'execution_assignment.json'
    task = _redispatch_task()

    queue_path.write_text(json.dumps(_queue_payload(task), indent=2), encoding='utf-8')
    active_execution_path.write_text(json.dumps(_live_active_execution(), indent=2), encoding='utf-8')

    first = controller.consume_queued_redispatch_assignment(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        assignment_dir=assignment_dir,
        latest_assignment_path=latest_assignment_path,
    )
    second = controller.consume_queued_redispatch_assignment(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        assignment_dir=assignment_dir,
        latest_assignment_path=latest_assignment_path,
    )

    assert first['consumed'] is True
    assert second['consumed'] is False
    assert second['reason'] == 'already_recorded'
    assert second['assignment_path'] == first['assignment_path']


