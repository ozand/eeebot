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


def test_terminal_selfevo_lane_is_retired_before_continuing_active_analyze_lane(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    (state_root / 'self_evolution' / 'failure_learning').mkdir(parents=True)
    (goals / 'current.json').write_text(json.dumps({
        'current_task_id': 'analyze-last-failed-candidate',
        'tasks': [
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'active'},
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
        ],
    }), encoding='utf-8')
    failure_learning_path = state_root / 'self_evolution' / 'failure_learning' / 'latest.json'
    failure_learning_path.write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-stale',
        'failed_commit': 'cafebabe',
        'health_reasons': ['stale_report'],
    }), encoding='utf-8')
    stale_time = time.time() - 3 * 3600
    os.utime(failure_learning_path, (stale_time, stale_time))
    (state_root / 'self_evolution' / 'runtime' / 'latest_issue_lifecycle.json').write_text(json.dumps({
        'schema_version': 'autoevolve-issue-lifecycle-v1',
        'status': 'terminal_merged',
        'github_issue_state': 'CLOSED',
        'issue_number': 61,
        'pr_number': 62,
        'selfevo_branch': 'fix/issue-61-analyze-last-failed-candidate',
        'selfevo_issue': {'number': 61, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
        'retry_allowed': False,
        'source_task_id': 'analyze-last-failed-candidate',
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-retire-analyze',
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
    assert plan['feedback_decision']['selection_source'] == 'feedback_terminal_selfevo_retire'
    assert plan['feedback_decision']['selected_task_id'] == 'record-reward'
    assert plan['feedback_decision']['terminal_selfevo_issue']['terminal_status'] == 'terminal_merged'
    assert plan['feedback_decision']['terminal_selfevo_issue']['selfevo_issue']['number'] == 61
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


def test_stale_complete_lane_record_reward_revives_failure_learning(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    failure_dir = state_root / 'self_evolution' / 'failure_learning'
    failure_dir.mkdir(parents=True)
    (failure_dir / 'latest.json').write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-stale-live',
        'failed_commit': 'badc0ffee',
        'health_reasons': ['stale_report'],
    }), encoding='utf-8')
    (goals / 'current.json').write_text(json.dumps({
        'current_task_id': 'record-reward',
        'tasks': [
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak', 'status': 'done'},
            {'task_id': 'materialize-pass-streak-improvement', 'title': 'Materialize improvement', 'status': 'done'},
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'},
        ],
        'feedback_decision': {
            'mode': 'complete_active_lane',
            'current_task_id': 'materialize-pass-streak-improvement',
            'selected_task_id': 'record-reward',
            'selection_source': 'feedback_complete_active_lane',
        },
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-stale-live-repair',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.2}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.2,
        feedback_decision=None,
        goals_dir=goals,
    )

    assert plan['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['feedback_decision']['mode'] == 'stale_complete_lane_record_reward_repair'
    assert plan['feedback_decision']['selection_source'] == 'feedback_complete_active_lane_to_failure_learning'
    assert plan['feedback_decision']['selected_task_id'] == 'analyze-last-failed-candidate'


def test_terminal_failure_learning_does_not_resurrect_already_retired_source_task(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    failure_dir = state_root / 'self_evolution' / 'failure_learning'
    failure_dir.mkdir(parents=True)
    (failure_dir / 'latest.json').write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-terminal-resurrection',
        'failed_commit': 'decafbad',
        'health_reasons': ['stale_report'],
        'learning_summary': 'Already-terminal failure-learning source must not be reactivated.',
    }), encoding='utf-8')
    (goals / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'record-reward',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'},
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'done', 'kind': 'review', 'terminal_reason': 'terminal_merged'},
        ],
        'feedback_decision': {
            'mode': 'retire_terminal_selfevo_lane',
            'current_task_id': 'analyze-last-failed-candidate',
            'selected_task_id': 'record-reward',
            'selection_source': 'feedback_terminal_selfevo_retire',
        },
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-terminal-failure-learning-idempotent',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.2}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.2,
        feedback_decision=None,
        goals_dir=goals,
    )

    decision = plan.get('feedback_decision') or {}
    assert plan['current_task_id'] == 'record-reward'
    assert decision.get('mode') != 'fresh_failure_learning_repair'
    assert decision.get('mode') != 'stale_complete_lane_record_reward_repair'
    assert decision.get('selected_task_id') != 'analyze-last-failed-candidate'
    assert all(task.get('task_id') != 'analyze-last-failed-candidate' or task.get('status') == 'done' for task in plan['tasks'])


def test_completed_materialization_does_not_reselect_terminal_failure_learning_task(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    failure_dir = state_root / 'self_evolution' / 'failure_learning'
    failure_dir.mkdir(parents=True)
    (failure_dir / 'latest.json').write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-terminal-materialization',
        'failed_commit': 'decafbad',
        'health_reasons': ['stale_report'],
    }), encoding='utf-8')
    artifact = state_root / 'improvements' / 'materialized-cycle-terminal.json'
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({'task_id': 'materialize-synthesized-improvement'}), encoding='utf-8')
    (goals / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'materialize-synthesized-improvement',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
            {'task_id': 'synthesize-next-improvement-candidate', 'title': 'Synthesize', 'status': 'done'},
            {'task_id': 'materialize-synthesized-improvement', 'title': 'Materialize synthesized', 'status': 'active'},
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'done', 'kind': 'review', 'terminal_reason': 'terminal_merged'},
        ],
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-terminal-materialization-complete',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.2}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.2,
        feedback_decision=None,
        goals_dir=goals,
        materialized_improvement_artifact_path=str(artifact),
    )

    decision = plan.get('feedback_decision') or {}
    assert plan['current_task_id'] == 'record-reward'
    assert decision.get('selected_task_id') == 'record-reward'
    assert decision.get('selection_source') != 'feedback_complete_active_lane_to_failure_learning'
    assert all(task.get('task_id') != 'analyze-last-failed-candidate' or task.get('status') == 'done' for task in plan['tasks'])


