import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_generated_candidates_are_carried_forward_from_recorded_plan(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    current_payload = {
        'schema_version': 'task-plan-v1',
        'current_task_id': 'record-reward',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'},
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'pending', 'kind': 'review'},
        ],
        'generated_candidates': [
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'pending', 'kind': 'review'}
        ],
    }
    (goals_dir / 'current.json').write_text(json.dumps(current_payload), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    assert any(item.get('task_id') == 'inspect-pass-streak' for item in (current.get('generated_candidates') or []))


def test_active_generated_lane_is_inferred_into_generated_candidates_when_explicit_list_is_missing(tmp_path: Path):
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
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'active', 'kind': 'review'},
        ],
        'generated_candidates': [],
    }
    (goals_dir / 'current.json').write_text(json.dumps(current_payload), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    assert any(item.get('task_id') == 'inspect-pass-streak' for item in (current.get('generated_candidates') or []))


def test_retire_goal_artifact_pair_selects_existing_non_bookkeeping_candidate(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    history_dir = goals_dir / 'history'
    history_dir.mkdir(parents=True)
    for idx in range(3):
        (history_dir / f'cycle-pass-{idx}.json').write_text(json.dumps({'result_status': 'PASS', 'goal_id': 'goal-bootstrap', 'current_task_id': 'record-reward'}), encoding='utf-8')

    current_payload = {
        'schema_version': 'task-plan-v1',
        'current_task_id': 'record-reward',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'},
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'pending', 'kind': 'review'},
        ],
        'generated_candidates': [
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'pending', 'kind': 'review'}
        ],
    }
    goals_dir.mkdir(parents=True, exist_ok=True)
    (goals_dir / 'current.json').write_text(json.dumps(current_payload), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    decision = current.get('feedback_decision') or {}
    assert decision.get('mode') == 'retire_goal_artifact_pair'
    assert decision.get('selected_task_id') == 'inspect-pass-streak'
