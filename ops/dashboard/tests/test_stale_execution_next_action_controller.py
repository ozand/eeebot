from __future__ import annotations

import json
from pathlib import Path

from scripts import build_status_snapshot as snapshot
from scripts import consume_stale_execution_next_actions as controller

REFERENCE_NOW = '2026-04-16T16:56:01Z'
STALE_STARTED_AT = '2026-04-16T11:40:49.015519Z'


def _stale_task() -> dict[str, object]:
    return {
        'created_at': '2026-04-16T07:30:50.705406Z',
        'status': 'stale_blocked',
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
        'dispatch_state': 'stale_blocked',
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
        'stale_execution_age_seconds': 52199.647579,
        'stale_execution_age': '14h29m59s',
        'stale_execution_threshold_minutes': 30,
        'stale_execution_recommended_next_action': 'Treat this as a stale-execution incident under the 30-minute investigation rule: check the live executor, confirm whether the task is still running, and either record the terminal result or re-dispatch the bounded slice.',
        'stale_execution_next_action_summary': 'Re-dispatch one bounded slice for goal-44e50921129bf475 after preserving the stale incident evidence.',
        'stale_execution_previous_status': 'in_progress',
        'stale_execution_previous_execution_state': None,
        'stale_execution_previous_started_at': STALE_STARTED_AT,
    }


def _queue_payload(task: dict[str, object]) -> dict[str, object]:
    return {'tasks': [dict(task)]}


def _next_action_payload(task: dict[str, object], queue_path: Path, incident_path: Path, next_action_path: Path) -> dict[str, object]:
    return {
        'next_action_created_at': '2026-04-17T02:10:48.663183Z',
        'next_action_created_by': 'consume_stale_execution_incidents.py',
        'next_action_type': 'incident_next_action',
        'next_action_mode': 'needs_redispatch',
        'task_key': task['dedupe_key'],
        'queue_task_index': 0,
        'source_stale_execution_incident_path': str(incident_path),
        'source_queue_path': str(queue_path),
        'source_active_execution_path': '/tmp/active_execution.json',
        'source_queue_task_snapshot': dict(task),
        'watchdog_result': {
            'stale_detected': True,
            'threshold_minutes': 30,
            'threshold_seconds': 3600,
            'task_key': task['dedupe_key'],
            'executor': 'hermes_subagent',
            'started_at': STALE_STARTED_AT,
            'age_seconds': 52199.647579,
            'age': '14h29m59s',
            'recommended_next_action': 'Treat this as a stale-execution incident under the 30-minute investigation rule: check the live executor, confirm whether the task is still running, and either record the terminal result or re-dispatch the bounded slice.',
            'inspection_source': 'active_execution',
            'task_index': 0,
            'task_status': 'stale_blocked',
            'observed_in_progress_candidates': 1,
        },
        'next_action_summary': 'Re-dispatch one bounded slice for goal-44e50921129bf475 after preserving the stale incident evidence. Use the bounded candidate to resume the queue.',
        'next_action_artifact_path': str(next_action_path),
        'incident_artifact_path': str(incident_path),
        'bounded_redispatch_candidate': {
            'status': 'needs_redispatch',
            'execution_state': 'needs_redispatch',
            'task_key': task['dedupe_key'],
            'active_goal': task['active_goal'],
            'diagnosis': task['diagnosis'],
            'severity': task['severity'],
            'report_source': task['report_source'],
            'failure_class': task['failure_class'],
            'remediation_class': task['remediation_class'],
            'recommended_remediation_action': task['recommended_remediation_action'],
            'blocked_next_step': task['blocked_next_step'],
            'requested_executor': task['requested_executor'],
            'reason': 'Redispatch is bounded to one fresh candidate only after the stale execution has been truthfully recorded.',
        },
    }


