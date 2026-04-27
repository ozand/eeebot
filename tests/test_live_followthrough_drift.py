import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import _build_task_plan_snapshot, run_self_evolving_cycle


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


def test_terminal_selfevo_issue_outranks_stale_complete_lane_repair_when_current_task_is_record_reward(tmp_path: Path, monkeypatch):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 27, 2, 30, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 120}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    (goals_dir / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'materialize-pass-streak-improvement',
        'tasks': [
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak', 'status': 'done'},
            {'task_id': 'materialize-pass-streak-improvement', 'title': 'Materialize improvement', 'status': 'active'},
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
        ],
        'feedback_decision': {
            'mode': 'complete_active_lane',
            'current_task_id': 'materialize-pass-streak-improvement',
            'selected_task_id': 'record-reward',
            'selection_source': 'feedback_complete_active_lane',
        },
    }), encoding='utf-8')

    learning_dir = tmp_path / 'state' / 'self_evolution' / 'failure_learning'
    learning_dir.mkdir(parents=True, exist_ok=True)
    failure_path = learning_dir / 'latest.json'
    failure_path.write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-stale-live',
        'failed_commit': 'deadbeef',
        'health_reasons': ['stale_report'],
    }), encoding='utf-8')
    old_time = time.time() - 7200
    os.utime(failure_path, (old_time, old_time))

    runtime_state = tmp_path / 'host-state'
    runtime_dir = runtime_state / 'self_evolution' / 'runtime'
    runtime_dir.mkdir(parents=True)
    (runtime_dir / 'latest_issue_lifecycle.json').write_text(json.dumps({
        'schema_version': 'autoevolve-issue-lifecycle-v1',
        'status': 'terminal_merged',
        'github_issue_state': 'CLOSED',
        'issue_number': 61,
        'selfevo_branch': 'fix/issue-61-analyze-last-failed-candidate',
        'selfevo_issue': {'number': 61, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
        'retry_allowed': False,
        'source_task_id': 'analyze-last-failed-candidate',
    }), encoding='utf-8')
    monkeypatch.setenv('NANOBOT_RUNTIME_STATE_ROOT', str(runtime_state))

    artifact = goals_dir.parent / 'materialized_improvements' / 'artifact.json'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text('{}', encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=tmp_path,
        cycle_id='cycle-terminal-outranks-stale-repair',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.2}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard'},
        report_path=tmp_path / 'state' / 'reports' / 'report.json',
        history_path=tmp_path / 'state' / 'goals' / 'history.json',
        improvement_score=1.2,
        feedback_decision=None,
        goals_dir=goals_dir,
        materialized_improvement_artifact_path=str(artifact),
    )

    assert plan['current_task_id'] == 'record-reward'
    assert plan['feedback_decision']['mode'] == 'retire_terminal_selfevo_lane'
    assert plan['feedback_decision']['selection_source'] == 'feedback_terminal_selfevo_retire'
    assert plan['feedback_decision']['selected_task_id'] == 'record-reward'
    assert plan['feedback_decision']['terminal_selfevo_issue']['selfevo_issue']['number'] == 61


def test_stale_subagent_lane_retirement_prefers_fresh_failure_learning_over_record_reward(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 27, 2, 30, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 120}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    (goals_dir / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'subagent-verify-materialized-improvement',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
            {'task_id': 'subagent-verify-materialized-improvement', 'title': 'Use one bounded subagent-assisted review to verify the materialized improvement artifact', 'status': 'active', 'kind': 'review'},
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'pending', 'kind': 'review'},
        ],
        'feedback_decision': {
            'mode': 'handoff_to_next_candidate',
            'current_task_id': 'materialize-pass-streak-improvement',
            'selected_task_id': 'subagent-verify-materialized-improvement',
            'selection_source': 'feedback_post_completion_handoff',
        },
    }), encoding='utf-8')

    learning_dir = tmp_path / 'state' / 'self_evolution' / 'failure_learning'
    learning_dir.mkdir(parents=True, exist_ok=True)
    (learning_dir / 'latest.json').write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-live-bad',
        'failed_commit': 'deadbeef',
        'health_reasons': ['stale_report'],
        'learning_summary': 'Fresh failure learning remains the stronger repair lane.',
    }), encoding='utf-8')

    runtime_dir = tmp_path / 'state' / 'self_evolution' / 'runtime'
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / 'latest_issue_lifecycle.json').write_text(json.dumps({
        'status': 'terminal_merged',
        'github_issue_state': 'CLOSED',
        'selfevo_branch': 'fix/issue-42-analyze-last-failed-candidate',
        'selfevo_issue': {'number': 42, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
        'retry_allowed': False,
    }), encoding='utf-8')

    # The discarded/no-material-change experiment is the live condition that used
    # to retire the subagent lane by reactivating record-reward.
    experiments_dir = tmp_path / 'state' / 'experiments'
    experiments_dir.mkdir(parents=True, exist_ok=True)
    (experiments_dir / 'latest.json').write_text(json.dumps({
        'outcome': 'discard',
        'revert_status': 'skipped_no_material_change',
    }), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=15)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    current = _read_json(tmp_path / 'state' / 'goals' / 'current.json')
    assert current['current_task_id'] == 'analyze-last-failed-candidate'
    assert current['feedback_decision']['selected_task_id'] == 'analyze-last-failed-candidate'
    assert current['feedback_decision']['selection_source'] == 'feedback_stale_subagent_retire_to_failure_learning'

    report = _read_json(sorted((tmp_path / 'state' / 'reports').glob('evolution-*.json'))[-1])
    assert report['current_task_id'] == 'analyze-last-failed-candidate'
    assert report['feedback_decision']['mode'] == 'fresh_failure_learning_repair'
