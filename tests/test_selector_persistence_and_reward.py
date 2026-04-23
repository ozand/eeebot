import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_selected_generated_candidate_persists_across_next_pass_cycle(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    current_payload = {
        'schema_version': 'task-plan-v1',
        'current_task_id': 'inspect-pass-streak',
        'tasks': [
            {'task_id': 'refresh-approval-gate', 'title': 'Refresh approval gate', 'status': 'done'},
            {'task_id': 'run-bounded-turn', 'title': 'Run bounded turn', 'status': 'done'},
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'active', 'kind': 'review'},
        ],
    }
    (goals_dir / 'current.json').write_text(json.dumps(current_payload), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    assert current['current_task_id'] == 'inspect-pass-streak'


def test_repeated_record_reward_pass_gets_less_optimistic_reward_signal(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    experiments_dir = tmp_path / 'state' / 'experiments'
    experiments_dir.mkdir(parents=True)
    (experiments_dir / 'latest.json').write_text(json.dumps({'result_status': 'PASS', 'current_task_id': 'record-reward', 'metric_current': 1.0, 'metric_frontier': 1.0}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    (goals_dir / 'current.json').write_text(json.dumps({'schema_version': 'task-plan-v1', 'current_task_id': 'record-reward', 'tasks': [{'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'}]}), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    report = sorted((tmp_path / 'state' / 'reports').glob('evolution-*.json'))[-1]
    payload = _read_json(report)
    assert payload['reward_signal']['value'] == 0.6
    assert payload['reward_signal']['source'] == 'bookkeeping_pass_streak_penalty'
