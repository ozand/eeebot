from __future__ import annotations

import json
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
