from __future__ import annotations

import json
import os
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
