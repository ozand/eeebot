import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_retired_record_reward_does_not_mask_fresh_failure_learning_even_with_terminal_selfevo(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 27, 1, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    (goals_dir / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'record-reward',
        'tasks': [{'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'}],
        'feedback_decision': {
            'mode': 'retire_goal_artifact_pair',
            'current_task_id': 'record-reward',
            'retire_goal_artifact_pair': True,
            'selected_task_id': None,
            'selection_source': 'recorded_current_task',
        },
    }), encoding='utf-8')

    learning_dir = tmp_path / 'state' / 'self_evolution' / 'failure_learning'
    learning_dir.mkdir(parents=True, exist_ok=True)
    (learning_dir / 'latest.json').write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-after-retired-record-reward',
        'failed_commit': 'badc0de',
        'health_reasons': ['current_task_drift'],
        'learning_summary': 'Fresh failure after reward-loop retirement; analyze before returning to bookkeeping.',
    }), encoding='utf-8')

    runtime_dir = tmp_path / 'state' / 'self_evolution' / 'runtime'
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / 'latest_issue_lifecycle.json').write_text(json.dumps({
        'status': 'terminal_merged',
        'github_issue_state': 'CLOSED',
        'selfevo_branch': 'fix/issue-19-analyze-last-failed-candidate',
        'selfevo_issue': {'number': 19, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
        'retry_allowed': False,
    }), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=15)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    assert current['current_task_id'] == 'analyze-last-failed-candidate'
    assert current['feedback_decision']['mode'] == 'fresh_failure_learning_after_reward_retirement'
    assert current['feedback_decision']['selected_task_id'] == 'analyze-last-failed-candidate'

    report = _read_json(sorted((tmp_path / 'state' / 'reports').glob('evolution-*.json'))[-1])
    assert report['current_task_id'] == 'analyze-last-failed-candidate'
    assert report['feedback_decision']['selection_source'] == 'feedback_fresh_failure_learning_after_reward_retirement'
