from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from nanobot_ops_dashboard import app as dashboard_app
from nanobot_ops_dashboard.app import create_app, _dashboard_runtime_parity, _selected_hypothesis_terminal_evidence, _material_progress_summary, _approval_snapshot, _autonomy_verdict, _ambition_utilization_verdict, _experiment_snapshot_from_payload, _discover_subagent_requests
from nanobot_ops_dashboard.config import DashboardConfig
from nanobot_ops_dashboard.storage import init_db, insert_collection, upsert_event


def _call_json(app, path: str) -> dict:
    captured = {}

    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = headers

    environ = {}
    setup_testing_defaults(environ)
    environ['PATH_INFO'] = path
    environ['QUERY_STRING'] = ''
    body = b''.join(app(environ, start_response)).decode('utf-8')
    assert captured['status'].startswith('200'), body
    return json.loads(body)


def _call_app(app, path: str):
    captured = {}

    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = headers

    environ = {}
    setup_testing_defaults(environ)
    environ['PATH_INFO'] = path
    environ['QUERY_STRING'] = ''
    body = b''.join(app(environ, start_response)).decode('utf-8')
    return captured['status'], body


def _seed_hypotheses_backlog(repo_root: Path, *, entry_count: int, selected_id: str, selected_title: str) -> Path:
    backlog = repo_root / 'workspace' / 'state' / 'hypotheses' / 'backlog.json'
    backlog.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    for index in range(1, entry_count + 1):
        entries.append({
            'hypothesis_id': f'hyp-{index}',
            'title': f'Hypothesis {index}',
            'bounded_priority_score': float(entry_count - index + 1),
            'selection_status': 'selected' if index == entry_count else 'candidate',
            'execution_spec': {
                'goal': f'Goal {index}',
                'task': f'Task {index}',
                'acceptance': f'Acceptance {index}',
                'budget': {'limit': index},
            },
            'wsjf': {'score': index},
            'hadi': {
                'hypothesis': f'Hypothesis statement {index}',
                'action': f'Action {index}',
            },
        })
    backlog.write_text(json.dumps({
        'schema_version': 'hypotheses-backlog-v1',
        'model': 'HADI',
        'entries': entries,
        'selected_hypothesis_id': selected_id,
        'selected_hypothesis_title': selected_title,
    }), encoding='utf-8')
    return backlog


def test_material_progress_compacts_recursive_selfevo_lifecycle_evidence() -> None:
    recursive_issue = {
        'number': 82,
        'title': 'Recursive lifecycle evidence',
        'url': 'https://github.com/ozand/eeebot-self-evolving/issues/82',
        'state': 'CLOSED',
    }
    current = recursive_issue
    for depth in range(12):
        nested = {
            'number': 82,
            'title': f'Recursive lifecycle evidence depth {depth}',
            'url': 'https://github.com/ozand/eeebot-self-evolving/issues/82',
            'state': 'CLOSED',
            'selfevo_issue': current,
            'last_issue_lifecycle': {'selfevo_issue': current, 'status': 'terminal_merged'},
            'huge_blob': 'x' * 1000,
        }
        current = nested
    payload = {
        'schema_version': 'material-progress-v1',
        'state': 'proven',
        'healthy_autonomy_allowed': True,
        'proofs': [
            {
                'kind': 'github_selfevo_pr',
                'evidence': {
                    'selfevo_issue': current,
                    'last_issue_lifecycle': {
                        'status': 'terminal_merged',
                        'selfevo_branch': 'fix/issue-82',
                        'selfevo_issue': current,
                        'pr': {'number': 92, 'url': 'https://github.com/ozand/eeebot-self-evolving/pull/92'},
                    },
                },
            }
        ],
    }

    compact = _material_progress_summary(payload)
    encoded = json.dumps(compact)

    assert compact['available'] is True
    issue = compact['proofs'][0]['evidence']['selfevo_issue']
    assert issue == {
        'number': 82,
        'title': 'Recursive lifecycle evidence depth 11',
        'url': 'https://github.com/ozand/eeebot-self-evolving/issues/82',
        'state': 'CLOSED',
    }
    lifecycle = compact['proofs'][0]['evidence']['last_issue_lifecycle']
    assert lifecycle['status'] == 'terminal_merged'
    assert lifecycle['pr_number'] == 92
    assert 'selfevo_issue' in lifecycle
    assert 'selfevo_issue' not in lifecycle['selfevo_issue']
    assert len(encoded) < 2500


def test_approvals_snapshot_omits_raw_recursive_payloads() -> None:
    raw_payload = {
        'current_plan': {
            'feedback_decision': {
                'mode': 'retire_terminal_selfevo_lane',
                'terminal_selfevo_issue': {
                    'status': 'terminal_merged',
                    'selfevo_issue': {
                        'number': 82,
                        'title': 'Recursive issue',
                        'selfevo_issue': {'number': 82, 'title': 'Nested recursive issue'},
                    },
                },
            },
            'selected_tasks': 'Record cycle reward [task_id=record-reward]',
        },
        'material_progress': {
            'proofs': [{'evidence': {'selfevo_issue': {'number': 82, 'selfevo_issue': {'number': 82}}}}]
        },
        'huge': 'x' * 200000,
    }
    row = {
        'id': 1,
        'collected_at': '2026-04-28T20:00:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'current_task': 'Record cycle reward',
        'gate_state': 'fresh',
        'report_source': '/state/reports/evolution.json',
        'outbox_source': '/state/outbox.json',
        'raw_json': json.dumps(raw_payload),
        'plan_history_json': json.dumps([raw_payload] * 5),
        'task_list_json': json.dumps([{'task_id': 'record-reward'}]),
        'approval_gate': json.dumps({'state': 'fresh'}),
    }

    snapshot = _approval_snapshot(row)
    encoded = json.dumps(snapshot)

    assert 'raw_json' not in snapshot
    assert 'plan_history_json' not in snapshot
    assert 'task_list_json' not in snapshot
    assert 'plan_history' not in snapshot['plan_snapshot']
    assert 'terminal_selfevo_issue' in encoded
    assert 'selfevo_issue": {"selfevo_issue' not in encoded
    assert len(encoded) < 5000


def test_dashboard_truth_prefers_current_summary_and_flags_stale_legacy_active_execution(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)
    (project_root / 'control').mkdir(parents=True, exist_ok=True)

    current_summary = {
        'task_plan': {
            'current_task': 'Use one bounded subagent-assisted review to verify the materialized improvement artifact',
            'current_task_id': 'subagent-verify-materialized-improvement',
            'feedback_decision': {'mode': 'execute_queued_revert', 'selection_source': 'feedback_discard_revert_followthrough'},
            'task_selection_source': 'feedback_discard_revert_followthrough',
            'selected_tasks': 'Use one bounded subagent-assisted review [task_id=subagent-verify-materialized-improvement]',
        },
        'blocker_summary': {
            'schema_version': 'blocker-summary-v1',
            'state': 'stagnant',
            'reason': 'terminal no-op state persists',
            'recommended_next_action': 'select a new bounded mutation or close the already terminal task',
            'source': 'workspace_state',
            'current_task_id': 'subagent-verify-materialized-improvement',
        },
        'runtime_source': {'source': 'workspace_state'},
    }
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps(current_summary), encoding='utf-8')
    active_exec_path = project_root / 'control' / 'active_execution.json'
    active_exec_path.write_text(json.dumps({
        'live_task': {'title': 'Record cycle reward', 'repo_root': '/home/ozand/herkoot/Projects/nanobot-ops-dashboard'},
        'has_actually_executing_task': True,
    }), encoding='utf-8')
    old = time.time() - 72 * 3600
    os.utime(active_exec_path, (old, old))

    remote_freshness = {
        'schema_version': 'selfevo-remote-freshness-v1',
        'state': 'stale',
        'remote_ref_stale': True,
        'remote_head': '45f4949',
        'default_branch_head': '2f2804e',
    }
    raw = {
        'current_plan': current_summary['task_plan'],
        'outbox': {'status': 'PASS'},
        'selfevo_remote_freshness': remote_freshness,
    }
    insert_collection(db, {
        'collected_at': '2026-04-24T07:30:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-current.json',
        'outbox_source': '/workspace/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps(raw),
    })
    upsert_event(db, {
        'collected_at': '2026-04-24T07:25:00Z',
        'source': 'repo',
        'event_type': 'cycle',
        'identity_key': 'cycle-1',
        'title': 'goal-bootstrap',
        'status': 'PASS',
        'detail_json': '{}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-24T07:30:00Z',
        'source': 'repo',
        'event_type': 'cycle',
        'identity_key': 'cycle-2',
        'title': 'goal-bootstrap',
        'status': 'PASS',
        'detail_json': '{}',
    })

    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/state',
    )
    app = create_app(cfg)

    analytics = _call_json(app, '/api/analytics')['analytics']
    assert analytics['current_streak']['status'] == 'PASS'
    assert analytics['current_streak']['length'] == 2

    plan = _call_json(app, '/api/plan')
    assert plan['feedback_decision']['mode'] == 'execute_queued_revert'
    assert plan['task_selection_source'] == 'feedback_discard_revert_followthrough'
    assert 'subagent-verify-materialized-improvement' in plan['selected_tasks_text']

    system = _call_json(app, '/api/system')
    control = system['control_plane']
    assert control['current_task'] == current_summary['task_plan']['current_task']
    assert control['current_blocker'] != 'Record cycle reward'
    assert control['blocker_summary'] == current_summary['blocker_summary']
    assert system['blocker_summary'] == current_summary['blocker_summary']
    assert system['selfevo_remote_freshness'] == remote_freshness
    assert control['selfevo_remote_freshness'] == remote_freshness
    assert control['active_execution']['staleness']['state'] == 'stale'
    assert control['active_execution']['legacy_path_reference_detected'] is True


def test_api_system_canonicalizes_stale_outbox_current_blocker_task_truth(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)

    current_summary = {
        'task_plan': {
            'current_task_id': 'analyze-last-failed-candidate',
            'selected_tasks': 'Analyze the last failed self-evolution candidate [task_id=analyze-last-failed-candidate]',
            'selected_task_title': 'Analyze the last failed self-evolution candidate before retrying mutation',
            'task_selection_source': 'generated_from_failure_learning',
            'feedback_decision': {
                'mode': 'handoff_to_next_candidate',
                'selected_task_id': 'analyze-last-failed-candidate',
                'selected_task_title': 'Analyze the last failed self-evolution candidate before retrying mutation',
                'selection_source': 'generated_from_failure_learning',
            },
        },
        'blocker_summary': {
            'schema_version': 'blocker-summary-v1',
            'state': 'stagnant',
            'reason': 'terminal no-op state persists',
            'recommended_next_action': 'select a new bounded mutation or close the already terminal task',
            'source': 'workspace_state',
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task_title': 'Analyze the last failed self-evolution candidate before retrying mutation',
        },
        'runtime_source': {'source': 'workspace_state'},
    }
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps(current_summary), encoding='utf-8')

    insert_collection(db, {
        'collected_at': '2026-04-24T07:30:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-current.json',
        'outbox_source': '/workspace/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({
            'current_plan': {
                'current_task_id': 'record-reward',
                'current_task': 'Record cycle reward',
                'selected_tasks': 'Record cycle reward [task_id=record-reward]',
                'task_selection_source': 'recorded_current_task',
                'feedback_decision': {
                    'mode': 'force_remediation',
                    'selected_task_id': 'record-reward',
                    'selected_task_title': 'Record cycle reward',
                    'selection_source': 'recorded_current_task',
                },
            },
            'outbox': {'status': 'PASS'},
        }),
    })

    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/state',
    )
    control = _call_json(create_app(cfg), '/api/system')['control_plane']
    blocker = control['current_blocker']

    assert blocker['current_task_id'] == current_summary['task_plan']['current_task_id']
    assert blocker['current_task'] == current_summary['task_plan']['current_task_id']
    assert blocker['selected_tasks'] == current_summary['task_plan']['selected_tasks']
    assert blocker['selected_tasks_text'] == current_summary['task_plan']['selected_tasks']
    assert blocker['selected_task_title'] == current_summary['task_plan']['selected_task_title']
    assert blocker['task_selection_source'] == current_summary['task_plan']['task_selection_source']
    assert blocker['stale_outbox_is_secondary'] is True
    assert blocker['stale_outbox_selected_tasks'] == 'Record cycle reward [task_id=record-reward]'
    assert blocker['stale_outbox_task_selection_source'] == 'recorded_current_task'
    assert blocker['task_truth_source'] == 'producer_summary.task_plan'
    assert control['blocker_summary']['current_task_id'] == current_summary['blocker_summary']['current_task_id']


