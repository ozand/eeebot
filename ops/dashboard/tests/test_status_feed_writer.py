from __future__ import annotations

import json
from pathlib import Path

from scripts import build_status_feed as feed
from scripts import build_status_snapshot as snapshot

REFERENCE_NOW = '2026-04-17T06:35:51.835039Z'


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


def _queue_with_live_task() -> dict[str, object]:
    return {
        'tasks': [
            {
                'created_at': '2026-04-16T07:30:50.705406Z',
                'status': 'in_progress',
                'source': 'hermes-autonomy-controller',
                'diagnosis': 'stagnating_on_quality_blocker',
                'severity': 'critical',
                'active_goal': 'goal-44e50921129bf475',
                'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json',
                'failure_class': 'no_concrete_change',
                'remediation_class': 'planner_hardening',
                'dedupe_key': 'stagnating_on_quality_blocker|goal-44e50921129bf475|/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json|no_concrete_change|planner_hardening',
                'delegated_executor_started_at': REFERENCE_NOW,
                'delegated_executor_requested_at': REFERENCE_NOW,
            }
        ]
    }


def _queue_without_live_task() -> dict[str, object]:
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


def _queue_with_fresh_and_stale_live_tasks() -> dict[str, object]:
    return {
        'tasks': [
            {
                'created_at': '2026-04-17T06:25:51.835039Z',
                'status': 'in_progress',
                'source': 'hermes-autonomy-controller',
                'diagnosis': 'recent_execution',
                'severity': 'medium',
                'active_goal': 'goal-fresh-task',
                'report_source': '/tmp/fresh-report.json',
                'failure_class': 'none',
                'remediation_class': 'monitoring',
                'dedupe_key': 'fresh-task',
                'delegated_executor_started_at': '2026-04-17T06:25:51.835039Z',
                'delegated_executor_requested_at': '2026-04-17T06:25:51.835039Z',
            },
            {
                'created_at': '2026-04-16T07:30:50.705406Z',
                'status': 'in_progress',
                'source': 'hermes-autonomy-controller',
                'diagnosis': 'stagnating_on_quality_blocker',
                'severity': 'critical',
                'active_goal': 'goal-44e50921129bf475',
                'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json',
                'failure_class': 'no_concrete_change',
                'remediation_class': 'planner_hardening',
                'dedupe_key': 'stagnating_on_quality_blocker|goal-44e50921129bf475|/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json|no_concrete_change|planner_hardening',
                'delegated_executor_started_at': '2026-04-16T11:40:49.015519Z',
                'delegated_executor_requested_at': '2026-04-16T11:40:49.015519Z',
            },
        ]
    }


def test_append_status_feed_prefers_stale_live_task_when_multiple_in_progress_tasks_exist(tmp_path: Path, monkeypatch) -> None:
    active_projects_path = tmp_path / 'active_projects.json'
    queue_path = tmp_path / 'execution_queue.json'
    active_execution_path = tmp_path / 'active_execution.json'
    feed_path = tmp_path / 'status_feed.jsonl'

    active_projects_path.write_text(json.dumps(_active_projects(), indent=2), encoding='utf-8')
    queue_path.write_text(json.dumps(_queue_with_fresh_and_stale_live_tasks(), indent=2), encoding='utf-8')

    monkeypatch.setattr(snapshot, 'ACTIVE_PROJECTS', active_projects_path)
    monkeypatch.setattr(snapshot, 'QUEUE', queue_path)
    monkeypatch.setattr(snapshot, 'ACTIVE_EXECUTION', active_execution_path)
    monkeypatch.setattr(feed, 'FEED_PATH', feed_path)

    feed.append_status_feed(updated_at=REFERENCE_NOW)

    entry = json.loads(feed_path.read_text(encoding='utf-8').splitlines()[-1])
    assert entry['status_snapshot_summary']['stale_execution_detected'] is True
    assert entry['status_snapshot_summary']['stale_execution_incidents'] == 0
    assert entry['current_live_task_or_state']['stale_execution_detected'] is True
    assert entry['current_live_task_or_state']['stale_execution_recommended_next_action'].startswith('Treat this as a stale-execution incident')
    assert entry['current_live_task_or_state']['stale_execution_age_seconds'] > 0
    assert entry['current_live_task_or_state']['active_goal'] == 'goal-44e50921129bf475'

    refreshed = json.loads(active_execution_path.read_text(encoding='utf-8'))
    assert refreshed['live_task']['stale_execution_detected'] is True
    assert refreshed['live_task']['active_goal'] == 'goal-44e50921129bf475'
    assert refreshed['summary']['stale_execution_detected'] is True


def _queue_without_live_task_but_with_stale_incident() -> dict[str, object]:
    return {
        'tasks': [
            {
                'created_at': '2026-04-16T07:30:50.705406Z',
                'status': 'stale_blocked',
                'source': 'hermes-autonomy-controller',
                'diagnosis': 'stagnating_on_quality_blocker',
                'severity': 'critical',
                'active_goal': 'goal-44e50921129bf475',
                'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json',
                'failure_class': 'no_concrete_change',
                'remediation_class': 'planner_hardening',
                'execution_state': 'needs_redispatch',
                'stale_execution_detected': True,
                'stale_execution_detected_at': REFERENCE_NOW,
                'stale_execution_incident_path': '/tmp/stale_execution_incident.json',
                'stale_execution_next_action_path': '/tmp/stale_execution_next_action.json',
            }
        ]
    }