def test_repeated_synthesized_materialization_completion_goes_to_reward_accounting(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    artifact = state_root / 'improvements' / 'materialized-cycle-repeat.json'
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({'task_id': 'materialize-synthesized-improvement'}), encoding='utf-8')
    (goals / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'record-reward',
        'feedback_decision': {
            'mode': 'complete_active_lane',
            'current_task_id': 'materialize-synthesized-improvement',
            'selected_task_id': 'record-reward',
            'selection_source': 'feedback_complete_active_lane',
            'artifact_path': str(artifact),
        },
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'},
            {'task_id': 'synthesize-next-improvement-candidate', 'title': 'Synthesize', 'status': 'done'},
            {'task_id': 'materialize-synthesized-improvement', 'title': 'Materialize synthesized', 'status': 'active'},
        ],
        'materialized_improvement_artifact_path': str(artifact),
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-repeat-materialization-completion',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.2}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.2,
        feedback_decision=None,
        goals_dir=goals,
        materialized_improvement_artifact_path=str(artifact),
    )

    decision = plan.get('feedback_decision') or {}
    assert plan['current_task_id'] == 'record-reward'
    assert decision.get('mode') == 'record_reward_after_synthesized_materialization'
    assert decision.get('selection_source') == 'feedback_synthesized_materialization_complete_reward'
    assert decision.get('selected_task_id') == 'record-reward'
    assert decision.get('mode') != 'complete_active_lane'


def test_record_reward_after_completed_synthesized_materialization_confirmation_advances_to_reward_accounting(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    artifact = state_root / 'improvements' / 'materialized-cycle-confirmed.json'
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({'task_id': 'materialize-synthesized-improvement'}), encoding='utf-8')
    (goals / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'record-reward',
        'feedback_decision': {
            'mode': 'complete_active_lane',
            'current_task_id': 'materialize-synthesized-improvement',
            'selected_task_id': 'record-reward',
            'selection_source': 'feedback_complete_active_lane',
            'artifact_path': str(artifact),
        },
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'},
            {'task_id': 'synthesize-next-improvement-candidate', 'title': 'Synthesize', 'status': 'done'},
            {'task_id': 'materialize-synthesized-improvement', 'title': 'Materialize synthesized', 'status': 'done'},
        ],
        'materialized_improvement_artifact_path': str(artifact),
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-record-reward-after-confirmed-materialization',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.2}, 'budget': {}, 'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0}, 'outcome': 'discard'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.2,
        feedback_decision=None,
        goals_dir=goals,
        materialized_improvement_artifact_path=str(artifact),
    )

    decision = plan.get('feedback_decision') or {}
    assert plan['current_task_id'] == 'record-reward'
    assert decision.get('mode') == 'record_reward_after_synthesized_materialization'
    assert decision.get('selection_source') == 'feedback_synthesized_materialization_complete_reward'
    assert decision.get('selected_task_id') == 'record-reward'
    assert decision.get('mode') != 'ambition_escalation_blocked'


