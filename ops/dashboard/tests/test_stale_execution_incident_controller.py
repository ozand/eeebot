from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from scripts import build_status_snapshot as snapshot
from scripts import consume_stale_execution_incidents as controller
from scripts import consume_stale_execution_next_actions as next_action_controller
from scripts import consume_queued_redispatch_assignments as redispatch_controller

REFERENCE_NOW = '2026-04-16T16:56:01Z'
STALE_STARTED_AT = '2026-04-16T11:40:49.015519Z'


def _stale_task() -> dict[str, object]:
    return {
        'created_at': '2026-04-16T07:30:50.705406Z',
        'status': 'in_progress',
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
        'dispatch_state': 'dispatched',
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
    }


def _queue_payload(task: dict[str, object]) -> dict[str, object]:
    queue_task = dict(task)
    queue_task.pop('operator_summary', None)
    return {'tasks': [queue_task]}


def _long_task_key() -> str:
    return (
        'stagnating_on_quality_blocker|goal-44e50921129bf475|'
        + 'x' * 220
        + '|no_concrete_change|planner_hardening'
    )


def test_consume_stale_execution_incident_marks_queue_and_writes_artifacts(tmp_path: Path, monkeypatch) -> None:
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    incident_dir = tmp_path / 'stale_execution_incidents'
    next_action_dir = tmp_path / 'stale_execution_next_actions'
    latest_incident_path = tmp_path / 'stale_execution_incident.json'
    latest_next_action_path = tmp_path / 'stale_execution_next_action.json'

    task = _stale_task()
    active_execution = {
        'updated_at': REFERENCE_NOW,
        'source_queue_path': str(queue_path),
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
        },
        'has_actually_executing_task': True,
        'live_task': {'task_key': task['dedupe_key'], 'task_index': 0, 'execution_state': 'in_progress'},
        'stale_execution_detected': True,
        'stale_execution_threshold_minutes': 30,
        'stale_execution_task': {
            'stale_detected': True,
            'threshold_minutes': 30,
            'threshold_seconds': 3600,
            'task_key': task['dedupe_key'],
            'executor': 'hermes_subagent',
            'started_at': STALE_STARTED_AT,
            'age_seconds': 51582.007993,
            'age': '14h19m42s',
            'recommended_next_action': 'Treat this as a stale-execution incident under the 30-minute investigation rule: check the live executor, confirm whether the task is still running, and either record the terminal result or re-dispatch the bounded slice.',
            'inspection_source': 'active_execution',
            'task_index': 0,
            'task_status': 'in_progress',
            'observed_in_progress_candidates': 1,
        },
        'active_tasks': [
            {
                'task_index': 0,
                'task_key': task['dedupe_key'],
                'queue_status': 'in_progress',
                'execution_state': 'in_progress',
                'is_live_execution': True,
                'is_blocked': False,
                'is_terminal': False,
                'source': 'hermes-autonomy-controller',
                'diagnosis': 'stagnating_on_quality_blocker',
                'severity': 'critical',
                'active_goal': 'goal-44e50921129bf475',
                'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json',
                'failure_class': 'no_concrete_change',
                'remediation_class': 'planner_hardening',
                'recommended_remediation_action': 'Tighten the next-cycle planner so it must emit exactly one file-level action plus one verification command and an explicit blocked-next-step fallback.',
                'blocked_next_step': 'Rewrite the cycle around one file-level action or an explicit blocked next step.',
                'requested_executor': 'hermes_subagent',
                'execution_request_path': '/home/ozand/herkoot/Projects/nanobot-ops-dashboard/control/execution_requests/example.json',
                'delegated_executor_started_at': STALE_STARTED_AT,
                'delegated_executor_requested_at': STALE_STARTED_AT,
                'delegated_executor_request_status': 'requested',
            }
        ],
        'terminal_tasks': [],
    }

    active_execution_path.write_text(json.dumps(active_execution, indent=2), encoding='utf-8')
    queue_path.write_text(json.dumps(_queue_payload(task), indent=2), encoding='utf-8')

    monkeypatch.setattr(controller, 'ACTIVE_EXECUTION_PATH', active_execution_path)
    monkeypatch.setattr(controller, 'QUEUE_PATH', queue_path)
    monkeypatch.setattr(controller, 'INCIDENT_DIR', incident_dir)
    monkeypatch.setattr(controller, 'NEXT_ACTION_DIR', next_action_dir)
    monkeypatch.setattr(controller, 'LATEST_INCIDENT_PATH', latest_incident_path)
    monkeypatch.setattr(controller, 'LATEST_NEXT_ACTION_PATH', latest_next_action_path)
    monkeypatch.setattr(snapshot, 'ACTIVE_EXECUTION', active_execution_path)
    monkeypatch.setattr(snapshot, 'QUEUE', queue_path)

    result = controller.consume_stale_execution_incident(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        incident_dir=incident_dir,
        next_action_dir=next_action_dir,
        latest_incident_path=latest_incident_path,
        latest_next_action_path=latest_next_action_path,
        now=REFERENCE_NOW,
    )

    assert result['consumed'] is True
    assert result['status'] == 'stale_blocked'
    assert result['execution_state'] == 'needs_redispatch'
    assert result['task_key'].startswith('stagnating_on_quality_blocker|goal-44e50921129bf475')
    assert result['incident_path'].endswith('.json')
    assert result['next_action_path'].endswith('.json')

    queue_after = json.loads(queue_path.read_text(encoding='utf-8'))
    updated_task = queue_after['tasks'][0]
    assert updated_task['status'] == 'stale_blocked'
    assert updated_task['execution_state'] == 'needs_redispatch'
    assert updated_task['stale_execution_detected'] is True
    assert updated_task['stale_execution_incident_path'] == result['incident_path']
    assert updated_task['stale_execution_next_action_path'] == result['next_action_path']

    incident_artifact = json.loads(Path(result['incident_path']).read_text(encoding='utf-8'))
    next_action_artifact = json.loads(Path(result['next_action_path']).read_text(encoding='utf-8'))
    assert incident_artifact['incident_type'] == 'stale_execution'
    assert incident_artifact['incident_state'] == 'stale_blocked'
    assert incident_artifact['bounded_redispatch_candidate']['status'] == 'needs_redispatch'
    assert 'Do not claim completion' in incident_artifact['next_action_summary']
    assert next_action_artifact['next_action_mode'] == 'needs_redispatch'
    assert next_action_artifact['bounded_redispatch_candidate']['execution_state'] == 'needs_redispatch'

    refreshed_registry = json.loads(active_execution_path.read_text(encoding='utf-8'))
    assert refreshed_registry['stale_execution_detected'] is True
    assert refreshed_registry['summary']['needs_redispatch'] == 1
    assert refreshed_registry['stale_execution_incident_task'] is not None
    assert refreshed_registry['stale_execution_incident_task']['queue_status'] == 'stale_blocked'
    assert refreshed_registry['stale_execution_incident_task']['execution_state'] == 'needs_redispatch'

    repeat_result = controller.consume_stale_execution_incident(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        incident_dir=incident_dir,
        next_action_dir=next_action_dir,
        latest_incident_path=latest_incident_path,
        latest_next_action_path=latest_next_action_path,
        now=REFERENCE_NOW,
    )
    assert repeat_result['consumed'] is False
    assert repeat_result['reason'] == 'already_recorded'


