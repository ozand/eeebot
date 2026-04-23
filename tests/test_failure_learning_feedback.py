import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_failure_learning_biases_next_cycle_toward_repair(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 23, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    learning_dir = tmp_path / 'state' / 'self_evolution' / 'failure_learning'
    learning_dir.mkdir(parents=True, exist_ok=True)
    (learning_dir / 'latest.json').write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-bad',
        'failed_commit': 'abc123',
        'health_reasons': ['stale_report', 'missing_control_plane_summary'],
        'rollback_target': '/tmp/prev',
        'learning_summary': 'Candidate failed health gate; inspect rollback evidence first.'
    }), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    (goals_dir / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'record-reward',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'}
        ],
        'generated_candidates': []
    }), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=15)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    assert current.get('current_task_id') == 'analyze-last-failed-candidate'
    assert any(item.get('task_id') == 'analyze-last-failed-candidate' for item in (current.get('generated_candidates') or []))
    assert current.get('failure_learning', {}).get('candidate_id') == 'candidate-bad'


def test_fresh_failure_learning_preempts_active_richer_lane(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 23, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    learning_dir = tmp_path / 'state' / 'self_evolution' / 'failure_learning'
    learning_dir.mkdir(parents=True, exist_ok=True)
    (learning_dir / 'latest.json').write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-fresh-bad',
        'failed_commit': 'feedbead',
        'health_reasons': ['service_inactive'],
        'rollback_target': '/tmp/prev',
        'learning_summary': 'Repair first.'
    }), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    (goals_dir / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'subagent-verify-materialized-improvement',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
            {'task_id': 'subagent-verify-materialized-improvement', 'title': 'Use one bounded subagent-assisted review to verify the materialized improvement artifact', 'status': 'active', 'kind': 'review'}
        ],
        'generated_candidates': [
            {'task_id': 'subagent-verify-materialized-improvement', 'title': 'Use one bounded subagent-assisted review to verify the materialized improvement artifact', 'status': 'active', 'kind': 'review'}
        ]
    }), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=15)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    assert current.get('current_task_id') == 'analyze-last-failed-candidate'
