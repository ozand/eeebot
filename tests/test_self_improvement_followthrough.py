import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import _derive_feedback_decision, run_self_evolving_cycle
from nanobot.runtime.autoevolve import write_candidate_blocked_status


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding='utf-8')


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def _fresh_gate(workspace: Path):
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    _write_json(workspace / 'state' / 'approvals' / 'apply.ok', {'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60})
    return expires_at


def test_repeated_pass_noncore_lane_retires_instead_of_continue_active_lane(tmp_path: Path):
    goals_dir = tmp_path / 'state' / 'goals'
    plan = {
        'schema_version': 'task-plan-v1',
        'current_task_id': 'analyze-last-failed-candidate',
        'tasks': [
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'active', 'kind': 'review'},
            {'task_id': 'subagent-verify-materialized-improvement', 'title': 'Use one bounded subagent-assisted review to verify the materialized improvement artifact', 'status': 'pending', 'kind': 'review'},
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'pending', 'kind': 'review'},
        ],
        'reward_signal': {'value': 1.2, 'source': 'materialized_improvement_artifact'},
    }
    _write_json(goals_dir / 'current.json', plan)
    for idx in range(3):
        _write_json(goals_dir / 'history' / f'cycle-pass-{idx}.json', {
            'result_status': 'PASS',
            'goal_id': 'goal-bootstrap',
            'current_task_id': 'analyze-last-failed-candidate',
        })

    decision = _derive_feedback_decision(plan, goals_dir)

    assert decision is not None
    assert decision['mode'] == 'retire_goal_artifact_pair'
    assert decision['selected_task_id'] != 'analyze-last-failed-candidate'
    assert decision['selection_source'] == 'feedback_pass_streak_switch'
    assert decision['retire_goal_artifact_pair'] is True


def test_discard_revert_queued_forces_followthrough_away_from_discarded_lane(tmp_path: Path):
    expires_at = _fresh_gate(tmp_path)
    goals_dir = tmp_path / 'state' / 'goals'
    _write_json(goals_dir / 'current.json', {
        'schema_version': 'task-plan-v1',
        'current_task_id': 'analyze-last-failed-candidate',
        'tasks': [
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'active', 'kind': 'review'},
            {'task_id': 'subagent-verify-materialized-improvement', 'title': 'Use one bounded subagent-assisted review to verify the materialized improvement artifact', 'status': 'pending', 'kind': 'review'},
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
        ],
        'reward_signal': {'value': 1.2, 'source': 'materialized_improvement_artifact'},
    })
    _write_json(tmp_path / 'state' / 'experiments' / 'latest.json', {
        'experiment_id': 'experiment-old',
        'current_task_id': 'analyze-last-failed-candidate',
        'outcome': 'discard',
        'revert_required': True,
        'revert_status': 'queued',
        'metric_current': 1.2,
        'metric_frontier': 2.0,
        'result_status': 'PASS',
    })

    execute = AsyncMock(return_value='agent completed bounded work')
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=expires_at - timedelta(minutes=30)))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    decision = current.get('feedback_decision') or {}
    assert decision.get('mode') == 'execute_queued_revert'
    assert decision.get('selected_task_id') != 'analyze-last-failed-candidate'
    assert current.get('current_task_id') != 'analyze-last-failed-candidate'


def test_stale_candidate_blocked_status_is_durable_and_marks_latest_candidate_stale(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    candidate = {
        'schema_version': 'autoevolve-candidate-v1',
        'candidate_id': 'candidate-stale',
        'commit': 'abc123',
        'remote_name': 'origin',
        'branch': 'main',
        'remote_head': 'def456',
        'remote_commit_visible': False,
        'clean_worktree': True,
    }

    blocked = write_candidate_blocked_status(workspace, candidate, 'remote_commit_not_visible')

    assert blocked['status'] == 'blocked'
    assert blocked['reason'] == 'remote_commit_not_visible'
    assert blocked['stale_candidate'] is True
    assert 'regenerate candidate' in blocked['recommended_next_action']
    latest_blocked = _read_json(workspace / 'state' / 'self_evolution' / 'runtime' / 'latest_blocked.json')
    latest_candidate = _read_json(workspace / 'state' / 'self_evolution' / 'candidates' / 'latest.json')
    current_state = _read_json(workspace / 'state' / 'self_evolution' / 'current_state.json')
    assert latest_blocked['candidate_id'] == 'candidate-stale'
    assert latest_candidate['status'] == 'stale'
    assert latest_candidate['stale_reason'] == 'remote_commit_not_visible'
    assert current_state['current_candidate']['status'] == 'stale'
