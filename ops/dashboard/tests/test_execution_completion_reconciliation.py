from __future__ import annotations

import json
from pathlib import Path

from scripts import build_status_snapshot as snapshot

REFERENCE_NOW = '2026-04-17T03:50:38Z'
COMPLETION_COMMIT = 'f557aeb25c70535862aa57f2f192a6c3947e1d73'
TASK_KEY = 'stagnating_on_quality_blocker|goal-44e50921129bf475|/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json|no_concrete_change|planner_hardening'


def _completed_task() -> dict[str, object]:
    return {
        'created_at': '2026-04-16T07:30:50.705406Z',
        'status': 'completed',
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
        'dedupe_key': TASK_KEY,
        'dispatched_at': '2026-04-16T07:42:30.781890Z',
        'dispatch_state': 'completed',
        'dispatched_by': 'consume_execution_queue.py',
        'execution_requested_at': '2026-04-16T08:10:03.072371Z',
        'execution_request_path': '/home/ozand/herkoot/Projects/nanobot-ops-dashboard/control/execution_requests/example.json',
        'requested_executor': 'hermes_subagent',
        'execution_request_status': 'completed',
        'executor_handoff_at': '2026-04-16T08:12:46.643769Z',
        'delegated_executor_started_at': '2026-04-16T11:40:49.015519Z',
        'delegated_executor_requested_at': '2026-04-16T11:40:49.015519Z',
        'delegated_executor_request_path': '/home/ozand/herkoot/Projects/nanobot-ops-dashboard/control/delegated_executor_requests/example.json',
        'delegated_executor_request_status': 'completed',
        'delegated_executor_request_previous_status': 'requested',
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
        'stale_execution_redispatch_previous_started_at': '2026-04-16T11:40:49.015519Z',
        'stale_execution_threshold_minutes': 60,
        'stale_execution_policy_summary': None,
        'stale_execution_redispatch_next_action_summary': 'Re-dispatch one bounded slice for the goal after preserving the stale incident evidence.',
        'stale_execution_redispatch_candidate': {
            'status': 'needs_redispatch',
            'execution_state': 'needs_redispatch',
            'task_key': TASK_KEY,
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
        'execution_completion_path': '/home/ozand/herkoot/Projects/nanobot-ops-dashboard/control/execution_completions/20260417T035038Z-stagnating_on_quality_blocker-goal-44e50921129bf475-no_concrete_change-planner_hardening.json',
        'execution_completion_status': 'verified_completed',
        'execution_completed_at': REFERENCE_NOW,
        'execution_completed_by': 'manual_reconciliation',
        'execution_completion_commit': COMPLETION_COMMIT,
        'execution_completion_verification_method': 'eeepc_side_by_side',
        'execution_completion_verification_status': 'passed',
        'execution_completion_summary': 'Bounded implementation completed and eeepc side-by-side verification passed; no live delegated executor remains.',
        'queue_status': 'completed',
        'execution_state': 'completed',
        'completion_recorded_at': REFERENCE_NOW,
        'completion_recorded_by': 'manual_reconciliation',
        'completion_commit': COMPLETION_COMMIT,
        'completion_verification_method': 'eeepc_side_by_side',
        'completion_verification_status': 'passed',
        'completion_summary': 'Bounded implementation completed and eeepc side-by-side verification passed.',
    }


def test_build_status_snapshot_marks_verified_completion_as_non_live(tmp_path: Path, monkeypatch) -> None:
    active_execution_path = tmp_path / 'active_execution.json'
    queue_path = tmp_path / 'execution_queue.json'
    queue = {'tasks': [_completed_task()]}

    monkeypatch.setattr(snapshot, 'ACTIVE_EXECUTION', active_execution_path)
    monkeypatch.setattr(snapshot, 'QUEUE', queue_path)

    registry = snapshot.build_active_execution(queue, REFERENCE_NOW)

    assert registry['has_actually_executing_task'] is False
    assert registry['live_task'] is None
    assert registry['summary']['active'] == 0
    assert registry['summary']['completed'] == 1
    assert registry['summary']['live_execution_tasks'] == 0
    assert len(registry['terminal_tasks']) == 1
    assert registry['terminal_tasks'][0]['execution_completion_status'] == 'verified_completed'
    assert registry['terminal_tasks'][0]['execution_completion_commit'] == COMPLETION_COMMIT
    assert registry['terminal_tasks'][0]['execution_completion_verification_status'] == 'passed'
    assert registry['terminal_tasks'][0]['stale_execution_threshold_minutes'] == 30
    assert registry['terminal_tasks'][0]['stale_execution_policy_summary'] == 'in_progress tasks older than 30 minutes must be investigated or escalated.'
    assert active_execution_path.exists()

    refreshed = json.loads(active_execution_path.read_text(encoding='utf-8'))
    assert refreshed['has_actually_executing_task'] is False
    assert refreshed['live_task'] is None
    assert refreshed['summary']['completed'] == 1
    assert refreshed['terminal_tasks'][0]['execution_completion_status'] == 'verified_completed'
    assert refreshed['terminal_tasks'][0]['stale_execution_threshold_minutes'] == 30
    assert refreshed['terminal_tasks'][0]['stale_execution_policy_summary'] == 'in_progress tasks older than 30 minutes must be investigated or escalated.'