def test_dashboard_current_task_authority_prefers_local_producer_over_legacy_live_reward_loop(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)

    producer_plan = {
        'current_task_id': 'analyze-last-failed-candidate',
        'current_task': 'Analyze the last failed self-evolution candidate before retrying mutation',
        'selected_tasks': 'Analyze the last failed self-evolution candidate [task_id=analyze-last-failed-candidate]',
        'task_selection_source': 'generated_from_failure_learning',
        'feedback_decision': {
            'mode': 'handoff_to_next_candidate',
            'selected_task_id': 'analyze-last-failed-candidate',
            'selected_task_title': 'Analyze the last failed self-evolution candidate before retrying mutation',
            'selection_source': 'generated_from_failure_learning',
        },
    }
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({
        'task_plan': producer_plan,
        'runtime_source': {'source': 'workspace_state'},
    }), encoding='utf-8')

    repo_raw = {
        'current_plan': producer_plan,
        'outbox': {'status': 'PASS'},
    }
    live_raw = {
        'current_plan': {
            'current_task_id': 'record-reward',
            'current_task': 'Record cycle reward',
            'selected_tasks': 'Record cycle reward [task_id=record-reward]',
        },
        'outbox': {'status': 'PASS'},
    }
    for source, raw in (('repo', repo_raw), ('eeepc', live_raw)):
        insert_collection(db, {
            'collected_at': '2026-04-24T07:30:00Z',
            'source': source,
            'status': 'PASS',
            'active_goal': 'goal-bootstrap',
            'approval_gate': None,
            'gate_state': None,
            'report_source': '/workspace/state/reports/evolution-current.json',
            'outbox_source': '/workspace/state/outbox/report.index.json',
            'artifact_paths_json': '[]',
            'promotion_summary': None,
            'promotion_candidate_path': None,
            'promotion_decision_record': None,
            'promotion_accepted_record': None,
            'raw_json': json.dumps(raw),
        })

    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/state',
    )
    app = create_app(cfg)

    system = _call_json(app, '/api/system')
    parity = system['runtime_parity']
    assert parity['state'] == 'legacy_reward_loop'
    assert parity['local_current_task_id'] == 'analyze-last-failed-candidate'
    assert parity['live_current_task_id'] == 'record-reward'
    assert parity['canonical_current_task_id'] == 'analyze-last-failed-candidate'
    assert 'current_task_drift' not in parity['reasons']
    assert 'legacy_live_reward_loop_current_task' in parity['reasons']
    assert system['control_plane']['current_task'] == producer_plan['current_task']

    plan = _call_json(app, '/api/plan')
    assert plan['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['current_task'] == producer_plan['current_task']
    assert plan['task_plan']['current_task_id'] == 'analyze-last-failed-candidate'


def test_dashboard_runtime_parity_trusts_fresh_live_failure_learning_handoff_and_reconciles_plan_and_hypotheses(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel == 'hypotheses/backlog.json':
            continue
        path.write_text('{}', encoding='utf-8')

    _seed_hypotheses_backlog(
        repo_root,
        entry_count=3,
        selected_id='record-reward',
        selected_title='Record cycle reward',
    )
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({
        'task_plan': {
            'current_task_id': 'record-reward',
            'current_task': 'Record cycle reward',
            'selected_tasks': 'Record cycle reward [task_id=record-reward]',
            'task_selection_source': 'recorded_current_task',
            'feedback_decision': {
                'mode': 'force_remediation',
                'selected_task_id': 'record-reward',
                'selected_task_title': 'Record cycle reward',
                'selection_source': 'recorded_current_task',
            },
        },
        'runtime_source': {'source': 'workspace_state'},
    }), encoding='utf-8')

    repo_raw = {
        'current_plan': {
            'current_task_id': 'record-reward',
            'current_task': 'Record cycle reward',
            'selected_tasks': 'Record cycle reward [task_id=record-reward]',
            'task_selection_source': 'recorded_current_task',
            'feedback_decision': {
                'mode': 'force_remediation',
                'selected_task_id': 'record-reward',
                'selected_task_title': 'Record cycle reward',
                'selection_source': 'recorded_current_task',
            },
        },
        'outbox': {'status': 'PASS'},
    }
    live_raw = {
        'current_plan': {
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task': 'Analyze the last failed self-evolution candidate before retrying mutation',
            'selected_tasks': 'Analyze the last failed self-evolution candidate [task_id=analyze-last-failed-candidate]',
            'task_selection_source': 'generated_from_failure_learning',
            'feedback_decision': {
                'mode': 'complete_active_lane',
                'selected_task_id': 'analyze-last-failed-candidate',
                'selected_task_title': 'Analyze the last failed self-evolution candidate before retrying mutation',
                'selection_source': 'feedback_complete_active_lane_to_failure_learning',
            },
        },
        'outbox': {'status': 'PASS'},
    }
    insert_collection(db, {
        'collected_at': '2026-04-24T07:30:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-current.json',
        'outbox_source': '/workspace/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps(repo_raw),
    })
    insert_collection(db, {
        'collected_at': '2026-04-24T07:31:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-current.json',
        'outbox_source': '/workspace/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps(live_raw),
    })

    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/state',
    )
    app = create_app(cfg)

    system = _call_json(app, '/api/system')
    parity = system['runtime_parity']
    assert parity['state'] == 'authority_resolved_with_source_skew'
    assert parity['local_current_task_id'] == 'record-reward'
    assert parity['live_current_task_id'] == 'analyze-last-failed-candidate'
    assert parity['canonical_current_task_id'] == 'analyze-last-failed-candidate'
    assert parity['authority_resolution'] == 'fresh_live_failure_learning_handoff'
    assert 'current_task_drift' not in parity['reasons']
    assert system['autonomy_verdict']['current_task_id'] == 'analyze-last-failed-candidate'
    assert system['control_plane']['current_task_id'] == 'analyze-last-failed-candidate'
    assert system['control_plane']['current_task'] == 'Analyze the last failed self-evolution candidate before retrying mutation'
    assert system['control_plane']['current_task_title'] == 'Analyze the last failed self-evolution candidate before retrying mutation'

    plan = _call_json(app, '/api/plan')
    assert plan['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['task_plan']['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['task_plan']['task_selection_source'] == 'generated_from_failure_learning'
    assert plan['feedback_decision']['selection_source'] == 'feedback_complete_active_lane_to_failure_learning'

    hypotheses = _call_json(app, '/api/hypotheses')
    assert hypotheses['selected_hypothesis_id'] == 'analyze-last-failed-candidate'
    assert hypotheses['selected_hypothesis_title'] == 'Analyze the last failed self-evolution candidate before retrying mutation'
    diagnostics = hypotheses['selected_hypothesis_diagnostics']
    assert diagnostics['selected_hypothesis_id'] == 'analyze-last-failed-candidate'
    assert diagnostics['canonical_runtime_task_id'] == 'analyze-last-failed-candidate'
    assert diagnostics['canonical_runtime_authority_resolution'] == 'fresh_live_failure_learning_handoff'


def test_api_plan_embeds_canonical_current_task_id_when_producer_task_plan_missing(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({
        'task_plan': {
            'current_task_id': None,
            'current_task': None,
            'selected_tasks': 'Analyze the last failed self-evolution candidate [task_id=analyze-last-failed-candidate]',
            'task_selection_source': 'generated_from_failure_learning',
        },
        'runtime_source': {'source': 'workspace_state'},
    }), encoding='utf-8')

    insert_collection(db, {
        'collected_at': '2026-04-24T07:30:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-current.json',
        'outbox_source': '/workspace/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({
            'current_plan': {
                'current_task': 'Analyze the last failed self-evolution candidate before retrying mutation',
                'selected_tasks': 'Analyze the last failed self-evolution candidate [task_id=analyze-last-failed-candidate]',
                'task_selection_source': 'generated_from_failure_learning',
            },
            'outbox': {'status': 'PASS'},
        }),
    })

    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/state',
    )

    plan = _call_json(create_app(cfg), '/api/plan')

    assert plan['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['current_task'] == 'Analyze the last failed self-evolution candidate before retrying mutation'
    assert plan['task_plan']['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['task_plan']['current_task'] == 'Analyze the last failed self-evolution candidate before retrying mutation'
    assert plan['task_plan']['selected_tasks'] == 'Analyze the last failed self-evolution candidate [task_id=analyze-last-failed-candidate]'
    assert plan['task_plan']['task_selection_source'] == 'generated_from_failure_learning'


def test_api_plan_prefers_canonical_task_plan_truth_over_latest_snapshot_current_task(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({
        'task_plan': {
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task': 'Analyze the last failed self-evolution candidate',
            'selected_tasks': 'Analyze the last failed self-evolution candidate [task_id=analyze-last-failed-candidate]',
            'task_selection_source': 'generated_from_failure_learning',
        },
        'runtime_source': {'source': 'workspace_state'},
    }), encoding='utf-8')
    insert_collection(db, {
        'collected_at': '2026-04-24T07:30:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-current.json',
        'outbox_source': '/workspace/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({
            'current_plan': {
                'current_task_id': 'record-reward',
                'current_task': 'record-reward',
                'selected_tasks': 'Record cycle reward [task_id=record-reward]',
                'task_selection_source': 'recorded_current_task',
            },
            'outbox': {'status': 'PASS'},
        }),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    plan = _call_json(create_app(cfg), '/api/plan')

    assert plan['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['task_plan']['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['current_task'] == 'Analyze the last failed self-evolution candidate'
    assert plan['task_plan']['selected_tasks'] == 'Analyze the last failed self-evolution candidate [task_id=analyze-last-failed-candidate]'


def test_api_plan_reconciles_mixed_task_plan_id_with_runtime_canonical_task(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)

    mixed_task_plan = {
        'current_task_id': 'record-reward',
        'current_task': 'record-reward',
        'selected_tasks': 'Record cycle reward [task_id=record-reward]',
        'task_selection_source': 'recorded_current_task',
    }
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({
        'task_plan': mixed_task_plan,
        'runtime_source': {'source': 'workspace_state'},
    }), encoding='utf-8')

    repo_raw = {
        'current_plan': {
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task': 'analyze-last-failed-candidate',
            'selected_tasks': 'Analyze the last failed self-evolution candidate [task_id=analyze-last-failed-candidate]',
            'task_selection_source': 'generated_from_failure_learning',
            'feedback_decision': {'mode': 'handoff_to_next_candidate'},
        },
        'outbox': {'status': 'PASS'},
    }
    live_raw = {
        'current_plan': {
            'current_task_id': 'record-reward',
            'current_task': 'Record cycle reward',
            'selected_tasks': 'Record cycle reward [task_id=record-reward]',
            'task_selection_source': 'recorded_current_task',
        },
        'outbox': {'status': 'PASS'},
    }
    for source, raw in (('repo', repo_raw), ('eeepc', live_raw)):
        insert_collection(db, {
            'collected_at': '2026-04-24T07:30:00Z',
            'source': source,
            'status': 'PASS',
            'active_goal': 'goal-bootstrap',
            'approval_gate': None,
            'gate_state': None,
            'report_source': '/workspace/state/reports/evolution-current.json',
            'outbox_source': '/workspace/state/outbox/report.index.json',
            'artifact_paths_json': '[]',
            'promotion_summary': None,
            'promotion_candidate_path': None,
            'promotion_decision_record': None,
            'promotion_accepted_record': None,
            'raw_json': json.dumps(raw),
        })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    system = _call_json(app, '/api/system')
    plan = _call_json(app, '/api/plan')

    assert system['runtime_parity']['canonical_current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['task_plan']['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['current_task'] == 'analyze-last-failed-candidate'
    assert plan['task_plan']['current_task'] == 'analyze-last-failed-candidate'


def test_api_plan_exposes_next_task_selection_separately_from_current_task(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({
        'task_plan': {
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task': 'analyze-last-failed-candidate',
            'task_selection_source': 'feedback_terminal_selfevo_retire',
            'feedback_decision': {
                'mode': 'retire_terminal_selfevo_lane',
                'current_task_id': 'analyze-last-failed-candidate',
                'selected_task_id': 'record-reward',
                'selected_task_title': 'Record cycle reward',
                'selected_task_label': 'Record cycle reward [task_id=record-reward]',
                'selection_source': 'feedback_terminal_selfevo_retire',
            },
        },
        'runtime_source': {'source': 'workspace_state'},
    }), encoding='utf-8')
    insert_collection(db, {
        'collected_at': '2026-04-26T15:18:40Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-current.json',
        'outbox_source': '/workspace/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({
            'current_plan': {
                'current_task_id': 'analyze-last-failed-candidate',
                'current_task': 'analyze-last-failed-candidate',
                'task_selection_source': 'feedback_terminal_selfevo_retire',
                'feedback_decision': {
                    'mode': 'retire_terminal_selfevo_lane',
                    'current_task_id': 'analyze-last-failed-candidate',
                    'selected_task_id': 'record-reward',
                    'selected_task_title': 'Record cycle reward',
                    'selected_task_label': 'Record cycle reward [task_id=record-reward]',
                    'selection_source': 'feedback_terminal_selfevo_retire',
                },
            },
            'outbox': {'status': 'PASS'},
        }),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    plan = _call_json(create_app(cfg), '/api/plan')

    assert plan['current_task_id'] == 'analyze-last-failed-candidate'
    assert plan['current_task'] == 'analyze-last-failed-candidate'
    assert plan['next_task_id'] == 'record-reward'
    assert plan['next_task_title'] == 'Record cycle reward'
    assert plan['next_task_label'] == 'Record cycle reward [task_id=record-reward]'
    assert plan['next_task_source'] == 'feedback_terminal_selfevo_retire'


def test_api_plan_uses_live_terminal_selfevo_retirement_as_current_task(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({
        'task_plan': {
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task': 'analyze-last-failed-candidate',
            'task_selection_source': 'feedback_complete_active_lane_to_failure_learning',
            'feedback_decision': {
                'mode': 'complete_active_lane',
                'selected_task_id': 'analyze-last-failed-candidate',
                'selection_source': 'feedback_complete_active_lane_to_failure_learning',
            },
        },
    }), encoding='utf-8')
    common = {
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
    }
    insert_collection(db, {
        **common,
        'collected_at': '2026-04-27T23:20:00Z',
        'source': 'repo',
        'report_source': '/workspace/state/reports/local.json',
        'outbox_source': '/workspace/state/outbox/local.index.json',
        'raw_json': json.dumps({
            'current_plan': {
                'current_task_id': 'analyze-last-failed-candidate',
                'current_task': 'analyze-last-failed-candidate',
                'task_selection_source': 'feedback_complete_active_lane_to_failure_learning',
                'feedback_decision': {
                    'mode': 'complete_active_lane',
                    'selected_task_id': 'analyze-last-failed-candidate',
                    'selection_source': 'feedback_complete_active_lane_to_failure_learning',
                },
            },
            'outbox': {'status': 'PASS'},
        }),
    })
    insert_collection(db, {
        **common,
        'collected_at': '2026-04-27T23:32:00Z',
        'source': 'eeepc',
        'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/live.json',
        'outbox_source': '/var/lib/eeepc-agent/self-evolving-agent/state/outbox/report.index.json',
        'raw_json': json.dumps({
            'current_plan': {
                'current_task_id': 'record-reward',
                'current_task': 'record-reward',
                'task_selection_source': 'feedback_terminal_selfevo_retire',
                'feedback_decision': {
                    'mode': 'retire_terminal_selfevo_lane',
                    'current_task_id': 'analyze-last-failed-candidate',
                    'selected_task_id': 'record-reward',
                    'selected_task_title': 'Record cycle reward',
                    'selected_task_label': 'Record cycle reward [task_id=record-reward]',
                    'selection_source': 'feedback_terminal_selfevo_retire',
                    'terminal_selfevo_issue': {'number': 61, 'status': 'terminal_merged'},
                },
            },
            'outbox': {'status': 'PASS'},
        }),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    app = create_app(cfg)
    system = _call_json(app, '/api/system')
    plan = _call_json(app, '/api/plan')

    assert system['runtime_parity']['authority_resolution'] == 'fresh_live_terminal_selfevo_retire'
    assert system['runtime_parity']['canonical_current_task_id'] == 'record-reward'
    assert plan['current_task_id'] == 'record-reward'
    assert plan['feedback_decision']['mode'] == 'retire_terminal_selfevo_lane'
    assert plan['next_task_id'] == 'record-reward'
    assert plan['task_selection_source'] == 'feedback_terminal_selfevo_retire'


def test_api_system_and_plan_adopt_fresh_live_active_lane_when_local_task_is_stale(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({
        'task_plan': {
            'current_task_id': 'record-reward',
            'current_task': 'record-reward',
            'selected_tasks': 'Record cycle reward [task_id=record-reward]',
            'task_selection_source': 'recorded_current_task',
            'feedback_decision': {
                'mode': 'retire_terminal_selfevo_lane',
                'current_task_id': 'analyze-last-failed-candidate',
                'selected_task_id': 'record-reward',
                'selection_source': 'feedback_terminal_selfevo_retire',
                'terminal_selfevo_issue': {'number': 61, 'status': 'terminal_merged'},
            },
        },
        'runtime_source': {'source': 'workspace_state'},
    }), encoding='utf-8')

    insert_collection(db, {
        'collected_at': '2026-04-27T23:20:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/local.json',
        'outbox_source': '/workspace/state/outbox/local.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({
            'current_plan': {
                'current_task_id': 'record-reward',
                'current_task': 'record-reward',
                'selected_tasks': 'Record cycle reward [task_id=record-reward]',
                'task_selection_source': 'feedback_terminal_selfevo_retire',
                'feedback_decision': {
                    'mode': 'retire_terminal_selfevo_lane',
                    'current_task_id': 'analyze-last-failed-candidate',
                    'selected_task_id': 'record-reward',
                    'selected_task_title': 'Record cycle reward',
                    'selected_task_label': 'Record cycle reward [task_id=record-reward]',
                    'selection_source': 'feedback_terminal_selfevo_retire',
                    'terminal_selfevo_issue': {'number': 61, 'status': 'terminal_merged'},
                },
            },
            'outbox': {'status': 'PASS'},
        }),
    })
    insert_collection(db, {
        'collected_at': '2026-04-27T23:32:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/live.json',
        'outbox_source': '/var/lib/eeepc-agent/self-evolving-agent/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({
            'current_plan': {
                'current_task_id': 'synthesize-next-improvement-candidate',
                'current_task': 'synthesize-next-improvement-candidate',
                'selected_tasks': 'Synthesize next improvement candidate [task_id=synthesize-next-improvement-candidate]',
                'task_selection_source': 'feedback_continue_active_lane',
                'feedback_decision': {
                    'mode': 'continue_active_lane',
                    'current_task_id': 'synthesize-next-improvement-candidate',
                    'selected_task_id': 'synthesize-next-improvement-candidate',
                    'selected_task_title': 'Synthesize next improvement candidate',
                    'selected_task_label': 'Synthesize next improvement candidate [task_id=synthesize-next-improvement-candidate]',
                    'selection_source': 'feedback_continue_active_lane',
                },
            },
            'outbox': {'status': 'PASS'},
        }),
    })

    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    system = _call_json(app, '/api/system')
    plan = _call_json(app, '/api/plan')

    assert system['runtime_parity']['state'] == 'authority_resolved_with_source_skew'
    assert system['runtime_parity']['authority_resolution'] == 'fresh_live_active_lane'
    assert system['runtime_parity']['canonical_current_task_id'] == 'synthesize-next-improvement-candidate'
    assert system['runtime_parity']['source_skew']['state'] == 'skewed'
    assert 'current_task_drift' in system['runtime_parity']['source_skew']['reasons']
    assert plan['current_plan_source'] == 'eeepc'
    assert plan['current_task_id'] == 'synthesize-next-improvement-candidate'
    assert plan['task_plan']['current_task_id'] == 'synthesize-next-improvement-candidate'
    assert plan['feedback_decision']['mode'] == 'continue_active_lane'
    assert plan['task_selection_source'] == 'feedback_continue_active_lane'
    assert plan['task_plan']['task_selection_source'] == 'feedback_continue_active_lane'



def test_runtime_parity_trusts_pass_streak_switch_to_reward_even_when_selected_task_is_local_task(tmp_path: Path) -> None:
    repo_root = tmp_path / 'nanobot'
    state_root = repo_root / 'workspace' / 'state'
    for rel in ['hypotheses/backlog.json', 'credits/latest.json', 'control_plane/current_summary.json', 'self_evolution/current_state.json']:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    repo_plan = {'current_task_id': 'inspect-pass-streak'}
    eeepc_plan = {
        'current_task_id': 'record-reward',
        'task_selection_source': 'feedback_pass_streak_switch',
        'feedback_decision': {
            'mode': 'retire_goal_artifact_pair',
            'current_task_id': 'analyze-last-failed-candidate',
            'selected_task_id': 'record-reward',
            'selection_source': 'feedback_pass_streak_switch',
            'retire_goal_artifact_pair': True,
        },
    }

    parity = _dashboard_runtime_parity(repo_plan, eeepc_plan, cfg)

    assert parity['state'] == 'authority_resolved_with_source_skew'
    assert parity['authority_resolution'] == 'fresh_live_pass_streak_switch'
    assert parity['canonical_current_task_id'] == 'record-reward'
    assert 'current_task_drift' not in parity['reasons']


def test_runtime_parity_explains_unreachable_live_authority_when_feedback_is_missing(tmp_path: Path) -> None:
    repo_root = tmp_path / 'nanobot'
    state_root = repo_root / 'workspace' / 'state'
    for rel in ['hypotheses/backlog.json', 'credits/latest.json', 'control_plane/current_summary.json', 'self_evolution/current_state.json']:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='192.168.1.44', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state')
    repo_plan = {
        'current_task_id': 'record-reward',
        'feedback_decision': {
            'mode': 'complete_active_lane',
            'current_task_id': 'materialize-synthesized-improvement',
            'selected_task_id': 'record-reward',
            'selection_source': 'feedback_complete_active_lane',
        },
    }
    eeepc_plan = {
        'status': 'BLOCK',
        'raw_json': json.dumps({
            'outbox': {},
            'goals': {},
            'reachability': {
                'reachable': False,
                'host': '192.168.1.44',
                'port': 22,
                'error': 'ssh timed out',
            },
        }),
    }

    parity = _dashboard_runtime_parity(repo_plan, eeepc_plan, cfg)

    assert parity['state'] == 'degraded'
    assert 'live_authority_unreachable' in parity['reasons']
    assert 'live_feedback_decision_missing' in parity['reasons']
    assert parity['live_authority']['reachable'] is False
    assert parity['live_authority']['host'] == '192.168.1.44'
    assert parity['live_authority']['port'] == 22
    assert parity['next_action'] == 'restore_live_authority_reachability_then_recollect'


def test_api_system_runtime_parity_uses_latest_eeepc_block_row_for_authority_reachability(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    for rel in ['hypotheses/backlog.json', 'credits/latest.json', 'control_plane/current_summary.json', 'self_evolution/current_state.json']:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    repo_plan = {
        'current_task_id': 'record-reward',
        'current_task': 'Record cycle reward',
        'feedback_decision': {
            'mode': 'complete_active_lane',
            'current_task_id': 'materialize-synthesized-improvement',
            'selected_task_id': 'record-reward',
            'selection_source': 'feedback_complete_active_lane',
        },
    }
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({'task_plan': repo_plan}), encoding='utf-8')
    insert_collection(db, {
        'collected_at': '2026-04-29T12:00:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-current.json',
        'outbox_source': '/workspace/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({'current_plan': repo_plan, 'outbox': {'status': 'PASS'}}),
    })
    insert_collection(db, {
        'collected_at': '2026-04-29T12:01:00Z',
        'source': 'eeepc',
        'status': 'BLOCK',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({
            'outbox': {},
            'goals': {},
            'reachability': {
                'reachable': False,
                'ssh_host': 'eeepc',
                'target': 'eeepc',
                'error': 'ssh: connect to host 192.168.1.44 port 22: Connection timed out',
                'recommended_next_action': 'Treat as a control-plane incident; verify eeepc power/network access, then retry collection.',
            },
        }),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    system = _call_json(app, '/api/system')

    parity = system['runtime_parity']
    assert 'live_authority_unreachable' in parity['reasons']
    assert 'live_feedback_decision_missing' in parity['reasons']
    assert parity['live_authority']['reachable'] is False
    assert 'Connection timed out' in parity['live_authority']['error']
    assert parity['next_action'] == 'restore_live_authority_reachability_then_recollect'


def test_closed_terminal_selfevo_evidence_is_historical_not_live_blocker(tmp_path: Path) -> None:
    repo_root = tmp_path / 'nanobot'
    state_root = repo_root / 'workspace' / 'state' / 'self_evolution'
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / 'current_state.json').write_text(json.dumps({
        'selfevo_issue': {
            'number': 82,
            'status': 'terminal_merged',
            'terminal_status': 'terminal_merged',
            'github_issue_state': 'CLOSED',
            'retry_allowed': False,
        },
        'last_pr': {
            'number': 89,
            'created': True,
            'dry_run': False,
        },
    }), encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    issue, pr = _selected_hypothesis_terminal_evidence(cfg)

    assert issue is None
    assert pr is None


def test_open_terminal_selfevo_evidence_remains_live_blocker(tmp_path: Path) -> None:
    repo_root = tmp_path / 'nanobot'
    state_root = repo_root / 'workspace' / 'state' / 'self_evolution'
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / 'current_state.json').write_text(json.dumps({
        'selfevo_issue': {
            'number': 83,
            'status': 'blocked',
            'github_issue_state': 'OPEN',
            'retry_allowed': True,
        },
        'last_pr': {
            'number': 90,
            'merged': False,
            'state': 'OPEN',
        },
    }), encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    issue, pr = _selected_hypothesis_terminal_evidence(cfg)

    assert issue and issue['number'] == 83
    assert pr and pr['number'] == 90


def test_api_system_exposes_selfevo_current_state_freshness_against_product_head(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q'], cwd=repo_root, check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo_root, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=repo_root, check=True)
    (repo_root / 'README.md').write_text('initial\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'README.md'], cwd=repo_root, check=True)
    subprocess.run(['git', 'commit', '-q', '-m', 'initial'], cwd=repo_root, check=True)
    stale_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo_root, text=True).strip()
    (repo_root / 'README.md').write_text('new head\n', encoding='utf-8')
    subprocess.run(['git', 'commit', '-q', '-am', 'new head'], cwd=repo_root, check=True)
    product_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo_root, text=True).strip()

    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({'task_plan': {'current_task_id': 'task-1'}}), encoding='utf-8')
    selfevo_dir = state_root / 'self_evolution'
    selfevo_dir.mkdir(parents=True, exist_ok=True)
    (selfevo_dir / 'current_state.json').write_text(json.dumps({
        'state': 'running',
        'remote_head': stale_commit,
        'current_candidate': {'commit': stale_commit},
    }), encoding='utf-8')
    insert_collection(db, {
        'collected_at': '2026-04-26T15:18:40Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-current.json',
        'outbox_source': '/workspace/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({'current_plan': {'current_task_id': 'task-1'}, 'outbox': {'status': 'PASS'}}),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    system = _call_json(create_app(cfg), '/api/system')
    freshness = system['selfevo_current_proof']['product_head_freshness']

    assert freshness['state'] == 'stale'
    assert freshness['product_head'] == product_head
    assert freshness['current_candidate_commit'] == stale_commit
    assert freshness['remote_head'] == stale_commit
    assert freshness['state_fresh'] is False


def test_api_system_treats_observed_product_head_as_fresh_without_rewriting_candidate(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q'], cwd=repo_root, check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo_root, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=repo_root, check=True)
    (repo_root / 'README.md').write_text('initial\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'README.md'], cwd=repo_root, check=True)
    subprocess.run(['git', 'commit', '-q', '-m', 'initial'], cwd=repo_root, check=True)
    stale_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo_root, text=True).strip()
    (repo_root / 'README.md').write_text('new observed product head\n', encoding='utf-8')
    subprocess.run(['git', 'commit', '-q', '-am', 'new observed product head'], cwd=repo_root, check=True)
    product_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo_root, text=True).strip()

    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({'task_plan': {'current_task_id': 'task-1'}}), encoding='utf-8')
    selfevo_dir = state_root / 'self_evolution'
    selfevo_dir.mkdir(parents=True, exist_ok=True)
    (selfevo_dir / 'current_state.json').write_text(json.dumps({
        'state': 'running',
        'current_candidate': {'commit': stale_commit},
        'observed_product_head': {'commit': product_head, 'source': 'git_rev_parse_head'},
    }), encoding='utf-8')
    insert_collection(db, {
        'collected_at': '2026-04-26T15:18:40Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-current.json',
        'outbox_source': '/workspace/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({'current_plan': {'current_task_id': 'task-1'}, 'outbox': {'status': 'PASS'}}),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    system = _call_json(create_app(cfg), '/api/system')
    freshness = system['selfevo_current_proof']['product_head_freshness']

    assert freshness['state'] == 'fresh'
    assert freshness['state_fresh'] is True
    assert freshness['product_head'] == product_head
    assert freshness['current_candidate_commit'] == stale_commit
    assert freshness['observed_product_head_commit'] == product_head
    assert freshness['state_commit'] == product_head


def test_dashboard_apis_expose_canonical_live_proof_pointers(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)
    (state_root / 'subagents' / 'requests').mkdir(parents=True, exist_ok=True)
    (state_root / 'subagents' / 'results').mkdir(parents=True, exist_ok=True)

    current_summary = {
        'task_plan': {
            'current_task_id': 'record-reward',
            'selected_tasks': 'Record cycle reward [task_id=record-reward]',
            'task_selection_source': 'recorded_current_task',
            'feedback_decision': {
                'mode': 'force_remediation',
                'selected_task_id': 'record-reward',
                'selection_source': 'recorded_current_task',
            },
        },
        'material_progress': {
            'schema_version': 'material-progress-v1',
            'state': 'blocked',
            'available': True,
            'healthy_autonomy_allowed': False,
            'proof_count': 0,
            'proofs': [],
            'qualifying_proofs': [],
            'blocking_reason': 'no_qualifying_material_progress',
        },
        'runtime_source': {'source': 'workspace_state'},
    }
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps(current_summary), encoding='utf-8')
    (state_root / 'experiments').mkdir(parents=True, exist_ok=True)
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({
        'experiment_id': 'exp-live-1',
        'status': 'PASS',
        'outcome': 'accepted',
        'budget_used': {'requests': 2, 'tool_calls': 4, 'subagents': 1, 'elapsed_seconds': 30},
    }), encoding='utf-8')
    request_path = state_root / 'subagents' / 'requests' / 'req-1.json'
    result_path = state_root / 'subagents' / 'results' / 'res-1.json'
    request_path.write_text(json.dumps({
        'task_id': 'record-reward',
        'task_title': 'Record cycle reward',
        'cycle_id': 'cycle-179',
        'status': 'queued',
        'source_artifact': 'workspace/state/reports/evolution-current.json',
    }), encoding='utf-8')
    result_path.write_text(json.dumps({
        'task_id': 'record-reward',
        'task_title': 'Record cycle reward',
        'cycle_id': 'cycle-179',
        'status': 'completed',
        'summary': 'materialized proof pointer',
        'request_path': str(request_path),
    }), encoding='utf-8')

    raw_plan = {
        'current_plan': current_summary['task_plan'],
        'material_progress': current_summary['material_progress'],
        'outbox': {'status': 'PASS'},
    }
    for source in ('repo', 'eeepc'):
        insert_collection(db, {
            'collected_at': '2026-04-24T07:30:00Z',
            'source': source,
            'status': 'PASS',
            'active_goal': 'goal-bootstrap',
            'approval_gate': None,
            'gate_state': None,
            'report_source': '/workspace/state/reports/evolution-current.json',
            'outbox_source': '/workspace/state/outbox/report.index.json',
            'artifact_paths_json': '[]',
            'promotion_summary': None,
            'promotion_candidate_path': None,
            'promotion_decision_record': None,
            'promotion_accepted_record': None,
            'raw_json': json.dumps(raw_plan),
        })
    upsert_event(db, {
        'collected_at': '2026-04-24T07:31:00Z',
        'source': 'repo',
        'event_type': 'subagent',
        'identity_key': 'subagent-cycle-179',
        'title': 'record-reward',
        'status': 'PASS',
        'detail_json': json.dumps({'cycle_id': 'cycle-179', 'report_path': str(result_path), 'origin': {'channel': 'runtime'}}),
    })

    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/state',
    )
    app = create_app(cfg)

    system = _call_json(app, '/api/system')
    assert system['control_plane']['current_task'] == 'record-reward'

    plan = _call_json(app, '/api/plan')
    assert plan['runtime_parity']['schema_version'] == 'runtime-parity-v1'
    assert plan['autonomy_verdict']['schema_version'] == 'autonomy-verdict-v1'
    assert plan['material_progress']['schema_version'] == 'material-progress-v1'

    experiments = _call_json(app, '/api/experiments')
    assert experiments['runtime_parity']['schema_version'] == 'runtime-parity-v1'
    assert experiments['autonomy_verdict']['schema_version'] == 'autonomy-verdict-v1'
    assert experiments['material_progress']['schema_version'] == 'material-progress-v1'
    assert experiments['latest'] is not None
    assert experiments['summary']['schema_version'] == 'experiment-summary-v1'
    assert experiments['summary']['available'] is True
    assert isinstance(experiments['items'], list)

    analytics_api = _call_json(app, '/api/analytics')
    assert analytics_api['autonomy_verdict']['schema_version'] == 'autonomy-verdict-v1'
    assert analytics_api['material_progress']['schema_version'] == 'material-progress-v1'
    assert analytics_api['runtime_parity']['schema_version'] == 'runtime-parity-v1'
    assert analytics_api['hypothesis_dynamics']['schema_version'] == 'hypothesis-dynamics-v1'

    subagents = _call_json(app, '/api/subagents')
    assert subagents['latest_request']['task_id'] == 'record-reward'
    assert subagents['latest_result']['task_id'] == 'record-reward'
    assert subagents['latest_telemetry']['title'] == 'record-reward'


def test_hypotheses_api_exposes_local_vs_live_diagnostics_and_prefers_live_canonical_backlog(tmp_path: Path, monkeypatch) -> None:
    import nanobot_ops_dashboard.app as dashboard_app

    project_root = Path('/home/ozand/herkoot/Projects/nanobot/ops/dashboard')
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)

    _seed_hypotheses_backlog(repo_root, entry_count=5, selected_id='local-hyp-5', selected_title='Local hypothesis 5')
    live_backlog = {
        'schema_version': 'hypotheses-backlog-v1',
        'model': 'HADI',
        'entries': [
            {
                'hypothesis_id': f'live-hyp-{index}',
                'title': f'Live hypothesis {index}',
                'bounded_priority_score': float(10 - index),
                'selection_status': 'selected' if index == 7 else 'candidate',
                'execution_spec': {
                    'goal': f'Live goal {index}',
                    'task': f'Live task {index}',
                    'acceptance': f'Live acceptance {index}',
                    'budget': {'limit': index * 10},
                },
                'wsjf': {'score': index * 3},
                'hadi': {
                    'hypothesis': f'Live hypothesis statement {index}',
                    'action': f'Live action {index}',
                },
            }
            for index in range(1, 8)
        ],
        'selected_hypothesis_id': 'live-hyp-7',
        'selected_hypothesis_title': 'Live hypothesis 7',
    }

    def fake_remote_file_preview(cfg, remote_path: str, max_chars: int = 800) -> dict:
        return {
            'path': remote_path,
            'exists': True,
            'preview': json.dumps(live_backlog),
        }

    monkeypatch.setattr(dashboard_app, '_remote_file_preview', fake_remote_file_preview)
    key_path = tmp_path / 'eeepc.key'
    key_path.write_text('test-key', encoding='utf-8')
    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=key_path,
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )
    app = create_app(cfg)

    payload = _call_json(app, '/api/hypotheses')
    assert payload['available'] is True
    assert payload['source'] == 'eeepc'
    assert payload['canonical_source'] == 'eeepc'
    assert payload['canonical_path'] == '/var/lib/eeepc-agent/self-evolving-agent/state/hypotheses/backlog.json'
    assert payload['live_path'] == '/var/lib/eeepc-agent/self-evolving-agent/state/hypotheses/backlog.json'
    assert payload['local_path'].endswith('/workspace/state/hypotheses/backlog.json')
    assert payload['local_entry_count'] == 5
    assert payload['live_entry_count'] == 7
    assert payload['entry_count'] == 7
    assert payload['selected_hypothesis_id'] == 'live-hyp-7'
    assert payload['selected_hypothesis_title'] == 'Live hypothesis 7'
    assert set(payload['mismatch_reasons']) >= {'entry_count_drift', 'selected_hypothesis_drift'}
    assert payload['source_mismatch'] is True
    assert payload['canonical_entry_count'] == 7
    assert payload['top_entries'][0]['hypothesis_id'] == 'live-hyp-1'

    status, body = _call_app(app, '/hypotheses')
    assert status.startswith('200')
    assert 'Canonical source: eeepc' in body
    assert 'Local entries' in body
    assert 'Live entries' in body
    assert 'entry_count_drift' in body
    assert 'Live hypothesis 7' in body


