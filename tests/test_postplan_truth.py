import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_postplan_task_identity_propagates_to_report_index_and_outbox(tmp_path: Path):
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
        'generated_candidates': [
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'active', 'kind': 'review'}
        ],
    }
    (goals_dir / 'current.json').write_text(json.dumps(current_payload), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    outbox = _read_json(tmp_path / 'state' / 'outbox' / 'latest.json')
    report_index = _read_json(tmp_path / 'state' / 'outbox' / 'report.index.json')
    assert (outbox.get('experiment') or {}).get('current_task_id') == current.get('current_task_id')
    assert (report_index.get('experiment') or {}).get('current_task_id') == current.get('current_task_id')
    assert (outbox.get('feedback_decision') or {}).get('selected_task_id') == (current.get('feedback_decision') or {}).get('selected_task_id')
    assert (report_index.get('feedback_decision') or {}).get('selected_task_id') == (current.get('feedback_decision') or {}).get('selected_task_id')
