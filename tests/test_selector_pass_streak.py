import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_repeated_pass_streak_selects_generated_review_candidate_next_cycle(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    history_dir = goals_dir / 'history'
    history_dir.mkdir(parents=True)
    for idx in range(3):
        (history_dir / f'cycle-pass-{idx}.json').write_text(json.dumps({'result_status': 'PASS', 'goal_id': 'goal-bootstrap', 'goal': {'follow_through': {'artifact_paths': ['/tmp/shared-artifact.txt']}}}), encoding='utf-8')

    current_payload = {
        'schema_version': 'task-plan-v1',
        'current_task_id': 'record-reward',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'},
            {'task_id': 'run-bounded-turn', 'title': 'Run bounded turn', 'status': 'pending'},
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'pending', 'kind': 'review'},
        ],
    }
    goals_dir.mkdir(parents=True, exist_ok=True)
    (goals_dir / 'current.json').write_text(json.dumps(current_payload), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    assert current['current_task_id'] == 'inspect-pass-streak'
    assert current['feedback_decision']['selected_task_id'] == 'inspect-pass-streak'