def test_terminal_selfevo_issue_outranks_stale_complete_lane_repair_when_current_task_is_record_reward(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    failure_dir = state_root / 'self_evolution' / 'failure_learning'
    failure_dir.mkdir(parents=True)
    failure_path = failure_dir / 'latest.json'
    failure_path.write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-stale-live',
        'failed_commit': 'badc0ffee',
        'health_reasons': ['stale_report'],
    }), encoding='utf-8')
    old_time = time.time() - 7200
    os.utime(failure_path, (old_time, old_time))
    artifact = state_root / 'materialized_improvements' / 'artifact.json'
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{}', encoding='utf-8')
    (goals / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'materialize-pass-streak-improvement',
        'tasks': [
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak', 'status': 'done'},
            {'task_id': 'materialize-pass-streak-improvement', 'title': 'Materialize improvement', 'status': 'active'},
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
        ],
    }), encoding='utf-8')
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

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-terminal-outranks-stale-repair',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.2}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.2,
        feedback_decision=None,
        goals_dir=goals,
        materialized_improvement_artifact_path=str(artifact),
    )

    assert plan['current_task_id'] == 'record-reward'
    assert plan['feedback_decision']['mode'] == 'retire_terminal_selfevo_lane'
    assert plan['feedback_decision']['selection_source'] == 'feedback_terminal_selfevo_retire'
    assert plan['feedback_decision']['selected_task_id'] == 'record-reward'
    assert plan['feedback_decision']['terminal_selfevo_issue']['selfevo_issue']['number'] == 61
    assert all(task.get('task_id') != 'analyze-last-failed-candidate' or task.get('status') == 'done' for task in plan['tasks'])


def test_terminal_selfevo_retirement_is_idempotent_after_record_reward_cycle(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)

    failure_dir = state_root / 'self_evolution' / 'failure_learning'
    failure_dir.mkdir(parents=True)
    (failure_dir / 'latest.json').write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'candidate-idempotent-loop',
        'failed_commit': 'feedface',
        'health_reasons': ['stale_report'],
        'learning_summary': 'Fresh failure learning should not re-retire an already retired terminal lane.',
    }), encoding='utf-8')

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

    (goals / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'record-reward',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'},
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'pending', 'kind': 'review'},
        ],
        'feedback_decision': {
            'mode': 'retire_terminal_selfevo_lane',
            'reason': 'latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate',
            'reward_value': 1.0,
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task_class': 'other',
            'selected_task_id': 'record-reward',
            'selected_task_class': 'reflection',
            'selection_source': 'feedback_terminal_selfevo_retire',
            'selected_task_title': 'Record cycle reward',
            'selected_task_label': 'Record cycle reward [task_id=record-reward]',
            'terminal_selfevo_issue': {
                'terminal_status': 'terminal_merged',
                'selfevo_issue': {'number': 61, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
            },
        },
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-terminal-retirement-idempotent',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.0}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.0,
        feedback_decision=None,
        goals_dir=goals,
    )

    decision = plan.get('feedback_decision') or {}

    assert plan['current_task_id'] == 'record-reward'
    assert plan.get('next_cycle_task_id') != 'analyze-last-failed-candidate'
    assert decision.get('mode') != 'retire_terminal_selfevo_lane'
    assert decision.get('selected_task_id') != 'analyze-last-failed-candidate'


def test_terminal_selfevo_retirement_is_idempotent_when_source_task_is_already_terminal(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)

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

    (goals / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'analyze-last-failed-candidate',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'done', 'kind': 'review', 'terminal_reason': 'terminal_merged'},
        ],
        'feedback_decision': {
            'mode': 'retire_terminal_selfevo_lane',
            'reason': 'latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate',
            'reward_value': 1.0,
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task_class': 'other',
            'selected_task_id': 'record-reward',
            'selected_task_class': 'reflection',
            'selection_source': 'feedback_terminal_selfevo_retire',
            'selected_task_title': 'Record cycle reward',
            'selected_task_label': 'Record cycle reward [task_id=record-reward]',
            'terminal_selfevo_issue': {
                'terminal_status': 'terminal_merged',
                'selfevo_issue': {'number': 61, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
            },
        },
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-terminal-source-already-retired',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.0}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.0,
        feedback_decision=None,
        goals_dir=goals,
    )

    decision = plan.get('feedback_decision') or {}

    assert plan['current_task_id'] == 'record-reward'
    assert decision.get('mode') != 'retire_terminal_selfevo_lane'
    assert decision.get('selection_source') != 'feedback_terminal_selfevo_retire'
    assert all(task.get('task_id') != 'analyze-last-failed-candidate' or task.get('status') == 'done' for task in plan['tasks'])


