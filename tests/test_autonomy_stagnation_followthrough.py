from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import autoevolve


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