def test_consume_stale_next_action_transforms_queue_to_queued_redispatch(tmp_path: Path, monkeypatch) -> None:
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    next_action_dir = tmp_path / 'stale_execution_next_actions'
    redispatch_dir = tmp_path / 'stale_execution_redispatches'
    latest_redispatch_path = tmp_path / 'stale_execution_redispatch.json'
    incident_path = tmp_path / 'stale_execution_incident.json'
    next_action_path = next_action_dir / '20260416T114049015519Z-stale-task.json'

    task = _stale_task()
    active_execution = {
        'updated_at': REFERENCE_NOW,
        'source_queue_path': str(queue_path),
        'summary': {
            'total': 1,
            'active': 1,
            'queued': 0,
            'in_progress': 0,
            'waiting_for_dispatch': 0,
            'blocked': 1,
            'completed': 0,
            'live_execution_tasks': 0,
            'stale_execution_detected': True,
            'stale_execution_incidents': 1,
        },
        'has_actually_executing_task': False,
        'live_task': None,
        'stale_execution_detected': True,
        'stale_execution_threshold_minutes': 30,
        'stale_execution_task': None,
        'stale_execution_incident_task': {
            'task_index': 0,
            'task_key': task['dedupe_key'],
            'queue_status': 'stale_blocked',
            'execution_state': 'needs_redispatch',
        },
        'active_tasks': [
            {
                'task_index': 0,
                'task_key': task['dedupe_key'],
                'queue_status': 'stale_blocked',
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
                'stale_execution_detected_at': '2026-04-17T02:10:48.663183Z',
                'stale_execution_incident_path': str(incident_path),
                'stale_execution_next_action_path': str(next_action_path),
                'stale_execution_age_seconds': 52199.647579,
                'stale_execution_age': '14h29m59s',
                'stale_execution_threshold_minutes': 30,
                'stale_execution_recommended_next_action': 'Treat this as a stale-execution incident under the 30-minute investigation rule: check the live executor, confirm whether the task is still running, and either record the terminal result or re-dispatch the bounded slice.',
                'stale_execution_next_action_summary': 'Re-dispatch one bounded slice for goal-44e50921129bf475 after preserving the stale incident evidence.',
                'dispatch_state': 'stale_blocked',
            }
        ],
        'terminal_tasks': [],
    }

    active_execution_path.write_text(json.dumps(active_execution, indent=2), encoding='utf-8')
    queue_path.write_text(json.dumps(_queue_payload(task), indent=2), encoding='utf-8')
    next_action_dir.mkdir(parents=True, exist_ok=True)
    next_action_path.write_text(json.dumps(_next_action_payload(task, queue_path, incident_path, next_action_path), indent=2), encoding='utf-8')

    monkeypatch.setattr(snapshot, 'ACTIVE_EXECUTION', active_execution_path)
    monkeypatch.setattr(snapshot, 'QUEUE', queue_path)
    monkeypatch.setattr(controller, 'ACTIVE_EXECUTION_PATH', active_execution_path)
    monkeypatch.setattr(controller, 'QUEUE_PATH', queue_path)
    monkeypatch.setattr(controller, 'NEXT_ACTION_DIR', next_action_dir)
    monkeypatch.setattr(controller, 'REDISPATCH_DIR', redispatch_dir)
    monkeypatch.setattr(controller, 'LATEST_REDISPATCH_PATH', latest_redispatch_path)

    result = controller.consume_stale_execution_next_action(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        next_action_dir=next_action_dir,
        redispatch_dir=redispatch_dir,
        latest_redispatch_path=latest_redispatch_path,
    )

    assert result['consumed'] is True
    assert result['status'] == 'queued'
    assert result['execution_state'] == 'queued'
    assert result['task_key'].startswith('stagnating_on_quality_blocker|goal-44e50921129bf475')
    assert result['redispatch_path'].endswith('.json')
    assert result['next_action_path'] == str(next_action_path)
    assert result['source_incident_path'] == str(incident_path)

    queue_after = json.loads(queue_path.read_text(encoding='utf-8'))
    updated_task = queue_after['tasks'][0]
    assert updated_task['status'] == 'queued'
    assert updated_task['execution_state'] == 'queued'
    assert updated_task['stale_execution_redispatch_artifact_path'] == result['redispatch_path']
    assert updated_task['stale_execution_redispatch_source_next_action_path'] == str(next_action_path)
    assert updated_task['stale_execution_redispatch_previous_status'] == 'stale_blocked'

    redispatch_artifact = json.loads(Path(result['redispatch_path']).read_text(encoding='utf-8'))
    assert redispatch_artifact['redispatch_type'] == 'stale_next_action_redispatch'
    assert redispatch_artifact['redispatch_state'] == 'queued'
    assert redispatch_artifact['source_stale_execution_next_action_path'] == str(next_action_path)
    assert redispatch_artifact['redispatched_queue_task_snapshot']['status'] == 'queued'
    assert redispatch_artifact['redispatched_queue_task_snapshot']['stale_execution_redispatch_source_incident_path'] == str(incident_path)

    latest_pointer = json.loads(latest_redispatch_path.read_text(encoding='utf-8'))
    assert latest_pointer['redispatch_artifact_path'] == result['redispatch_path']

    refreshed_registry = json.loads(active_execution_path.read_text(encoding='utf-8'))
    assert refreshed_registry['stale_execution_detected'] is True
    assert refreshed_registry['summary']['queued'] == 1
    assert refreshed_registry['summary']['in_progress'] == 0
    assert refreshed_registry['stale_execution_incident_task']['queue_status'] == 'queued'

    repeat_result = controller.consume_stale_execution_next_action(
        active_execution_path=active_execution_path,
        queue_path=queue_path,
        next_action_dir=next_action_dir,
        redispatch_dir=redispatch_dir,
        latest_redispatch_path=latest_redispatch_path,
    )
    assert repeat_result['consumed'] is False
    assert repeat_result['reason'] == 'already_recorded'
    assert repeat_result['redispatch_path'] == result['redispatch_path']