def test_append_status_feed_writes_current_live_state(tmp_path: Path, monkeypatch) -> None:
    active_projects_path = tmp_path / 'active_projects.json'
    queue_path = tmp_path / 'execution_queue.json'
    active_execution_path = tmp_path / 'active_execution.json'
    feed_path = tmp_path / 'status_feed.jsonl'

    active_projects_path.write_text(json.dumps(_active_projects(), indent=2), encoding='utf-8')
    queue_path.write_text(json.dumps(_queue_with_live_task(), indent=2), encoding='utf-8')

    monkeypatch.setattr(snapshot, 'ACTIVE_PROJECTS', active_projects_path)
    monkeypatch.setattr(snapshot, 'QUEUE', queue_path)
    monkeypatch.setattr(snapshot, 'ACTIVE_EXECUTION', active_execution_path)
    monkeypatch.setattr(feed, 'FEED_PATH', feed_path)

    result = feed.append_status_feed(updated_at=REFERENCE_NOW)

    assert result['status_feed_path'] == str(feed_path)
    assert feed_path.exists()
    lines = feed_path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry['timestamp'] == REFERENCE_NOW
    assert entry['live_execution_exists'] is True
    assert entry['current_live_task_or_state']['execution_state'] == 'in_progress'
    assert entry['current_live_task_or_state']['active_goal'] == 'goal-44e50921129bf475'
    assert entry['active_projects_summary'][0]['id'] == 'project-nanobot-eeepc-owner-loop'
    assert entry['active_projects_summary'][0]['status'] == 'in_progress'

    refreshed = json.loads(active_execution_path.read_text(encoding='utf-8'))
    assert refreshed['has_actually_executing_task'] is True
    assert refreshed['live_task']['task_key'].startswith('stagnating_on_quality_blocker|goal-44e50921129bf475')


def test_append_status_feed_records_stale_no_live_executor_state(tmp_path: Path, monkeypatch) -> None:
    active_projects_path = tmp_path / 'active_projects.json'
    queue_path = tmp_path / 'execution_queue.json'
    active_execution_path = tmp_path / 'active_execution.json'
    feed_path = tmp_path / 'status_feed.jsonl'

    active_projects_path.write_text(json.dumps(_active_projects(), indent=2), encoding='utf-8')
    queue_path.write_text(json.dumps(_queue_without_live_task_but_with_stale_incident(), indent=2), encoding='utf-8')

    monkeypatch.setattr(snapshot, 'ACTIVE_PROJECTS', active_projects_path)
    monkeypatch.setattr(snapshot, 'QUEUE', queue_path)
    monkeypatch.setattr(snapshot, 'ACTIVE_EXECUTION', active_execution_path)
    monkeypatch.setattr(feed, 'FEED_PATH', feed_path)

    feed.append_status_feed(updated_at=REFERENCE_NOW)

    entry = json.loads(feed_path.read_text(encoding='utf-8').splitlines()[-1])
    assert entry['live_execution_exists'] is False
    assert isinstance(entry['current_live_task_or_state'], dict)
    assert entry['current_live_task_or_state']['execution_state'] == 'needs_redispatch'
    assert entry['current_live_task_or_state']['status'] == 'stale_blocked'
    assert entry['current_live_task_or_state']['stale_execution_detected'] is True
    assert entry['current_live_task_or_state']['incident_path'] == '/tmp/stale_execution_incident.json'
    assert entry['current_live_task_or_state']['next_action_path'] == '/tmp/stale_execution_next_action.json'
    assert entry['current_live_task_or_state']['source'] == 'hermes-autonomy-controller'
    assert entry['current_live_task_or_state']['report_source'] == '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T121151Z.json'
    assert entry['current_live_task_or_state']['next_action'] == 'redispatch_stale_execution'
    assert entry['active_projects_summary'][0]['status'] == 'stale_blocked'
    assert entry['status_snapshot_summary']['needs_redispatch'] == 1
    assert entry['status_snapshot_summary']['stale_execution_detected'] is True


def test_append_status_feed_records_no_live_executor_state(tmp_path: Path, monkeypatch) -> None:
    active_projects_path = tmp_path / 'active_projects.json'
    queue_path = tmp_path / 'execution_queue.json'
    active_execution_path = tmp_path / 'active_execution.json'
    feed_path = tmp_path / 'status_feed.jsonl'

    active_projects_path.write_text(json.dumps(_active_projects(), indent=2), encoding='utf-8')
    queue_path.write_text(json.dumps(_queue_without_live_task(), indent=2), encoding='utf-8')

    monkeypatch.setattr(snapshot, 'ACTIVE_PROJECTS', active_projects_path)
    monkeypatch.setattr(snapshot, 'QUEUE', queue_path)
    monkeypatch.setattr(snapshot, 'ACTIVE_EXECUTION', active_execution_path)
    monkeypatch.setattr(feed, 'FEED_PATH', feed_path)

    feed.append_status_feed(updated_at=REFERENCE_NOW)

    entry = json.loads(feed_path.read_text(encoding='utf-8').splitlines()[-1])
    assert entry['live_execution_exists'] is False
    assert isinstance(entry['current_live_task_or_state'], dict)
    assert entry['current_live_task_or_state']['execution_state'] == 'waiting_for_dispatch'
    assert entry['current_live_task_or_state']['status'] == 'waiting_for_dispatch'
    assert entry['current_live_task_or_state']['next_action'] == 'dispatch_or_enqueue_bounded_task'
    assert entry['active_projects_summary'][0]['status'] == 'waiting_for_dispatch'
