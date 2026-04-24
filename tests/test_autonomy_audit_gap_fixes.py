from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import autoevolve
from nanobot.runtime.coordinator import _build_experiment_snapshot, _write_credits_ledger


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def test_noop_selfevo_export_writes_terminal_artifact_and_skips_pr(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    export_result = {
        'ok': True,
        'stdout_tail': 'exported-noop /tmp/selfevo main\n',
        'publish_remote_branch': 'chore/issue-13-subagent-verify-materialized-improvement',
        'publish_repo': 'ozand/eeebot-self-evolving',
    }

    terminal = autoevolve.write_noop_export_status(
        workspace=workspace,
        export_result=export_result,
        selfevo_issue={'number': 13, 'title': 'Use one bounded subagent-assisted review'},
        selfevo_branch='chore/issue-13-subagent-verify-materialized-improvement',
        reason='exported_noop',
    )

    assert terminal['status'] == 'terminal_noop'
    assert terminal['ok'] is True
    assert terminal['pr_creation_allowed'] is False
    assert terminal['reason'] == 'exported_noop'
    assert 'skip PR creation' in terminal['recommended_next_action']
    persisted = _read_json(workspace / 'state' / 'self_evolution' / 'runtime' / 'latest_noop.json')
    assert persisted['selfevo_issue']['number'] == 13
    state = _read_json(workspace / 'state' / 'self_evolution' / 'current_state.json')
    assert state['last_noop']['status'] == 'terminal_noop'


def test_selfevo_already_merged_pr_marks_issue_lifecycle_terminal(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'

    record = autoevolve.write_issue_lifecycle_status(
        workspace=workspace,
        selfevo_issue={'number': 14, 'title': 'Inspect repeated PASS streak'},
        selfevo_branch='chore/issue-14-inspect-pass-streak',
        pr={'number': 15, 'state': 'MERGED', 'merged': True, 'url': 'https://github.com/ozand/eeebot-self-evolving/pull/15'},
        action='closed_after_merge',
    )

    assert record['status'] == 'terminal_merged'
    assert record['issue_number'] == 14
    assert record['pr_number'] == 15
    assert record['retry_allowed'] is False
    persisted = _read_json(workspace / 'state' / 'self_evolution' / 'runtime' / 'latest_issue_lifecycle.json')
    assert persisted['linked_issue_action'] == 'closed_after_merge'
    state = _read_json(workspace / 'state' / 'self_evolution' / 'current_state.json')
    assert state['last_issue_lifecycle']['status'] == 'terminal_merged'


def test_discard_equal_metric_revert_terminalizes_as_no_material_change(tmp_path: Path) -> None:
    experiment = _build_experiment_snapshot(
        experiment_id='exp-no-change',
        cycle_id='cycle-1',
        goal_id='goal-bootstrap',
        result_status='PASS',
        approval_gate_state='fresh',
        next_hint='continue',
        selected_tasks='Verify materialized artifact [task_id=subagent-verify-materialized-improvement]',
        task_selection_source='feedback_discard_revert_followthrough',
        cycle_started_utc='2026-04-24T00:00:00Z',
        cycle_ended_utc='2026-04-24T00:01:00Z',
        report_path=tmp_path / 'report.json',
        history_path=tmp_path / 'history.json',
        outbox_path=tmp_path / 'outbox.json',
        promotion_candidate_id=None,
        review_status=None,
        decision=None,
        reward_signal={'value': 1.2, 'source': 'materialized_improvement_artifact', 'result_status': 'PASS'},
        feedback_decision={'selected_task_id': 'subagent-verify-materialized-improvement'},
        previous_experiment={'metric_current': 2.0, 'metric_frontier': 2.0},
        contract_path=tmp_path / 'contract.json',
        revert_path=tmp_path / 'revert.json',
    )

    assert experiment['outcome'] == 'discard'
    assert experiment['revert_required'] is True
    assert experiment['revert_status'] == 'skipped_no_material_change'
    assert experiment['revert']['terminal'] is True
    assert experiment['revert']['reason'] == 'discarded telemetry did not produce a material file change to revert'


def test_credits_zeroed_for_discarded_experiment_with_unresolved_or_noop_revert(tmp_path: Path) -> None:
    credits = _write_credits_ledger(
        credits_dir=tmp_path / 'credits',
        cycle_id='cycle-discard',
        goal_id='goal-bootstrap',
        result_status='PASS',
        reward_signal={'value': 1.2, 'source': 'materialized_improvement_artifact', 'result_status': 'PASS'},
        budget_used={'requests': 1, 'tool_calls': 1},
        recorded_at_utc='2026-04-24T00:01:00Z',
        experiment={'outcome': 'discard', 'revert_required': True, 'revert_status': 'queued'},
    )

    assert credits['delta'] == 0.0
    assert credits['balance'] == 0.0
    assert credits['reward_gate']['status'] == 'suppressed'
    assert credits['reward_gate']['reason'] == 'discarded_experiment_unresolved_revert'


def test_runtime_parity_summary_flags_missing_feedback_decision() -> None:
    summary = autoevolve.runtime_parity_summary(
        local_plan={'current_task_id': 'subagent-verify-materialized-improvement', 'feedback_decision': {'mode': 'execute_queued_revert'}},
        live_plan={'current_task_id': 'record-reward', 'feedback_decision': None},
    )

    assert summary['state'] == 'degraded'
    assert 'feedback_decision_missing_on_live' in summary['reasons']
    assert summary['local_current_task_id'] == 'subagent-verify-materialized-improvement'
    assert summary['live_current_task_id'] == 'record-reward'
