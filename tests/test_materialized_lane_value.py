import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_materialized_lane_gets_reward_bonus_readiness_and_deeper_budget(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    current_payload = {
        'schema_version': 'task-plan-v1',
        'current_task_id': 'materialize-pass-streak-improvement',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'done', 'kind': 'review'},
            {'task_id': 'materialize-pass-streak-improvement', 'title': 'Materialize one concrete bounded improvement from the repeated PASS insight', 'status': 'active', 'kind': 'execution'},
        ],
        'generated_candidates': [
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'done', 'kind': 'review'},
            {'task_id': 'materialize-pass-streak-improvement', 'title': 'Materialize one concrete bounded improvement from the repeated PASS insight', 'status': 'active', 'kind': 'execution'},
        ]
    }
    (goals_dir / 'current.json').write_text(json.dumps(current_payload), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    report = _read_json(sorted((tmp_path / 'state' / 'reports').glob('evolution-*.json'))[-1])
    summary = _read_json(tmp_path / 'state' / 'control_plane' / 'current_summary.json')
    assert report['reward_signal']['value'] >= 1.2
    assert report['reward_signal']['source'] == 'materialized_improvement_artifact'
    assert report['budget_used']['tool_calls'] >= 2
    assert report['review_status'] == 'reviewed'
    assert report['decision'] == 'accept'
    assert summary['experiment']['review_status'] == 'reviewed'
    assert summary['experiment']['decision'] == 'accept'
    latest = _read_json(tmp_path / 'state' / 'promotions' / 'latest.json')
    candidate_id = latest['promotion_candidate_id']
    assert latest['decision_record'].endswith(f'decisions/{candidate_id}.json')
    assert latest['accepted_record'].endswith(f'accepted/{candidate_id}.json')
