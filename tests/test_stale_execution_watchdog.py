from __future__ import annotations

from pathlib import Path

from scripts import build_status_snapshot as status_snapshot
from scripts.stale_execution_watchdog import detect_stale_execution


REFERENCE_NOW = '2026-04-16T16:56:01Z'
STALE_STARTED_AT = '2026-04-16T11:40:49.015519Z'


def _stale_active_task() -> dict[str, object]:
    return {
        'task_index': 0,
        'task_key': 'stagnating_on_quality_blocker|goal-44e50921129bf475|/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json|no_concrete_change|planner_hardening',
        'dedupe_key': 'stagnating_on_quality_blocker|goal-44e50921129bf475|/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json|no_concrete_change|planner_hardening',
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


def _stale_queue_task() -> dict[str, object]:
    task = _stale_active_task()
    task.pop('execution_state')
    task.pop('is_live_execution')
    task.pop('is_blocked')
    task.pop('is_terminal')
    task.pop('task_index')
    task['status'] = 'in_progress'
    return task


def test_detect_stale_execution_flags_live_task_over_threshold() -> None:
    active_execution = {'active_tasks': [_stale_active_task()]}
    result = detect_stale_execution(active_execution=active_execution, threshold_minutes=30, now=REFERENCE_NOW)

    assert result['stale_detected'] is True
    assert result['threshold_minutes'] == 30
    assert result['policy_summary'] == 'in_progress tasks older than 30 minutes must be investigated or escalated.'
    assert result['task_key'].startswith('stagnating_on_quality_blocker|goal-44e50921129bf475')
    assert result['executor'] == 'hermes_subagent'
    assert result['started_at'] == STALE_STARTED_AT
    assert result['age_seconds'] and result['age_seconds'] > 60 * 60
    assert 'stale-execution incident' in result['recommended_next_action']


def test_build_status_snapshot_exposes_stale_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(status_snapshot, 'ACTIVE_EXECUTION', tmp_path / 'active_execution.json')
    queue = {'tasks': [_stale_queue_task()]}

    registry = status_snapshot.build_active_execution(queue, REFERENCE_NOW)

    assert registry['stale_execution_detected'] is True
    assert registry['summary']['stale_execution_detected'] is True
    assert registry['stale_execution_threshold_minutes'] == 30
    assert registry['stale_execution_policy_summary'] == 'in_progress tasks older than 30 minutes must be investigated or escalated.'
    assert registry['stale_execution_task'] is not None
    assert registry['stale_execution_task']['stale_detected'] is True
    assert registry['stale_execution_task']['started_at'] == STALE_STARTED_AT
    assert registry['stale_execution_task']['task_key'].startswith('stagnating_on_quality_blocker|goal-44e50921129bf475')
    assert (tmp_path / 'active_execution.json').exists()