def test_build_status_snapshot_reports_needs_redispatch_state(tmp_path: Path, monkeypatch) -> None:
    active_execution_path = tmp_path / 'active_execution.json'
    queue = {
        'tasks': [
            {
                'task_index': 0,
                'task_key': 'stale-task',
                'status': 'stale_blocked',
                'execution_state': 'needs_redispatch',
                'is_live_execution': False,
                'is_blocked': True,
                'is_terminal': False,
                'source': 'hermes-autonomy-controller',
                'diagnosis': 'stagnating_on_quality_blocker',
                'severity': 'critical',
                'active_goal': 'goal-44e50921129bf475',
                'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json',
                'failure_class': 'no_concrete_change',
                'remediation_class': 'planner_hardening',
                'recommended_remediation_action': 'Tighten the next-cycle planner so it must emit exactly one file-level action plus one verification command and an explicit blocked-next-step fallback.',
                'blocked_next_step': 'Rewrite the cycle around one file-level action or an explicit blocked next step.',
                'requested_executor': 'hermes_subagent',
                'stale_execution_detected': True,
                'stale_execution_detected_at': REFERENCE_NOW,
                'stale_execution_incident_path': '/tmp/stale_execution_incident.json',
                'stale_execution_next_action_path': '/tmp/stale_execution_next_action.json',
                'stale_execution_age_seconds': 51582.007993,
                'stale_execution_age': '14h19m42s',
                'stale_execution_threshold_minutes': 30,
                'stale_execution_recommended_next_action': 'Re-dispatch one bounded slice for the active goal after preserving the stale incident evidence.',
                'stale_execution_next_action_summary': 'Re-dispatch one bounded slice after preserving the stale incident evidence.',
                'dispatch_state': 'stale_blocked',
            }
        ]
    }

    monkeypatch.setattr(snapshot, 'ACTIVE_EXECUTION', active_execution_path)
    monkeypatch.setattr(snapshot, 'QUEUE', tmp_path / 'execution_queue.json')

    registry = snapshot.build_active_execution(queue, REFERENCE_NOW)

    assert registry['stale_execution_detected'] is True
    assert registry['summary']['needs_redispatch'] == 1
    assert registry['summary']['stale_execution_incidents'] == 1
    assert registry['stale_execution_task'] is None
    assert registry['stale_execution_incident_task'] is not None
    assert registry['stale_execution_incident_task']['queue_status'] == 'stale_blocked'
    assert registry['stale_execution_incident_task']['execution_state'] == 'needs_redispatch'
    assert registry['active_tasks'][0]['execution_state'] == 'needs_redispatch'
    assert active_execution_path.exists()