def test_dashboard_apis_hydrate_selected_hypothesis_diagnostics_and_material_progress_from_live_canonical_reports(tmp_path: Path, monkeypatch) -> None:
    import nanobot_ops_dashboard.app as dashboard_app

    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)

    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)
    (state_root / 'credits').mkdir(parents=True, exist_ok=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True, exist_ok=True)
    _seed_hypotheses_backlog(
        repo_root,
        entry_count=5,
        selected_id='materialize-synthesized-improvement',
        selected_title='Materialize synthesized improvement',
    )

    live_report_path = '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-current.json'
    live_report = {
        'current_plan': {
            'current_task_id': 'materialize-synthesized-improvement',
            'current_task': 'Materialize synthesized improvement',
            'selected_hypothesis_id': 'materialize-synthesized-improvement',
            'selected_hypothesis_title': 'Materialize synthesized improvement',
            'task_selection_source': 'generated_from_failure_learning',
            'feedback_decision': {
                'mode': 'handoff_to_next_candidate',
                'current_task_id': 'materialize-synthesized-improvement',
                'selected_task_id': 'materialize-synthesized-improvement',
                'selected_task_title': 'Materialize synthesized improvement',
                'selection_source': 'generated_from_failure_learning',
            },
            'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0, 'elapsed_seconds': 0},
            'experiment': {
                'outcome': 'discard',
                'revert_status': 'skipped_no_material_change',
                'revert_required': True,
            },
        },
        'material_progress': {
            'schema_version': 'material-progress-v1',
            'state': 'proven',
            'available': True,
            'healthy_autonomy_allowed': True,
            'proof_count': 1,
            'proofs': [{'kind': 'accepted_experiment', 'present': True, 'reason': 'experiment_accepted'}],
            'qualifying_proofs': ['accepted_experiment'],
            'blocking_reason': None,
        },
    }

    def fake_remote_file_preview(cfg, remote_path: str, max_chars: int = 800) -> dict:
        if remote_path == live_report_path:
            return {
                'path': remote_path,
                'exists': True,
                'preview': json.dumps(live_report),
            }
        return {'path': remote_path, 'exists': False, 'preview': None}

    monkeypatch.setattr(dashboard_app, '_remote_file_preview', fake_remote_file_preview)
    key_path = tmp_path / 'eeepc.key'
    key_path.write_text('test-key', encoding='utf-8')

    (project_root / 'control').mkdir(parents=True, exist_ok=True)
    (project_root / 'control' / 'current_summary.json').write_text(json.dumps({
        'material_progress': {
            'schema_version': 'material-progress-v1',
            'state': 'blocked',
            'available': True,
            'healthy_autonomy_allowed': False,
            'proof_count': 0,
            'proofs': [],
            'qualifying_proofs': [],
            'blocking_reason': 'no_qualifying_material_progress',
        },
    }), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({
        'reward_gate': {
            'status': 'suppressed',
            'reason': 'discarded_experiment_unresolved_revert',
        },
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'current_state.json').write_text(json.dumps({
        'selfevo_issue': {
            'number': 61,
            'title': 'Materialize synthesized improvement',
            'url': 'https://github.com/ozand/eeebot-self-evolving/issues/61',
        },
        'last_pr': {
            'number': 62,
            'title': 'Materialized improvement follow-through',
            'url': 'https://github.com/ozand/eeebot-self-evolving/pull/62',
        },
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({
        'status': 'terminal_noop',
        'selfevo_issue': {
            'number': 61,
            'title': 'Materialize synthesized improvement',
            'url': 'https://github.com/ozand/eeebot-self-evolving/issues/61',
        },
        'pr': {
            'number': 62,
            'title': 'Materialized improvement follow-through',
            'url': 'https://github.com/ozand/eeebot-self-evolving/pull/62',
        },
    }), encoding='utf-8')

    for idx in range(5):
        upsert_event(db, {
            'collected_at': f'2026-04-24T13:{idx:02d}:00Z',
            'source': 'eeepc',
            'event_type': 'cycle',
            'identity_key': f'cycle-live-{idx}',
            'title': 'summary-only cycle',
            'status': 'PASS',
            'detail_json': json.dumps({
                'report_source': live_report_path,
                'artifact_paths': [live_report_path],
            }),
        })

    insert_collection(db, {
        'collected_at': '2026-04-24T13:05:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': live_report_path,
        'outbox_source': live_report_path,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({'report_source': live_report_path}),
    })

    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=key_path,
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )
    app = create_app(cfg)

    hypotheses = _call_json(app, '/api/hypotheses')
    system = _call_json(app, '/api/system')

    diagnostics = hypotheses['selected_hypothesis_diagnostics']
    assert diagnostics['selected_hypothesis_id'] == 'materialize-synthesized-improvement'
    assert diagnostics['state'] == 'stagnant'
    assert diagnostics['run_count'] == 5
    assert diagnostics['run_streak'] == 5
    assert diagnostics['last_24h']['total_runs'] == 5
    assert diagnostics['last_24h']['discard_count'] == 5
    assert diagnostics['last_24h']['reward_gate']['status'] == 'suppressed'
    assert diagnostics['last_24h']['reward_gate']['reason'] == 'discarded_experiment_unresolved_revert'

    assert system['control_plane']['current_task'] == 'Materialize synthesized improvement'
    assert system['control_plane']['material_progress']['state'] == 'proven'
    assert system['control_plane']['material_progress']['healthy_autonomy_allowed'] is True
    assert system['material_progress']['state'] == 'proven'
    assert system['material_progress']['healthy_autonomy_allowed'] is True

    assert system['autonomy_verdict']['state'] == 'stagnant'
    assert 'hypothesis_dynamics_stagnant' in system['autonomy_verdict']['reasons']


def test_runtime_parity_adopts_fresh_live_synthesized_materialization_when_local_task_is_stale(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _dashboard_runtime_parity

    repo_root = tmp_path / 'nanobot'
    state_root = repo_root / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    parity = _dashboard_runtime_parity(
        {'current_task_id': 'analyze-last-failed-candidate'},
        {
            'current_task_id': 'materialize-synthesized-improvement',
            'task_selection_source': 'feedback_synthesis_materialization',
            'feedback_decision': {
                'mode': 'materialize_synthesized_improvement',
                'current_task_id': 'synthesize-next-improvement-candidate',
                'selected_task_id': 'materialize-synthesized-improvement',
                'selection_source': 'feedback_synthesis_materialization',
            },
        },
        cfg,
    )

    assert parity['state'] == 'authority_resolved_with_source_skew'
    assert 'current_task_drift' not in parity['reasons']
    assert parity['canonical_current_task_id'] == 'materialize-synthesized-improvement'
    assert parity['authority_resolution'] == 'fresh_live_synthesized_materialization'


def test_runtime_parity_adopts_fresh_live_post_materialization_reward_when_local_task_is_stale(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _dashboard_runtime_parity

    repo_root = tmp_path / 'nanobot'
    state_root = repo_root / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    parity = _dashboard_runtime_parity(
        {'current_task_id': 'synthesize-next-improvement-candidate'},
        {
            'current_task_id': 'record-reward',
            'task_selection_source': 'feedback_synthesized_materialization_complete_reward',
            'feedback_decision': {
                'mode': 'record_reward_after_synthesized_materialization',
                'current_task_id': 'record-reward',
                'selected_task_id': 'record-reward',
                'selection_source': 'feedback_synthesized_materialization_complete_reward',
            },
        },
        cfg,
    )

    assert parity['state'] == 'authority_resolved_with_source_skew'
    assert 'current_task_drift' not in parity['reasons']
    assert parity['canonical_current_task_id'] == 'record-reward'
    assert parity['authority_resolution'] == 'fresh_live_post_materialization_reward'


def test_runtime_parity_adopts_fresh_live_synthesized_candidate_after_reward_rotation_when_local_task_is_stale(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _dashboard_runtime_parity

    repo_root = tmp_path / 'nanobot'
    state_root = repo_root / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    parity = _dashboard_runtime_parity(
        {'current_task_id': 'record-reward'},
        {
            'current_task_id': 'synthesize-next-improvement-candidate',
            'feedback_decision': {
                'mode': 'synthesize_next_candidate',
                'selection_source': 'feedback_no_selectable_retired_lane_synthesis',
                'selected_task_id': 'synthesize-next-improvement-candidate',
            },
            'task_selection_source': 'feedback_no_selectable_retired_lane_synthesis',
        },
        cfg,
    )

    assert parity['state'] == 'authority_resolved_with_source_skew'
    assert 'current_task_drift' not in parity['reasons']
    assert parity['canonical_current_task_id'] == 'synthesize-next-improvement-candidate'
    assert parity['authority_resolution'] == 'fresh_live_synthesis_candidate'


def test_subagent_visibility_hydrates_bridge_result_report_budget_and_artifacts(tmp_path: Path) -> None:
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    report_path = repo_root / 'workspace' / 'state' / 'reports' / 'evolution-subagent.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        'cycle_id': 'cycle-subagent',
        'current_task_id': 'materialize-synthesized-improvement',
        'result_status': 'PASS',
        'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 1, 'elapsed_seconds': 37},
        'artifact_paths': ['/tmp/subagent-result.json'],
    }), encoding='utf-8')
    bridge_dir = repo_root / '.nanobot' / 'subagents'
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / 'a25ae7e7.json').write_text(json.dumps({
        'status': 'ok',
        'task_id': 'materialize-synthesized-improvement',
        'cycle_id': 'cycle-subagent',
        'report_path': str(report_path),
        'summary': 'edited prompts/diagnostics.md',
    }), encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    subagents = _call_json(create_app(cfg), '/api/subagents')

    assert subagents['summary']['result_count'] == 1
    latest = subagents['latest_result']
    assert latest['task_id'] == 'materialize-synthesized-improvement'
    assert latest['report_path'] == str(report_path)
    assert latest['canonical_report_hydrated'] is True
    assert latest['hydrated_report_current_task_id'] == 'materialize-synthesized-improvement'
    assert latest['budget_used']['subagents'] == 1
    assert latest['artifact_paths'] == ['/tmp/subagent-result.json']


def test_eeepc_privileged_rollout_readiness_surfaces_partial_live_report_when_privileged_reads_fail(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    (repo_root / 'workspace' / 'state' / 'control_plane').mkdir(parents=True, exist_ok=True)
    (repo_root / 'workspace' / 'state' / 'control_plane' / 'current_summary.json').write_text(json.dumps({
        'task_plan': {
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task': 'Analyze the last failed self-evolution candidate before retrying mutation',
        },
        'material_progress': {'state': 'proven', 'healthy_autonomy_allowed': True},
    }), encoding='utf-8')
    insert_collection(db, {
        'collected_at': '2026-04-26T19:02:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-latest.json',
        'outbox_source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-latest.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({
            'outbox': {
                'status': 'PASS',
                'source': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-latest.json',
                'selected_tasks': 'Record cycle reward [task_id=record-reward]',
                'task_selection_source': 'recorded_current_task',
                'feedback_decision': None,
            },
            'goals': {},
            'reachability': {'reachable': True},
            'source_errors': {
                'outbox': {'stage': 'ssh:/state/outbox/report.index.json', 'message': 'Permission denied'},
                'goals': {'stage': 'ssh:/state/goals/registry.json', 'message': 'Permission denied'},
            },
        }),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    readiness = _call_json(create_app(cfg), '/api/system')['eeepc_privileged_rollout_readiness']

    assert readiness['schema_version'] == 'eeepc-privileged-rollout-readiness-v1'
    assert readiness['state'] == 'blocked_privileged_access'
    assert readiness['requires_privileged_access'] is True
    assert readiness['available_partial_proof'] == 'latest_readable_report'
    assert 'read_authority_outbox' in readiness['blocked_capabilities']
    assert 'read_goal_registry' in readiness['blocked_capabilities']
    assert 'execute_opencode_nanobot_or_sudo' in readiness['blocked_capabilities']
    assert readiness['source_errors']['outbox']['message'] == 'Permission denied'
    assert readiness['runtime_parity_state'] == 'legacy_reward_loop'
    assert readiness['next_issue'] == 210


def test_runtime_parity_adopts_fresh_live_hadi_handoff_when_local_task_is_stale(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _dashboard_runtime_parity

    repo_root = tmp_path / 'nanobot'
    state_root = repo_root / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    parity = _dashboard_runtime_parity(
        {
            'current_task_id': 'analyze-last-failed-candidate',
            'feedback_decision': {'mode': 'retire_terminal_selfevo_lane', 'selected_task_id': 'record-reward'},
        },
        {
            'current_task_id': 'subagent-verify-materialized-improvement',
            'task_selection_source': 'feedback_post_completion_handoff',
            'feedback_decision': {
                'mode': 'handoff_to_next_candidate',
                'current_task_id': 'materialize-pass-streak-improvement',
                'selected_task_id': 'subagent-verify-materialized-improvement',
                'selection_source': 'feedback_post_completion_handoff',
            },
        },
        cfg,
    )

    assert parity['state'] == 'authority_resolved_with_source_skew'
    assert 'current_task_drift' not in parity['reasons']
    assert parity['canonical_current_task_id'] == 'subagent-verify-materialized-improvement'
    assert parity['authority_resolution'] == 'fresh_live_hadi_handoff'


def test_runtime_parity_adopts_fresh_live_pass_streak_switch_when_local_task_is_stale(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _dashboard_runtime_parity

    repo_root = tmp_path / 'nanobot'
    state_root = repo_root / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    parity = _dashboard_runtime_parity(
        {
            'current_task_id': 'analyze-last-failed-candidate',
            'feedback_decision': {'mode': 'fresh_failure_learning_repair', 'selected_task_id': 'analyze-last-failed-candidate'},
        },
        {
            'current_task_id': 'inspect-pass-streak',
            'task_selection_source': 'feedback_pass_streak_switch',
            'feedback_decision': {
                'mode': 'retire_goal_artifact_pair',
                'current_task_id': 'record-reward',
                'selected_task_id': 'inspect-pass-streak',
                'selection_source': 'feedback_pass_streak_switch',
                'retire_goal_artifact_pair': True,
                'strong_pass_count': 3,
            },
        },
        cfg,
    )

    assert parity['state'] == 'authority_resolved_with_source_skew'
    assert 'current_task_drift' not in parity['reasons']
    assert parity['canonical_current_task_id'] == 'inspect-pass-streak'
    assert parity['authority_resolution'] == 'fresh_live_pass_streak_switch'


def test_runtime_parity_adopts_live_terminal_selfevo_retirement_when_local_task_is_stale(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _dashboard_runtime_parity

    repo_root = tmp_path / 'nanobot'
    state_root = repo_root / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    parity = _dashboard_runtime_parity(
        {
            'current_task_id': 'analyze-last-failed-candidate',
            'task_selection_source': 'feedback_complete_active_lane_to_failure_learning',
            'feedback_decision': {
                'mode': 'complete_active_lane',
                'selected_task_id': 'analyze-last-failed-candidate',
                'selection_source': 'feedback_complete_active_lane_to_failure_learning',
            },
        },
        {
            'current_task_id': 'record-reward',
            'task_selection_source': 'feedback_terminal_selfevo_retire',
            'feedback_decision': {
                'mode': 'retire_terminal_selfevo_lane',
                'current_task_id': 'analyze-last-failed-candidate',
                'selected_task_id': 'record-reward',
                'selection_source': 'feedback_terminal_selfevo_retire',
                'terminal_selfevo_issue': {'number': 61, 'status': 'terminal_merged'},
            },
        },
        cfg,
    )

    assert parity['state'] == 'authority_resolved_with_source_skew'
    assert 'current_task_drift' not in parity['reasons']
    assert parity['canonical_current_task_id'] == 'record-reward'
    assert parity['authority_resolution'] == 'fresh_live_terminal_selfevo_retire'


def test_ambition_utilization_flags_low_budget_discard_streak() -> None:
    from nanobot_ops_dashboard.app import _ambition_utilization_verdict

    analytics = {
        'recent_status_sequence': [
            {
                'status': 'PASS',
                'title': 'analyze-last-failed-candidate',
                'detail': {
                    'current_task_id': 'analyze-last-failed-candidate',
                    'experiment': {'outcome': 'discard'},
                    'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0, 'elapsed_seconds': 0},
                },
            }
            for _ in range(6)
        ]
    }
    verdict = _ambition_utilization_verdict(analytics=analytics, experiment_visibility={}, subagent_visibility={})
    assert verdict['state'] == 'underutilized'
    assert 'low_budget_discard_streak' in verdict['reasons']
    assert 'subagents_unused' in verdict['reasons']
    assert verdict['recommended_next_action'] == 'escalate_to_higher_ambition_lane_or_emit_precise_blocker'
    assert verdict['escalation']['schema_version'] == 'ambition-escalation-v1'
    assert verdict['escalation']['state'] == 'required'
    assert verdict['escalation']['policy'] == 'select_safe_bounded_lane_or_emit_precise_blocker'
    assert 'materialize-synthesized-improvement' in verdict['escalation']['safe_bounded_lanes']


def test_ambition_utilization_treats_blocked_escalation_as_underutilized() -> None:
    from nanobot_ops_dashboard.app import _ambition_utilization_verdict

    blocked_feedback = {
        'mode': 'ambition_escalation_blocked',
        'selection_source': 'feedback_ambition_escalation_blocked',
        'ambition_escalation': {
            'state': 'blocked',
            'reasons': ['same_task_streak', 'subagents_unused', 'tool_budget_underused'],
            'blocker': 'no_safe_bounded_escalation_lane_selectable',
        },
    }
    verdict = _ambition_utilization_verdict(
        analytics={'recent_status_sequence': []},
        experiment_visibility={
            'current_experiment': {
                'outcome': 'discard',
                'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0, 'elapsed_seconds': 0},
                'raw': {'feedback_decision': blocked_feedback},
            }
        },
        subagent_visibility={},
    )

    assert verdict['state'] == 'underutilized'
    assert 'ambition_escalation_blocked' in verdict['reasons']
    assert verdict['escalation']['state'] == 'blocked'
    assert verdict['escalation']['blocker'] == 'no_safe_bounded_escalation_lane_selectable'
    assert verdict['recommended_next_action'] == 'resolve_ambition_escalation_blocker'


def test_autonomy_verdict_blocks_healthy_progress_when_ambition_is_blocked(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _autonomy_verdict

    cfg = DashboardConfig(
        project_root=tmp_path / 'dashboard',
        nanobot_repo_root=tmp_path / 'repo',
        db_path=tmp_path / 'dashboard.sqlite3',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'id_ed25519',
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )
    verdict = _autonomy_verdict(
        analytics={'recent_status_sequence': []},
        plan_latest={'current_task_id': 'record-reward'},
        experiment_visibility={'current_experiment': {}},
        credits_visibility={'current': {}},
        cfg=cfg,
        material_progress={'state': 'proven', 'healthy_autonomy_allowed': True},
        runtime_parity={'state': 'healthy', 'reasons': [], 'local_current_task_id': 'record-reward', 'live_current_task_id': 'record-reward', 'canonical_current_task_id': 'record-reward'},
        ambition_utilization={
            'state': 'underutilized',
            'reasons': ['ambition_escalation_blocked'],
            'escalation': {'state': 'blocked', 'blocker': 'no_safe_bounded_escalation_lane_selectable'},
        },
        hypothesis_dynamics={'state': 'healthy'},
        promotion_replay_readiness={'state': 'not_ready', 'decision_record': {'present': True}, 'accepted_record': {'present': True}},
        strong_reflection_freshness={'state': 'fresh'},
    )

    assert verdict['state'] == 'stagnant'
    assert 'ambition_underutilized' in verdict['reasons']


def test_historical_blocked_escalation_does_not_poison_current_ambition_truth() -> None:
    from nanobot_ops_dashboard.app import _ambition_utilization_verdict

    verdict = _ambition_utilization_verdict(
        analytics={
            'recent_status_sequence': [
                {
                    'detail': {
                        'feedback_decision': {
                            'mode': 'ambition_escalation_blocked',
                            'ambition_escalation': {'state': 'blocked', 'blocker': 'old_blocker'},
                        }
                    }
                }
            ]
        },
        experiment_visibility={'current_experiment': {'outcome': 'accept', 'raw': {'feedback_decision': {'mode': 'complete_active_lane'}}}},
        subagent_visibility={},
    )

    assert verdict['state'] == 'substantive'
    assert 'ambition_escalation_blocked' not in verdict['reasons']


def test_current_blocked_escalation_overrides_rotating_synthesis_window() -> None:
    from nanobot_ops_dashboard.app import _ambition_utilization_verdict

    rotating_rows = [
        {'detail': {'current_task_id': 'synthesize-next-improvement-candidate', 'feedback_decision': {'mode': 'synthesize_next_candidate'}, 'materialized_improvement_artifact_path': '/tmp/a.json', 'experiment': {'outcome': 'accept'}, 'budget_used': {'requests': 4, 'tool_calls': 8, 'subagents': 1, 'elapsed_seconds': 30}}},
        {'detail': {'current_task_id': 'materialize-synthesized-improvement', 'feedback_decision': {'mode': 'complete_active_lane'}, 'materialized_improvement_artifact_path': '/tmp/b.json', 'experiment': {'outcome': 'accept'}, 'budget_used': {'requests': 4, 'tool_calls': 8, 'subagents': 1, 'elapsed_seconds': 30}}},
        {'detail': {'current_task_id': 'record-reward', 'feedback_decision': {'mode': 'record_reward_after_synthesized_materialization'}, 'materialized_improvement_artifact_path': '/tmp/c.json', 'experiment': {'outcome': 'accept'}, 'budget_used': {'requests': 4, 'tool_calls': 8, 'subagents': 1, 'elapsed_seconds': 30}}},
    ]
    verdict = _ambition_utilization_verdict(
        analytics={'recent_status_sequence': rotating_rows},
        experiment_visibility={
            'current_experiment': {
                'outcome': 'discard',
                'raw': {
                    'feedback_decision': {
                        'mode': 'ambition_escalation_blocked',
                        'ambition_escalation': {'state': 'blocked', 'blocker': 'no_safe_bounded_escalation_lane_selectable'},
                    }
                },
            }
        },
        subagent_visibility={},
    )

    assert verdict['rotating_synthesis_reward_window'] is True
    assert verdict['state'] == 'underutilized'
    assert verdict['escalation']['state'] == 'blocked'


def test_ambition_utilization_treats_rotating_synthesis_reward_window_as_substantive() -> None:
    from nanobot_ops_dashboard.app import _ambition_utilization_verdict

    def row(task_id: str, mode: str, artifact: str) -> dict:
        return {
            'status': 'PASS',
            'title': task_id,
            'detail': {
                'current_task_id': task_id,
                'materialized_improvement_artifact_path': artifact,
                'feedback_decision': {
                    'mode': mode,
                    'selected_task_id': task_id,
                    'selection_source': 'feedback_no_selectable_retired_lane_synthesis'
                    if mode == 'synthesize_next_candidate'
                    else 'feedback_synthesized_materialization_complete_reward'
                    if mode == 'record_reward_after_synthesized_materialization'
                    else 'feedback_complete_active_lane',
                },
                'experiment': {'outcome': 'discard'},
                'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0, 'elapsed_seconds': 0},
            },
        }

    analytics = {
        'recent_status_sequence': [
            row('record-reward', 'record_reward_after_synthesized_materialization', 'materialized-cycle-c.json'),
            row('record-reward', 'complete_active_lane', 'materialized-cycle-c.json'),
            row('synthesize-next-improvement-candidate', 'synthesize_next_candidate', 'materialized-cycle-b.json'),
            row('record-reward', 'record_reward_after_synthesized_materialization', 'materialized-cycle-b.json'),
            row('record-reward', 'complete_active_lane', 'materialized-cycle-b.json'),
            row('synthesize-next-improvement-candidate', 'synthesize_next_candidate', 'materialized-cycle-a.json'),
        ]
    }

    verdict = _ambition_utilization_verdict(analytics=analytics, experiment_visibility={}, subagent_visibility={})

    assert verdict['state'] == 'substantive'
    assert verdict['reasons'] == []
    assert verdict['recommended_next_action'] is None
    assert verdict['escalation'] is None


def test_ambition_utilization_ignores_sparse_goal_only_cycle_rows() -> None:
    from nanobot_ops_dashboard.app import _ambition_utilization_verdict

    analytics = {
        'recent_status_sequence': [
            {
                'status': 'PASS',
                'title': 'goal-bootstrap',
                'detail': {
                    'report_source': f'/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-{index}.json',
                    'artifact_paths': [f'/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-{index}.json'],
                },
            }
            for index in range(20)
        ]
    }

    verdict = _ambition_utilization_verdict(analytics=analytics, experiment_visibility={}, subagent_visibility={})

    assert verdict['state'] == 'substantive'
    assert verdict['recent_window'] == 1
    assert verdict['reasons'] == []
    assert verdict['escalation'] is None


def test_strong_reflection_freshness_exposes_latest_artifact(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from nanobot_ops_dashboard.app import _strong_reflection_freshness

    repo_root = tmp_path / 'nanobot'
    latest = repo_root / 'workspace' / 'state' / 'strong_reflection' / 'latest.json'
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps({
        'schema_version': 'strong-reflection-run-v1',
        'recorded_at_utc': '2026-04-27T00:00:00+00:00',
        'summary': 'Self-evolving cycle PASS — evidence=evidence-1',
        'mode': 'strong-reflection',
    }), encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=repo_root, db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing', eeepc_state_root='/state')
    result = _strong_reflection_freshness(cfg, datetime(2026, 4, 27, 1, 0, tzinfo=timezone.utc))
    assert result['state'] == 'fresh'
    assert result['available'] is True
    assert result['summary'].startswith('Self-evolving cycle PASS')


def test_runtime_parity_accepts_local_failure_learning_repair_over_stale_live_complete_lane(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _dashboard_runtime_parity

    state = tmp_path / 'repo' / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=tmp_path / 'repo', db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing', eeepc_state_root='/state')
    repo_plan = {
        'current_task_id': 'analyze-last-failed-candidate',
        'feedback_decision': {
            'mode': 'complete_active_lane',
            'selection_source': 'feedback_complete_active_lane_to_failure_learning',
            'selected_task_id': 'analyze-last-failed-candidate',
        },
    }
    live_plan = {
        'current_task_id': 'record-reward',
        'task_selection_source': 'feedback_complete_active_lane',
        'feedback_decision': {
            'mode': 'complete_active_lane',
            'current_task_id': 'materialize-pass-streak-improvement',
            'selected_task_id': 'record-reward',
            'selection_source': 'feedback_complete_active_lane',
        },
    }

    result = _dashboard_runtime_parity(repo_plan, live_plan, cfg)

    assert result['state'] == 'authority_resolved_with_source_skew'
    assert result['reasons'] == []
    assert result['canonical_current_task_id'] == 'analyze-last-failed-candidate'
    assert result['authority_resolution'] == 'local_failure_learning_repair_over_stale_live_complete_lane'


def test_runtime_parity_adopts_fresh_live_active_lane_when_local_task_is_stale(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _dashboard_runtime_parity

    state = tmp_path / 'repo' / 'workspace' / 'state'
    for rel in [
        'hypotheses/backlog.json',
        'credits/latest.json',
        'control_plane/current_summary.json',
        'self_evolution/current_state.json',
    ]:
        path = state / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=tmp_path / 'repo', db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing', eeepc_state_root='/state')
    repo_plan = {
        'current_task_id': 'record-reward',
        'feedback_decision': {
            'mode': 'retire_terminal_selfevo_lane',
            'selection_source': 'feedback_terminal_selfevo_retire',
            'selected_task_id': 'record-reward',
        },
    }
    live_plan = {
        'current_task_id': 'synthesize-next-improvement-candidate',
        'current_task': 'synthesize-next-improvement-candidate',
        'task_selection_source': 'feedback_continue_active_lane',
        'feedback_decision': {
            'mode': 'continue_active_lane',
            'current_task_id': 'synthesize-next-improvement-candidate',
            'selected_task_id': 'synthesize-next-improvement-candidate',
            'selection_source': 'feedback_continue_active_lane',
        },
    }

    result = _dashboard_runtime_parity(repo_plan, live_plan, cfg)

    assert result['state'] == 'authority_resolved_with_source_skew'
    assert result['reasons'] == []
    assert result['canonical_current_task_id'] == 'synthesize-next-improvement-candidate'
    assert result['authority_resolution'] == 'fresh_live_active_lane'


def test_api_system_exposes_ambition_and_strong_reflection_top_level(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    latest = state_root / 'strong_reflection' / 'latest.json'
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps({
        'schema_version': 'strong-reflection-run-v1',
        'recorded_at_utc': '2026-04-27T20:00:00+00:00',
        'summary': 'Self-evolving cycle PASS — evidence=e-system',
        'mode': 'strong-reflection',
    }), encoding='utf-8')
    (state_root / 'goals').mkdir(parents=True, exist_ok=True)
    (state_root / 'goals' / 'current.json').write_text(json.dumps({
        'schema_version': 'task-plan-v1',
        'current_task_id': 'inspect-pass-streak',
        'current_task': 'Inspect repeated PASS streak for a new bounded improvement',
        'tasks': [],
    }), encoding='utf-8')
    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/state',
    )

    system = _call_json(create_app(cfg), '/api/system')

    assert system['ambition_utilization']['schema_version'] == 'ambition-utilization-v1'
    assert system['strong_reflection_freshness']['schema_version'] == 'strong-reflection-freshness-v1'
    assert system['strong_reflection_freshness']['available'] is True


def test_strong_reflection_freshness_falls_back_to_live_eeepc_artifact(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timezone
    import nanobot_ops_dashboard.app as dashboard_app
    from nanobot_ops_dashboard.app import _strong_reflection_freshness

    def fake_remote_file_preview(cfg, remote_path: str, max_chars: int = 800) -> dict:
        return {
            'path': remote_path,
            'exists': True,
            'preview': json.dumps({
                'schema_version': 'strong-reflection-run-v1',
                'recorded_at_utc': '2026-04-27T20:00:00+00:00',
                'summary': 'Self-evolving cycle PASS — evidence=live',
                'mode': 'strong-reflection',
            }),
        }

    monkeypatch.setattr(dashboard_app, '_remote_file_preview', fake_remote_file_preview)
    key_path = tmp_path / 'missing-key'
    key_path.write_text('test-key', encoding='utf-8')
    cfg = DashboardConfig(
        project_root=tmp_path / 'dashboard',
        nanobot_repo_root=tmp_path / 'repo',
        db_path=tmp_path / 'dashboard.sqlite3',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=key_path,
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )

    result = _strong_reflection_freshness(cfg, datetime(2026, 4, 27, 21, 0, tzinfo=timezone.utc))

    assert result['available'] is True
    assert result['state'] == 'fresh'
    assert result['source'] == 'eeepc'
    assert result['path'] == '/var/lib/eeepc-agent/self-evolving-agent/state/strong_reflection/latest.json'
    assert result['summary'].endswith('live')


def test_api_system_uses_collected_eeepc_strong_reflection_when_local_missing(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    insert_collection(db, {
        'collected_at': '2026-04-27T21:00:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/state/reports/evolution-live.json',
        'outbox_source': '/state/outbox/report.index.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({
            'strong_reflection': {
                'schema_version': 'strong-reflection-run-v1',
                'recorded_at_utc': '2999-04-27T20:00:00+00:00',
                'summary': 'Self-evolving cycle PASS — evidence=collected-live',
                'mode': 'strong-reflection',
                'path': '/var/lib/eeepc-agent/self-evolving-agent/state/strong_reflection/latest.json',
            }
        }),
    })
    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )

    system = _call_json(create_app(cfg), '/api/system')

    freshness = system['strong_reflection_freshness']
    assert freshness['available'] is True
    assert freshness['state'] == 'fresh'
    assert freshness['source'] == 'eeepc'
    assert freshness['summary'].endswith('collected-live')


def test_autonomy_verdict_flags_blocked_pending_promotion_lifecycle(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _autonomy_verdict

    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=tmp_path / 'repo', db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing', eeepc_state_root='/state')
    verdict = _autonomy_verdict(
        analytics={'recent_status_sequence': [], 'current_streak': {'status': 'PASS', 'length': 3}},
        plan_latest={'current_task_id': 'synthesize-next-improvement-candidate'},
        experiment_visibility={'current_experiment': {'outcome': 'keep'}},
        credits_visibility={'current': {'delta': 1.0}},
        cfg=cfg,
        material_progress={'state': 'proven', 'healthy_autonomy_allowed': True},
        runtime_parity={'state': 'healthy', 'canonical_current_task_id': 'synthesize-next-improvement-candidate', 'local_current_task_id': 'synthesize-next-improvement-candidate', 'live_current_task_id': 'synthesize-next-improvement-candidate', 'reasons': []},
        hypothesis_dynamics={'state': 'healthy'},
        promotion_replay_readiness={
            'state': 'blocked',
            'reason': 'not_accepted',
            'review_status': 'pending_policy_review',
            'decision': 'pending_policy_review',
            'decision_record': None,
            'accepted_record': None,
        },
    )

    assert verdict['state'] == 'stagnant'
    assert 'promotion_lifecycle_blocked' in verdict['reasons']
    assert verdict['promotion_replay_readiness']['state'] == 'blocked'


def test_autonomy_verdict_flags_missing_strong_reflection(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _autonomy_verdict

    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=tmp_path / 'repo', db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing', eeepc_state_root='/state')
    verdict = _autonomy_verdict(
        analytics={'recent_status_sequence': [], 'current_streak': {'status': 'PASS', 'length': 3}},
        plan_latest={'current_task_id': 'synthesize-next-improvement-candidate'},
        experiment_visibility={'current_experiment': {'outcome': 'keep'}},
        credits_visibility={'current': {'delta': 1.0}},
        cfg=cfg,
        material_progress={'state': 'proven', 'healthy_autonomy_allowed': True},
        runtime_parity={'state': 'healthy', 'reasons': []},
        hypothesis_dynamics={'state': 'healthy'},
        strong_reflection_freshness={'state': 'missing', 'reason': 'strong_reflection_latest_missing'},
    )

    assert verdict['state'] == 'stagnant'
    assert 'strong_reflection_not_fresh' in verdict['reasons']


def test_api_system_promotes_stuck_promotion_lifecycle_to_autonomy_verdict(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.storage import upsert_event

    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    insert_collection(db, {'collected_at': '2999-04-27T21:00:00Z', 'source': 'repo', 'status': 'PASS', 'active_goal': 'goal-bootstrap', 'current_task': 'Record cycle reward', 'raw_json': '{}'})
    upsert_event(db, {
        'collected_at': '2999-04-27T21:00:00Z',
        'source': 'repo',
        'event_type': 'promotion',
        'identity_key': 'promotion-stuck',
        'title': 'promotion-stuck | pending_policy_review | pending_policy_review',
        'status': 'pending_policy_review',
        'detail_json': json.dumps({
            'candidate_path': '/state/promotions/promotion-stuck.json',
            'decision_record': 'missing',
            'accepted_record': 'missing',
            'governance_packet': {'review_status': 'pending_policy_review', 'decision': 'pending_policy_review'},
        }),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    system = _call_json(create_app(cfg), '/api/system')

    readiness = system['control_plane']['promotion_replay_readiness']
    assert readiness['state'] == 'blocked'
    assert readiness['decision_record'] == 'missing'
    assert readiness['accepted_record'] == 'missing'
    assert 'promotion_lifecycle_blocked' in system['autonomy_verdict']['reasons']


def test_api_system_does_not_block_explicitly_not_ready_promotion(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.storage import upsert_event

    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    insert_collection(db, {'collected_at': '2999-04-27T21:00:00Z', 'source': 'repo', 'status': 'PASS', 'active_goal': 'goal-bootstrap', 'current_task': 'Analyze', 'raw_json': '{}'})
    upsert_event(db, {
        'collected_at': '2999-04-27T21:00:00Z',
        'source': 'repo',
        'event_type': 'promotion',
        'identity_key': 'promotion-not-ready',
        'title': 'promotion-not-ready | not_ready_for_policy_review | not_ready_for_policy_review',
        'status': 'not_ready_for_policy_review',
        'detail_json': json.dumps({
            'candidate_path': '/state/promotions/promotion-not-ready.json',
            'decision_record': None,
            'accepted_record': None,
            'governance_packet': {'review_packet_status': 'not_ready', 'review_status': 'not_ready_for_policy_review', 'decision': 'not_ready_for_policy_review'},
        }),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    system = _call_json(create_app(cfg), '/api/system')

    readiness = system['control_plane']['promotion_replay_readiness']
    assert readiness['state'] == 'not_ready'
    assert readiness['reason'] == 'promotion_candidate_not_ready_for_policy_review'
    assert 'promotion_lifecycle_blocked' not in system['autonomy_verdict']['reasons']


def test_remote_file_preview_kill_switch_avoids_request_time_ssh(tmp_path: Path, monkeypatch) -> None:
    import nanobot_ops_dashboard.app as dashboard_app
    from nanobot_ops_dashboard.app import _remote_file_preview

    monkeypatch.delenv('NANOBOT_DASHBOARD_REMOTE_PREVIEWS', raising=False)

    def fail_if_called(*args, **kwargs):
        raise AssertionError('remote preview attempted request-time subprocess/ssh')

    monkeypatch.setattr(dashboard_app.subprocess, 'run', fail_if_called)
    key_path = tmp_path / 'eeepc.key'
    key_path.write_text('test-key', encoding='utf-8')
    cfg = DashboardConfig(
        project_root=tmp_path / 'dashboard',
        nanobot_repo_root=tmp_path / 'repo',
        db_path=tmp_path / 'dashboard.sqlite3',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=key_path,
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )

    result = _remote_file_preview(
        cfg,
        '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-large.json',
        max_chars=50000,
    )

    assert result == {
        'path': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-large.json',
        'exists': False,
        'preview': None,
        'disabled': True,
    }


def test_remote_file_preview_can_be_enabled_for_explicit_operator_debug(tmp_path: Path, monkeypatch) -> None:
    import nanobot_ops_dashboard.app as dashboard_app
    from nanobot_ops_dashboard.app import _remote_file_preview

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr='')

    monkeypatch.setenv('NANOBOT_DASHBOARD_REMOTE_PREVIEWS', '1')
    monkeypatch.setattr(dashboard_app.subprocess, 'run', fake_run)
    key_path = tmp_path / 'eeepc.key'
    key_path.write_text('test-key', encoding='utf-8')
    cfg = DashboardConfig(
        project_root=tmp_path / 'dashboard',
        nanobot_repo_root=tmp_path / 'repo',
        db_path=tmp_path / 'dashboard.sqlite3',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=key_path,
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )

    result = _remote_file_preview(cfg, '/remote/report.json', max_chars=50000)

    assert result['exists'] is True
    assert result['preview'] == '{"ok": true}'
    assert calls
    cmd, kwargs = calls[0]
    assert 'ssh' in cmd[0]
    assert 'head -c 8000' in cmd[-1]
    assert kwargs['timeout'] == 3


def test_experiment_snapshot_exposes_budget_used_and_subagent_consumption(tmp_path: Path) -> None:
    from nanobot_ops_dashboard.app import _experiment_snapshot_from_payload

    payload = {
        'schema_version': 'experiment-v1',
        'experiment_id': 'experiment-cycle-293',
        'result_status': 'PASS',
        'budget': {'max_requests': 2},
        'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 1, 'elapsed_seconds': 0},
        'subagent_consumption': {'schema_version': 'subagent-consumption-v1', 'consumed_count': 1},
    }
    path = tmp_path / 'experiment.json'
    path.write_text(json.dumps(payload), encoding='utf-8')

    snapshot = _experiment_snapshot_from_payload(payload, path)

    assert snapshot is not None
    assert snapshot['budget_used']['subagents'] == 1
    assert snapshot['subagent_consumption']['consumed_count'] == 1


def test_hypotheses_visibility_reconciles_stale_selection_to_runtime_canonical_task() -> None:
    from nanobot_ops_dashboard.app import _reconcile_hypotheses_visibility_with_runtime

    visibility = {
        'selected_hypothesis_id': 'inspect-pass-streak',
        'selected_hypothesis_title': 'Inspect repeated PASS streak',
        'mismatch_reasons': [],
        'top_entries': [
            {'hypothesis_id': 'record-reward', 'title': 'Record cycle reward'},
            {'hypothesis_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak'},
        ],
    }
    runtime_parity = {
        'state': 'healthy',
        'canonical_current_task_id': 'record-reward',
        'authority_resolution': 'fresh_live_post_materialization_reward',
        'reasons': [],
    }

    reconciled = _reconcile_hypotheses_visibility_with_runtime(visibility, runtime_parity)

    assert reconciled['selected_hypothesis_id'] == 'record-reward'
    assert reconciled['selected_hypothesis_title'] == 'Record cycle reward'
    assert reconciled['runtime_reconciled_selected_hypothesis'] is True
    assert reconciled['stale_selected_hypothesis_id'] == 'inspect-pass-streak'
    assert 'selected_hypothesis_reconciled_to_runtime' in reconciled['mismatch_reasons']


def test_autonomy_verdict_blocks_historical_progress_when_recent_window_is_discard_only_and_subagents_stale(tmp_path: Path) -> None:
    analytics = {
        'recent_status_sequence': [
            {
                'status': 'PASS',
                'title': 'record-reward' if idx % 2 else 'synthesize-next-improvement-candidate',
                'detail': {
                    'current_task_id': 'record-reward' if idx % 2 else 'synthesize-next-improvement-candidate',
                    'experiment': {'outcome': 'discard', 'revert_status': 'skipped_no_material_change'},
                    'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0, 'elapsed_seconds': 0},
                },
            }
            for idx in range(8)
        ],
        'current_streak': {'status': 'PASS', 'length': 8},
    }
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=tmp_path / 'nanobot', db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    verdict = _autonomy_verdict(
        analytics=analytics,
        plan_latest={'current_task_id': 'record-reward'},
        experiment_visibility={'current_experiment': {'outcome': 'discard'}},
        credits_visibility={'current': {}},
        cfg=cfg,
        material_progress={'schema_version': 'material-progress-v1', 'state': 'proven', 'healthy_autonomy_allowed': True},
        subagent_visibility={'summary': {'result_count': 1, 'stale_result_count': 1, 'fresh_result_count': 0}},
    )

    assert verdict['state'] == 'stagnant'
    assert 'recent_window_discard_only' in verdict['reasons']
    assert 'subagent_evidence_stale' in verdict['reasons']


def test_autonomy_verdict_blocks_healthy_progress_when_subagent_request_is_queued_without_result(tmp_path: Path) -> None:
    cfg = DashboardConfig(project_root=tmp_path / 'dashboard', nanobot_repo_root=tmp_path / 'nanobot', db_path=tmp_path / 'dashboard.sqlite3', eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    verdict = _autonomy_verdict(
        analytics={'recent_status_sequence': [], 'current_streak': {'status': 'PASS', 'length': 3}},
        plan_latest={'current_task_id': 'subagent-verify-materialized-improvement'},
        experiment_visibility={'current_experiment': {'outcome': 'accept'}},
        credits_visibility={'current': {}},
        cfg=cfg,
        material_progress={'schema_version': 'material-progress-v1', 'state': 'proven', 'healthy_autonomy_allowed': True},
        subagent_visibility={'summary': {'queued_request_count': 1, 'result_count': 0, 'blocked_result_count': 0, 'stale_result_count': 0, 'fresh_result_count': 0}},
    )

    assert verdict['state'] == 'stagnant'
    assert 'subagent_request_unresolved' in verdict['reasons']


def test_ambition_utilization_escalates_rotating_synthesis_reward_window_when_subagents_and_tools_underused() -> None:
    analytics = {
        'recent_status_sequence': [
            {
                'status': 'PASS',
                'title': task_id,
                'detail': {
                    'current_task_id': task_id,
                    'materialized_improvement_artifact_path': f'workspace/state/improvements/artifact-{idx}.json',
                    'feedback_decision': {'mode': mode},
                    'experiment': {'outcome': 'discard'},
                    'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0, 'elapsed_seconds': 0},
                },
            }
            for idx, (task_id, mode) in enumerate([
                ('record-reward', 'record_reward_after_synthesized_materialization'),
                ('synthesize-next-improvement-candidate', 'synthesize_next_candidate'),
                ('record-reward', 'complete_active_lane'),
                ('synthesize-next-improvement-candidate', 'synthesize_next_candidate'),
                ('record-reward', 'record_reward_after_synthesized_materialization'),
                ('synthesize-next-improvement-candidate', 'complete_active_lane'),
            ])
        ]
    }

    verdict = _ambition_utilization_verdict(
        analytics=analytics,
        experiment_visibility={'current_experiment': {'outcome': 'discard'}},
        subagent_visibility={'summary': {'fresh_result_count': 0}},
    )

    assert verdict['state'] == 'underutilized'
    assert 'subagents_unused' in verdict['reasons']
    assert 'tool_budget_underused' in verdict['reasons']
    assert verdict['recommended_next_action'] == 'escalate_to_higher_ambition_lane_or_emit_precise_blocker'


def test_experiment_snapshot_hydrates_phase_and_subagent_consumption_from_budget(tmp_path: Path) -> None:
    path = tmp_path / 'latest.json'
    path.write_text('{}', encoding='utf-8')

    snapshot = _experiment_snapshot_from_payload(
        {'experiment_id': 'exp-1', 'status': 'PASS', 'outcome': 'discard', 'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0}},
        path,
    )

    assert snapshot['phase'] == 'completed'
    assert snapshot['subagent_consumption']['state'] == 'unused'
    assert snapshot['subagent_consumption']['used'] == 0
    assert snapshot['subagent_consumption']['source'] == 'budget_used'



def test_api_system_hydrates_unknown_blocker_from_autonomy_verdict(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'control_plane').mkdir(parents=True, exist_ok=True)
    (state_root / 'experiments').mkdir(parents=True, exist_ok=True)
    (state_root / 'credits').mkdir(parents=True, exist_ok=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True, exist_ok=True)
    (state_root / 'strong_reflection').mkdir(parents=True, exist_ok=True)
    (state_root / 'hypotheses').mkdir(parents=True, exist_ok=True)
    (state_root / 'hypotheses' / 'backlog.json').write_text(json.dumps([]), encoding='utf-8')
    (state_root / 'strong_reflection' / 'latest.json').write_text(json.dumps({
        'recorded_at_utc': '2999-04-24T12:59:00+00:00',
        'status': 'PASS',
        'summary': 'fresh test reflection',
    }), encoding='utf-8')
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({
        'task_plan': {'current_task_id': 'record-reward', 'current_task': 'Record cycle reward'},
        'current_blocker': {'kind': 'unknown'},
        'material_progress': {
            'schema_version': 'material-progress-v1',
            'state': 'blocked',
            'available': True,
            'healthy_autonomy_allowed': False,
            'proof_count': 0,
            'proofs': [],
            'qualifying_proofs': [],
            'blocking_reason': 'missing_current_material_progress',
        },
    }), encoding='utf-8')
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({'outcome': 'discard', 'revert_status': 'skipped_no_material_change'}), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({'delta': 0.0, 'reward_gate': {'status': 'suppressed'}}), encoding='utf-8')
    cfg = DashboardConfig(
        project_root=project_root,
        nanobot_repo_root=repo_root,
        db_path=db,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/state',
    )

    control = _call_json(create_app(cfg), '/api/system')['control_plane']

    assert control['current_blocker']['kind'] == 'diagnostic_gap'
    assert control['current_blocker']['source'] == 'autonomy_verdict'
    assert control['current_blocker']['failure_class'] == 'material_progress_missing'
    assert control['current_blocker']['blocked_next_step']
    assert control['blocker_summary']['state'] == 'stagnant'
    assert control['blocker_summary']['reason'] == 'material_progress_missing'
    assert control['blocker_summary']['source'] == 'autonomy_verdict'


def test_subagent_visibility_preserves_generation_scoped_identity(tmp_path: Path):
    repo = tmp_path / 'repo'
    state = repo / 'workspace' / 'state'
    requests = state / 'subagents' / 'requests'
    results = state / 'subagents' / 'results'
    requests.mkdir(parents=True)
    results.mkdir(parents=True)
    request_path = requests / 'request-cycle-a.json'
    request_id = 'subagent-verify-materialized-improvement-cycle-a-deadbeef'
    request_path.write_text(json.dumps({
        'schema_version': 'subagent-request-v1',
        'request_status': 'queued',
        'task_id': 'subagent-verify-materialized-improvement',
        'semantic_task_id': 'subagent-verify-materialized-improvement',
        'request_id': request_id,
        'verification_task_id': request_id,
        'verification_role': 'materialized_improvement_review',
        'cycle_id': 'cycle-a',
        'profile': 'research_only',
        'source_artifact': str(state / 'improvements' / 'materialized-cycle-a.json'),
    }), encoding='utf-8')
    result_path = results / f'result-{request_id}.json'
    result_path.write_text(json.dumps({
        'schema_version': 'subagent-result-v1',
        'status': 'blocked',
        'request_path': str(request_path),
        'task_id': 'subagent-verify-materialized-improvement',
        'semantic_task_id': 'subagent-verify-materialized-improvement',
        'request_id': request_id,
        'verification_task_id': request_id,
        'verification_role': 'materialized_improvement_review',
        'cycle_id': 'cycle-a',
        'source_artifact': str(state / 'improvements' / 'materialized-cycle-a.json'),
    }), encoding='utf-8')
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'dashboard.sqlite3',
        nanobot_repo_root=repo,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'key',
        eeepc_state_root=str(state),
    )

    visibility = _discover_subagent_requests(cfg)

    assert visibility['latest_request']['request_id'] == request_id
    assert visibility['latest_request']['semantic_task_id'] == 'subagent-verify-materialized-improvement'
    assert visibility['latest_request']['verification_task_id'] == request_id
    assert visibility['latest_result']['request_id'] == request_id
    assert visibility['latest_result']['semantic_task_id'] == 'subagent-verify-materialized-improvement'
    assert visibility['subagent_rollup']['latest_request']['request_id'] == request_id
    assert visibility['subagent_rollup']['latest_result']['verification_task_id'] == request_id
    assert visibility['subagent_rollup']['active_task_linkage']['request_id'] == request_id



def test_subagent_visibility_prefers_canonical_eeepc_state_over_stale_local(tmp_path: Path):
    repo = tmp_path / 'repo'
    local_state = repo / 'workspace' / 'state'
    canonical_state = tmp_path / 'canonical-eeepc-state'
    for root in (local_state, canonical_state):
        (root / 'subagents' / 'requests').mkdir(parents=True)
        (root / 'subagents' / 'results').mkdir(parents=True)
    local_req = local_state / 'subagents' / 'requests' / 'request-cycle-local.json'
    local_req.write_text(json.dumps({
        'schema_version': 'subagent-request-v1',
        'request_status': 'queued',
        'task_id': 'subagent-verify-materialized-improvement',
        'cycle_id': 'cycle-local',
    }), encoding='utf-8')
    request_id = 'subagent-verify-materialized-improvement-cycle-live-12345678'
    canonical_req = canonical_state / 'subagents' / 'requests' / 'request-cycle-live.json'
    canonical_req.write_text(json.dumps({
        'schema_version': 'subagent-request-v1',
        'request_status': 'queued',
        'task_id': 'subagent-verify-materialized-improvement',
        'semantic_task_id': 'subagent-verify-materialized-improvement',
        'request_id': request_id,
        'verification_task_id': request_id,
        'verification_role': 'materialized_improvement_review',
        'cycle_id': 'cycle-live',
        'source_artifact': str(canonical_state / 'improvements' / 'materialized-cycle-live.json'),
    }), encoding='utf-8')
    canonical_res = canonical_state / 'subagents' / 'results' / f'result-{request_id}.json'
    canonical_res.write_text(json.dumps({
        'schema_version': 'subagent-result-v1',
        'status': 'blocked',
        'request_path': str(canonical_req),
        'task_id': 'subagent-verify-materialized-improvement',
        'semantic_task_id': 'subagent-verify-materialized-improvement',
        'request_id': request_id,
        'verification_task_id': request_id,
        'verification_role': 'materialized_improvement_review',
        'cycle_id': 'cycle-live',
        'source_artifact': str(canonical_state / 'improvements' / 'materialized-cycle-live.json'),
    }), encoding='utf-8')
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'dashboard.sqlite3',
        nanobot_repo_root=repo,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root=str(canonical_state),
    )

    visibility = _discover_subagent_requests(cfg)

    assert visibility['source']['selected'] == 'eeepc'
    assert visibility['source']['local_state_root'] == str(local_state)
    assert visibility['source']['canonical_state_root'] == str(canonical_state)
    assert visibility['source_skew']['state'] == 'skewed'
    assert visibility['latest_request']['request_id'] == request_id
    assert visibility['latest_result']['request_id'] == request_id
    assert visibility['latest_result']['verification_task_id'] == request_id
    assert visibility['summary']['sources'] == ['eeepc']



def test_subagent_visibility_uses_remote_canonical_state_when_not_local(tmp_path: Path, monkeypatch):
    repo = tmp_path / 'repo'
    local_state = repo / 'workspace' / 'state'
    (local_state / 'subagents' / 'requests').mkdir(parents=True)
    (local_state / 'subagents' / 'requests' / 'request-cycle-local.json').write_text(json.dumps({
        'schema_version': 'subagent-request-v1',
        'request_status': 'queued',
        'task_id': 'subagent-verify-materialized-improvement',
        'cycle_id': 'cycle-local',
    }), encoding='utf-8')
    request_id = 'subagent-verify-materialized-improvement-cycle-remote-abcdef12'
    remote_root = '/var/lib/eeepc-agent/self-evolving-agent/state'
    monkeypatch.setattr(dashboard_app, '_remote_subagent_state_payload', lambda cfg, state_root: {
        'ok': True,
        'source_root': remote_root,
        'requests': [{
            'path': f'{remote_root}/subagents/requests/request-cycle-remote.json',
            'source': 'eeepc',
            'source_root': remote_root,
            'task_id': 'subagent-verify-materialized-improvement',
            'semantic_task_id': 'subagent-verify-materialized-improvement',
            'request_id': request_id,
            'verification_task_id': request_id,
            'verification_role': 'materialized_improvement_review',
            'cycle_id': 'cycle-remote',
            'request_status': 'queued',
            'status': 'queued',
            'age_seconds': 3,
        }],
        'results': [{
            'path': f'{remote_root}/subagents/results/result-{request_id}.json',
            'source': 'eeepc',
            'source_root': remote_root,
            'request_path': f'{remote_root}/subagents/requests/request-cycle-remote.json',
            'task_id': 'subagent-verify-materialized-improvement',
            'semantic_task_id': 'subagent-verify-materialized-improvement',
            'request_id': request_id,
            'verification_task_id': request_id,
            'verification_role': 'materialized_improvement_review',
            'cycle_id': 'cycle-remote',
            'status': 'blocked',
            'age_seconds': 3,
        }],
    })
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'dashboard.sqlite3',
        nanobot_repo_root=repo,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root=remote_root,
    )

    visibility = _discover_subagent_requests(cfg)

    assert visibility['source']['selected'] == 'eeepc'
    assert visibility['source']['canonical_remote'] is True
    assert visibility['latest_request']['request_id'] == request_id
    assert visibility['latest_result']['request_id'] == request_id
    assert visibility['summary']['sources'] == ['eeepc']
    assert visibility['source_skew']['state'] == 'skewed'



def test_remote_subagent_fetch_uses_sudo_password_and_record_limit(tmp_path: Path, monkeypatch):
    captured = {}
    class Completed:
        stdout = json.dumps({'ok': True, 'source_root': '/remote/state', 'requests': [], 'results': []})
    def fake_run(cmd, capture_output, text, timeout, check):
        captured['cmd'] = cmd
        captured['timeout'] = timeout
        return Completed()
    monkeypatch.setattr(dashboard_app.subprocess, 'run', fake_run)
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'dashboard.sqlite3',
        nanobot_repo_root=tmp_path / 'repo',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/remote/state',
        eeepc_sudo_password='dummy-password',
        max_subagent_records=7,
    )

    payload = dashboard_app._remote_subagent_state_payload(cfg, '/remote/state')

    assert payload['ok'] is True
    remote_command = captured['cmd'][-1]
    assert "sudo -S -p ''" in remote_command
    assert 'dummy-password' in remote_command
    assert '/remote/state' in remote_command
    assert remote_command.rstrip().endswith(' 7')



def test_control_plane_material_progress_prefers_canonical_eeepc_over_stale_local(tmp_path: Path):
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'dashboard.sqlite3',
        nanobot_repo_root=tmp_path / 'repo',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'missing-key',
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )
    repo_latest = {
        'active_goal': 'goal-bootstrap',
        'status': 'PASS',
        'raw_json': json.dumps({
            'material_progress': {
                'schema_version': 'material-progress-v1',
                'state': 'blocked',
                'blocking_reason': 'missing_current_material_progress',
                'proofs': [{
                    'kind': 'consumed_subagent_result',
                    'present': False,
                    'reason': 'subagent_result_missing',
                    'evidence': {'latest_result_path': str(tmp_path / 'repo' / 'workspace' / 'state' / 'subagents' / 'results' / 'stale.json')},
                }],
                'qualifying_proofs': [],
            }
        }),
    }
    eeepc_latest = {
        'active_goal': 'goal-bootstrap',
        'status': 'PASS',
        'raw_json': json.dumps({
            'material_progress': {
                'schema_version': 'material-progress-v1',
                'state': 'blocked',
                'blocking_reason': 'delegated_verification_terminal_blocked',
                'proofs': [{
                    'kind': 'consumed_subagent_result',
                    'present': True,
                    'reason': 'subagent_result_terminal_blocked',
                    'evidence': {
                        'source': 'eeepc',
                        'source_root': '/var/lib/eeepc-agent/self-evolving-agent/state',
                        'request_id': 'subagent-verify-materialized-improvement-cycle-live-12345678',
                        'verification_task_id': 'subagent-verify-materialized-improvement-cycle-live-12345678',
                        'latest_result_path': '/var/lib/eeepc-agent/self-evolving-agent/state/subagents/results/result-live.json',
                        'terminal_reason': 'local_executor_unavailable',
                    },
                }],
                'qualifying_proofs': [],
            }
        }),
    }

    summary = dashboard_app._control_plane_summary(repo_latest, eeepc_latest, {}, {}, cfg)

    material = summary['material_progress']
    assert material['blocking_reason'] == 'delegated_verification_terminal_blocked'
    assert material['proofs'][0]['present'] is True
    assert material['proofs'][0]['reason'] == 'subagent_result_terminal_blocked'
    assert material['proofs'][0]['evidence']['source_root'] == '/var/lib/eeepc-agent/self-evolving-agent/state'
    assert material['proofs'][0]['evidence']['request_id'] == 'subagent-verify-materialized-improvement-cycle-live-12345678'
    encoded = json.dumps(material)
    assert 'subagent_result_missing' not in encoded
    assert '/workspace/state/subagents/results/stale.json' not in encoded

