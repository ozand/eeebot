from __future__ import annotations

import json
import os
import time
from pathlib import Path

from nanobot.runtime import autoevolve
from nanobot.runtime.coordinator import (
    _build_task_plan_snapshot,
    _subagent_lane_health,
)


def test_terminal_noop_retire_decision_advances_repeated_subagent_lane(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    (goals / 'current.json').write_text(json.dumps({
        'current_task_id': 'subagent-verify-materialized-improvement',
        'tasks': [
            {'task_id': 'subagent-verify-materialized-improvement', 'title': 'Verify materialized improvement', 'status': 'active'},
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
        ],
        'feedback_decision': {'mode': 'handoff_to_next_candidate', 'selected_task_id': 'subagent-verify-materialized-improvement'},
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({
        'status': 'terminal_noop',
        'selfevo_issue': {'number': 13},
        'recommended_next_action': 'select a new bounded mutation or close the already terminal task',
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-noop',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.0}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard', 'revert_status': 'skipped_no_material_change'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.0,
        feedback_decision={'mode': 'handoff_to_next_candidate', 'selected_task_id': 'subagent-verify-materialized-improvement'},
        goals_dir=goals,
    )

    assert plan['current_task_id'] != 'subagent-verify-materialized-improvement'
    assert plan['feedback_decision']['mode'] == 'retire_terminal_noop_lane'
    assert plan['feedback_decision']['selection_source'] == 'feedback_terminal_noop_retire'


def test_terminal_selfevo_issue_reuse_skips_duplicate_issue_creation(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state' / 'self_evolution' / 'runtime'
    state_root.mkdir(parents=True)
    (state_root / 'latest_noop.json').write_text(json.dumps({
        'status': 'terminal_noop',
        'retry_allowed': False,
        'selfevo_branch': 'fix/issue-20-analyze-last-failed-candidate',
        'selfevo_issue': {'number': 20, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
    }), encoding='utf-8')

    def _fail_if_called(*args, **kwargs):
        raise AssertionError('gh should not be called when a terminal selfevo lane is already recorded')

    monkeypatch.setattr(autoevolve.subprocess, 'run', _fail_if_called)

    issue = autoevolve.ensure_selfevo_issue(
        repo='ozand/eeebot-self-evolving',
        title='Analyze the last failed self-evolution candidate before retrying mutation',
        body='duplicate guard test',
        workspace=workspace,
        source_task_id='analyze-last-failed-candidate',
    )

    assert issue['number'] == 20
    assert issue['created'] is False
    assert issue['reused_terminal_lane'] is True
    assert issue['terminal_status'] == 'terminal_noop'


def test_terminal_selfevo_lane_is_retired_when_latest_issue_lifecycle_closed(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    (state_root / 'self_evolution' / 'failure_learning').mkdir(parents=True)
    (goals / 'current.json').write_text(json.dumps({
        'current_task_id': 'record-reward',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'},
        ],
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'failure_learning' / 'latest.json').write_text(json.dumps({
        'candidate_id': 'candidate-1',
        'failed_commit': 'deadbeef',
        'health_reasons': ['no_material_change'],
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_issue_lifecycle.json').write_text(json.dumps({
        'status': 'terminal_merged',
        'github_issue_state': 'CLOSED',
        'selfevo_branch': 'fix/issue-19-analyze-last-failed-candidate',
        'selfevo_issue': {'number': 19, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
        'retry_allowed': False,
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-retire',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.0}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard', 'revert_status': 'skipped_no_material_change'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.0,
        feedback_decision=None,
        goals_dir=goals,
    )

    assert plan['current_task_id'] == 'record-reward'
    assert plan['feedback_decision']['mode'] == 'retire_terminal_selfevo_lane'
    assert plan['feedback_decision']['selected_task_id'] == 'record-reward'
    assert all(task.get('task_id') != 'analyze-last-failed-candidate' or task.get('status') == 'done' for task in plan['tasks'])


def test_subagent_lane_health_marks_stale_queued_request(tmp_path: Path) -> None:
    state_root = tmp_path / 'state'
    req_dir = state_root / 'subagents' / 'requests'
    req_dir.mkdir(parents=True)
    req = req_dir / 'request-cycle-old.json'
    req.write_text(json.dumps({'request_status': 'queued', 'task_id': 'subagent-verify-materialized-improvement'}), encoding='utf-8')
    old = time.time() - 3 * 3600
    req.touch()
    req.chmod(0o644)
    import os
    os.utime(req, (old, old))

    health = _subagent_lane_health(state_root=state_root, current_task_id='subagent-verify-materialized-improvement', stale_after_seconds=3600)

    assert health['state'] == 'stale'
    assert health['stale_request_count'] == 1
    assert health['recommended_action'] == 'retire_or_block_stale_subagent_lane'


def test_issue_lifecycle_does_not_claim_closed_when_github_issue_open(tmp_path: Path) -> None:
    record = autoevolve.write_issue_lifecycle_status(
        workspace=tmp_path / 'workspace',
        selfevo_issue={'number': 14, 'title': 'Inspect repeated PASS streak'},
        selfevo_branch='chore/issue-14-inspect-pass-streak',
        pr={'number': 15, 'state': 'MERGED', 'merged': True},
        action='closed_after_merge',
        github_issue_state='OPEN',
    )

    assert record['status'] == 'terminal_merged_issue_still_open'
    assert record['linked_issue_action'] == 'still_open_after_merge'
    assert record['github_issue_state'] == 'OPEN'
    assert record['retry_allowed'] is True


def test_runtime_parity_summary_classifies_legacy_reward_loop() -> None:
    summary = autoevolve.runtime_parity_summary(
        local_plan={'current_task_id': 'subagent-verify-materialized-improvement', 'feedback_decision': {'mode': 'handoff_to_next_candidate'}},
        live_plan={'selected_tasks': 'Record cycle reward [task_id=record-reward]', 'task_selection_source': 'recorded_current_task', 'feedback_decision': None},
        live_artifacts={'hypotheses_backlog': False, 'credits_latest': False, 'control_plane_current_summary': False, 'self_evolution_current_state': False},
    )

    assert summary['state'] == 'legacy_reward_loop'
    assert 'live_feedback_decision_missing' in summary['reasons']
    assert 'live_hadi_artifacts_missing' in summary['reasons']
    assert summary['missing_live_artifacts'] == ['hypotheses_backlog', 'credits_latest', 'control_plane_current_summary', 'self_evolution_current_state']


def test_complete_active_lane_prefers_failure_learning_over_record_reward(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    failure_dir = state_root / 'self_evolution' / 'failure_learning'
    failure_dir.mkdir(parents=True)
    artifact = state_root / 'materialized_improvements' / 'artifact.json'
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{}', encoding='utf-8')
    failure_path = failure_dir / 'latest.json'
    failure_path.write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-ambitious',
        'failed_commit': 'deadbeef',
        'health_reasons': ['stale_report'],
    }), encoding='utf-8')
    old_time = time.time() - 7200
    os.utime(failure_path, (old_time, old_time))
    (goals / 'current.json').write_text(json.dumps({
        'current_task_id': 'materialize-pass-streak-improvement',
        'tasks': [
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak', 'status': 'done'},
            {'task_id': 'materialize-pass-streak-improvement', 'title': 'Materialize one bounded improvement', 'status': 'active'},
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
        ],
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-complete-active',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.2}, 'budget': {}, 'budget_used': {}, 'outcome': 'keep'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.2,
        feedback_decision=None,
        goals_dir=goals,
        materialized_improvement_artifact_path=str(artifact),
    )

    assert plan['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['feedback_decision']['mode'] == 'complete_active_lane'
    assert plan['feedback_decision']['selection_source'] == 'feedback_complete_active_lane_to_failure_learning'
    assert plan['feedback_decision']['selected_task_id'] == 'analyze-last-failed-candidate'
