from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import autoevolve


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


def test_runtime_parity_summary_flags_missing_feedback_decision() -> None:
    summary = autoevolve.runtime_parity_summary(
        local_plan={'current_task_id': 'subagent-verify-materialized-improvement', 'feedback_decision': {'mode': 'execute_queued_revert'}},
        live_plan={'current_task_id': 'record-reward', 'feedback_decision': None},
    )

    assert summary['state'] == 'degraded'
    assert 'feedback_decision_missing_on_live' in summary['reasons']
    assert summary['local_current_task_id'] == 'subagent-verify-materialized-improvement'
    assert summary['live_current_task_id'] == 'record-reward'