def test_terminal_selfevo_active_lane_with_continue_decision_is_retired(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
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
    (goals / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'analyze-last-failed-candidate',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'active', 'kind': 'review', 'terminal_reason': 'terminal_merged'},
        ],
        'feedback_decision': {
            'mode': 'continue_active_lane',
            'reason': 'active non-core lane remains the best bounded next step',
            'current_task_id': 'analyze-last-failed-candidate',
            'selected_task_id': 'analyze-last-failed-candidate',
            'selection_source': 'feedback_continue_active_lane',
        },
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-terminal-continue-active',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.2}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.2,
        feedback_decision={'mode': 'continue_active_lane', 'selected_task_id': 'analyze-last-failed-candidate'},
        goals_dir=goals,
    )

    assert plan['current_task_id'] == 'record-reward'
    assert plan['feedback_decision']['mode'] == 'retire_terminal_selfevo_lane'
    assert plan['feedback_decision']['selection_source'] == 'feedback_terminal_selfevo_retire'
    assert all(task.get('task_id') != 'analyze-last-failed-candidate' or task.get('status') == 'done' for task in plan['tasks'])


def test_terminal_selfevo_retirement_is_not_replayed_after_selected_reward_lane(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    runtime_state = tmp_path / 'host-state'
    runtime_dir = runtime_state / 'self_evolution' / 'runtime'
    runtime_dir.mkdir(parents=True)
    (runtime_dir / 'latest_issue_lifecycle.json').write_text(json.dumps({
        'schema_version': 'autoevolve-issue-lifecycle-v1',
        'status': 'terminal_merged',
        'github_issue_state': 'CLOSED',
        'issue_number': 61,
        'selfevo_issue': {'number': 61, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
        'retry_allowed': False,
        'source_task_id': 'analyze-last-failed-candidate',
    }), encoding='utf-8')
    monkeypatch.setenv('NANOBOT_RUNTIME_STATE_ROOT', str(runtime_state))
    artifact_path = state_root / 'improvements' / 'materialized-cycle-live.json'
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(json.dumps({'task_id': 'materialize-synthesized-improvement'}), encoding='utf-8')
    history = goals / 'history'
    history.mkdir()
    for index in range(3):
        (history / f'cycle-record-{index}.json').write_text(json.dumps({
            'schema_version': 'task-history-v1',
            'cycle_id': f'cycle-record-{index}',
            'goal_id': 'goal-bootstrap',
            'result_status': 'PASS',
            'current_task_id': 'record-reward',
            'artifact_paths': [str(artifact_path)],
            'recorded_at_utc': f'2026-04-15T12:0{index}:00Z',
        }), encoding='utf-8')
    (goals / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'record-reward',
        'materialized_improvement_artifact_path': str(artifact_path),
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'active'},
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'done', 'terminal_reason': 'terminal_merged'},
            {'task_id': 'synthesize-next-improvement-candidate', 'title': 'Synthesize', 'status': 'done'},
            {'task_id': 'materialize-synthesized-improvement', 'title': 'Materialize synthesized', 'status': 'done'},
        ],
        'feedback_decision': {
            'mode': 'retire_terminal_selfevo_lane',
            'current_task_id': 'analyze-last-failed-candidate',
            'selected_task_id': 'record-reward',
            'selection_source': 'feedback_terminal_selfevo_retire',
            'terminal_selfevo_issue': {'terminal_status': 'terminal_merged'},
        },
    }), encoding='utf-8')

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-terminal-reward-selected',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        experiment={'reward_signal': {'value': 1.2}, 'budget': {}, 'budget_used': {}, 'outcome': 'discard'},
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        improvement_score=1.2,
        feedback_decision=None,
        goals_dir=goals,
    )

    decision = plan.get('feedback_decision') or {}
    assert plan['current_task_id'] == 'record-reward'
    assert decision.get('mode') != 'retire_terminal_selfevo_lane'
    assert decision.get('selection_source') != 'feedback_terminal_selfevo_retire'


