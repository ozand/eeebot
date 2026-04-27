from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from nanobot_ops_dashboard.app import create_app
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

    subagents = _call_json(app, '/api/subagents')
    assert subagents['latest_request']['task_id'] == 'record-reward'
    assert subagents['latest_result']['task_id'] == 'record-reward'
    assert subagents['latest_telemetry']['title'] == 'record-reward'


def test_api_system_exposes_eeepc_privileged_rollout_readiness_from_source_errors(tmp_path: Path) -> None:
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

    assert parity['state'] == 'healthy'
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

    assert parity['state'] == 'healthy'
    assert 'current_task_drift' not in parity['reasons']
    assert parity['canonical_current_task_id'] == 'inspect-pass-streak'
    assert parity['authority_resolution'] == 'fresh_live_pass_streak_switch'


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
