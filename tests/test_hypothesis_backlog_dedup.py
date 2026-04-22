import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_hypothesis_backlog_deduplicates_generated_candidates_against_research_feed(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    history_dir = goals_dir / 'history'
    history_dir.mkdir(parents=True)
    for idx in range(3):
        (history_dir / f'cycle-pass-{idx}.json').write_text(json.dumps({'result_status': 'PASS'}), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    backlog = _read_json(tmp_path / 'state' / 'hypotheses' / 'backlog.json')
    task_ids = [item.get('task_id') for item in backlog['entries']]
    assert task_ids.count('inspect-pass-streak') == 1