def test_stale_execution_artifact_filenames_are_bounded_for_long_task_keys(tmp_path: Path, monkeypatch) -> None:
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    incident_dir = tmp_path / 'stale_execution_incidents'
    next_action_dir = tmp_path / 'stale_execution_next_actions'
    redispatch_dir = tmp_path / 'stale_execution_redispatches'
    assignment_dir = tmp_path / 'execution_assignments'
    latest_incident_path = tmp_path / 'stale_execution_incident.json'
    latest_next_action_path = tmp_path / 'stale_execution_next_action.json'
    latest_redispatch_path = tmp_path / 'stale_execution_redispatch.json'
    latest_assignment_path = tmp_path / 'execution_assignment.json'

    long_task_key = _long_task_key()
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', long_task_key.strip())
    slug = re.sub(r'-{2,}', '-', slug).strip('-._')
    expected_digest = hashlib.sha256(slug.encode('utf-8')).hexdigest()[:12]

    task = {
        'created_at': '2026-04-16T07:30:50.705406Z',
        'status': 'in_progress',
        'source': 'hermes-autonomy-controller',
        'diagnosis': 'stagnating_on_quality_blocker',
        'severity': 'critical',
        'active_goal': 'goal-44e50921129bf475',
        'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json',
        'failure_class': 'no_concrete_change',
        'blocked_next_step': 'Rewrite the cycle around one file-level action or an explicit blocked next step.',
        'remediation_class': 'planner_hardening',
        'recommended_remediation_action': 'Tighten the next-cycle planner so it must emit exactly one file-level action plus one verification command and an explicit blocked-next-step fallback.',
        'dedupe_key': long_task_key,
        'dispatched_at': '2026-04-16T07:42:30.781890Z',
        'dispatch_state': 'dispatched',
        'execution_requested_at': '2026-04-16T08:10:03.072371Z',
        'requested_executor': 'hermes_subagent',
        'executor_handoff_at': '2026-04-16T08:12:46.643769Z',
        'delegated_executor_started_at': STALE_STARTED_AT,
        'delegated_executor_requested_at': STALE_STARTED_AT,
        'delegated_executor_request_status': 'requested',
    }
    active_execution = {
        'updated_at': REFERENCE_NOW,
        'source_queue_path': str(queue_path),
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
        },
        'has_actually_executing_task': True,
        'live_task': {'task_key': long_task_key, 'task_index': 0, 'execution_state': 'in_progress'},
        'stale_execution_detected': True,
        'stale_execution_threshold_minutes': 30,
        'stale_execution_task': {
            'stale_detected': True,
            'threshold_minutes': 30,
            'threshold_seconds': 3600,
            'task_key': long_task_key,
            'executor': 'hermes_subagent',
            'started_at': STALE_STARTED_AT,
            'age_seconds': 51582.007993,
            'age': '14h19m42s',
            'recommended_next_action': 'Treat this as a stale-execution incident under the 30-minute investigation rule: check the live executor, confirm whether the task is still running, and either record the terminal result or re-dispatch the bounded slice.',
            'inspection_source': 'active_execution',
            'task_index': 0,
            'task_status': 'in_progress',
            'observed_in_progress_candidates': 1,
        },
        'active_tasks': [
            {
                'task_index': 0,
                'task_key': long_task_key,
                'queue_status': 'in_progress',
                'execution_state': 'in_progress',
                'is_live_execution': True,
                'is_blocked': False,
                'is_terminal': False,
                'source': 'hermes-autonomy-controller',
                'diagnosis': 'stagnating_on_quality_blocker',
                'severity': 'critical',
                'active_goal': 'goal-44e50921129bf475',
                'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json',
                'failure_class': 'no_concrete_change',
                'remediation_class': 'planner_hardening',
                'recommended_remediation_action': 'Tighten the next-cycle planner so it must emit exactly one file-level action plus one verification command and an explicit blocked-next-step fallback.',
                'blocked_next_step': 'Rewrite the cycle around one file-level action or an explicit blocked next step.',
                'requested_executor': 'hermes_subagent',
                'execution_request_path': '/home/ozand/herkoot/Projects/nanobot-ops-dashboard/control/execution_requests/example.json',
                'delegated_executor_started_at': STALE_STARTED_AT,
                'delegated_executor_requested_at': STALE_STARTED_AT,
                'delegated_executor_request_status': 'requested',
            }
        ],
        'terminal_tasks': [],
    }

    active_execution_path.write_text(json.dumps(active_execution, indent=2), encoding='utf-8')
    queue_path.write_text(json.dumps(_queue_payload(task), indent=2), encoding='utf-8')

    monkeypatch.setattr(controller, 'ACTIVE_EXECUTION_PATH', active_execution_path)
    monkeypatch.setattr(controller, 'QUEUE_PATH', queue_path)
    monkeypatch.setattr(controller, 'INCIDENT_DIR', incident_dir)
    monkeypatch.setattr(controller, 'NEXT_ACTION_DIR', next_action_dir)
    monkeypatch.setattr(controller, 'LATEST_INCIDENT_PATH', latest_incident_path)
    monkeypatch.setattr(controller, 'LATEST_NEXT_ACTION_PATH', latest_next_action_path)
    monkeypatch.setattr(snapshot, 'ACTIVE_EXECUTION', active_execution_path)
    monkeypatch.setattr(snapshot, 'QUEUE', queue_path)

    incident_result = controller.consume_stale_execution_incident(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        incident_dir=incident_dir,
        next_action_dir=next_action_dir,
        latest_incident_path=latest_incident_path,
        latest_next_action_path=latest_next_action_path,
        now=REFERENCE_NOW,
    )
    incident_name = Path(incident_result['incident_path']).name
    next_action_name = Path(incident_result['next_action_path']).name
    assert len(incident_name) < 140
    assert len(next_action_name) < 140
    assert incident_name.endswith(f'-{expected_digest}.json')
    assert next_action_name.endswith(f'-{expected_digest}.json')

    next_action_result = next_action_controller.consume_stale_execution_next_action(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        next_action_dir=next_action_dir,
        redispatch_dir=redispatch_dir,
        latest_redispatch_path=latest_redispatch_path,
        now=REFERENCE_NOW,
    )
    redispatch_name = Path(next_action_result['redispatch_path']).name
    assert len(redispatch_name) < 140
    assert redispatch_name.endswith(f'-{expected_digest}.json')

    redispatch_controller.consume_queued_redispatch_assignment(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        assignment_dir=assignment_dir,
        latest_assignment_path=latest_assignment_path,
        now=REFERENCE_NOW,
    )
    assignment_files = list(assignment_dir.glob('*.json'))
    assert len(assignment_files) == 1
    assignment_name = assignment_files[0].name
    assert len(assignment_name) < 140
    assert assignment_name.endswith(f'-{expected_digest}.json')