def test_failure_learning_uses_resolved_runtime_state_root(tmp_path: Path, monkeypatch) -> None:
    from nanobot.runtime.coordinator import _latest_failure_learning

    workspace = tmp_path / 'release'
    workspace.mkdir()
    runtime_state = tmp_path / 'host-state'
    failure_dir = runtime_state / 'self_evolution' / 'failure_learning'
    failure_dir.mkdir(parents=True)
    (failure_dir / 'latest.json').write_text(json.dumps({
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': 'host-control-plane-candidate',
        'failed_commit': 'abc123',
    }), encoding='utf-8')
    monkeypatch.setenv('NANOBOT_RUNTIME_STATE_ROOT', str(runtime_state))

    result = _latest_failure_learning(workspace)

    assert result is not None
    assert result['candidate_id'] == 'host-control-plane-candidate'
    assert result['_source_path'] == str(failure_dir / 'latest.json')


def test_terminal_selfevo_issue_uses_resolved_runtime_state_root(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    runtime_state = tmp_path / 'host-state'
    runtime_dir = runtime_state / 'self_evolution' / 'runtime'
    runtime_dir.mkdir(parents=True)
    lifecycle_path = runtime_dir / 'latest_issue_lifecycle.json'
    lifecycle_path.write_text(json.dumps({
        'schema_version': 'autoevolve-issue-lifecycle-v1',
        'status': 'terminal_merged',
        'github_issue_state': 'CLOSED',
        'selfevo_branch': 'fix/issue-261-analyze-last-failed-candidate',
        'selfevo_issue': {'number': 261, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
        'retry_allowed': False,
    }), encoding='utf-8')
    monkeypatch.setenv('NANOBOT_RUNTIME_STATE_ROOT', str(runtime_state))

    result = autoevolve.resolve_terminal_selfevo_issue(workspace=workspace, source_task_id='analyze-last-failed-candidate')

    assert result is not None
    assert result['number'] == 261
    assert result['created'] is False
    assert result['reused_terminal_lane'] is True
    assert result['terminal_status'] == 'terminal_merged'


def test_coordinator_retires_analyze_last_failed_candidate_when_terminal_issue_exists_only_in_runtime_state_root(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / 'workspace'
    state_root = workspace / 'state'
    goals = state_root / 'goals'
    goals.mkdir(parents=True)
    (goals / 'current.json').write_text(json.dumps({
        'current_task_id': 'analyze-last-failed-candidate',
        'tasks': [
            {'task_id': 'analyze-last-failed-candidate', 'title': 'Analyze the last failed self-evolution candidate before retrying mutation', 'status': 'active'},
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
        ],
    }), encoding='utf-8')
    runtime_state = tmp_path / 'host-state'
    runtime_dir = runtime_state / 'self_evolution' / 'runtime'
    runtime_dir.mkdir(parents=True)
    (runtime_dir / 'latest_issue_lifecycle.json').write_text(json.dumps({
        'schema_version': 'autoevolve-issue-lifecycle-v1',
        'status': 'terminal_merged',
        'github_issue_state': 'CLOSED',
        'issue_number': 261,
        'selfevo_branch': 'fix/issue-261-analyze-last-failed-candidate',
        'selfevo_issue': {'number': 261, 'title': 'Analyze the last failed self-evolution candidate before retrying mutation'},
        'retry_allowed': False,
        'source_task_id': 'analyze-last-failed-candidate',
    }), encoding='utf-8')
    monkeypatch.setenv('NANOBOT_RUNTIME_STATE_ROOT', str(runtime_state))

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id='cycle-retire-terminal-selfevo-runtime-root',
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
    assert plan['feedback_decision']['selection_source'] == 'feedback_terminal_selfevo_retire'
    assert plan['feedback_decision']['selected_task_id'] == 'record-reward'
    assert plan['feedback_decision']['terminal_selfevo_issue']['selfevo_issue']['number'] == 261
    assert all(task.get('task_id') != 'analyze-last-failed-candidate' or task.get('status') == 'done' for task in plan['tasks'])
