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


def _seed_pass_cycle(db: Path, idx: int, task_id: str = 'subagent-verify-materialized-improvement') -> None:
    stamp = f'2026-04-24T12:{idx:02d}:00Z'
    raw = {
        'current_plan': {
            'current_task_id': task_id,
            'current_task': task_id,
            'feedback_decision': {'mode': 'handoff_to_next_candidate', 'selected_task_id': task_id},
            'task_selection_source': 'feedback_review_to_execution',
            'selected_tasks': f'{task_id} [task_id={task_id}]',
        },
        'outbox': {'status': 'PASS'},
    }
    insert_collection(db, {
        'collected_at': stamp,
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': f'/workspace/state/reports/evolution-{idx}.json',
        'outbox_source': '/workspace/state/outbox/latest.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps(raw),
    })
    upsert_event(db, {
        'collected_at': stamp,
        'source': 'repo',
        'event_type': 'cycle',
        'identity_key': f'cycle-{idx}',
        'title': task_id,
        'status': 'PASS',
        'detail_json': json.dumps({'current_task_id': task_id}),
    })


def _write_control_plane_summary(project_root: Path, *, material_progress: dict | None = None, task_plan: dict | None = None) -> None:
    summary: dict[str, object] = {}
    if material_progress is not None:
        summary['material_progress'] = material_progress
    if task_plan is not None:
        summary['task_plan'] = task_plan
    path = project_root / 'workspace' / 'state' / 'control_plane' / 'current_summary.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary), encoding='utf-8')


def test_api_subagents_returns_json_with_stale_queued_request(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    req_dir = state_root / 'subagents' / 'requests'
    req_dir.mkdir(parents=True)
    req = req_dir / 'request-cycle-old.json'
    req.write_text(json.dumps({'schema_version': 'subagent-request-v1', 'request_status': 'queued', 'task_id': 'subagent-verify-materialized-improvement'}), encoding='utf-8')
    old = time.time() - 3 * 3600
    os.utime(req, (old, old))
    upsert_event(db, {
        'collected_at': '2026-04-24T12:00:00Z',
        'source': 'repo',
        'event_type': 'subagent',
        'identity_key': 'subagent-old',
        'title': 'subagent verify',
        'status': 'queued',
        'detail_json': json.dumps({'task_id': 'subagent-verify-materialized-improvement'}),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    payload = _call_json(create_app(cfg), '/api/subagents')

    assert payload['summary']['total_events'] == 1
    assert payload['summary']['stale_request_count'] == 1
    assert payload['requests'][0]['status'] == 'stale'
    assert payload['requests'][0]['task_id'] == 'subagent-verify-materialized-improvement'


def test_dashboard_autonomy_verdict_flags_pass_but_stagnant(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True)
    (state_root / 'credits').mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    for i in range(10):
        _seed_pass_cycle(db, i)
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({'outcome': 'discard', 'revert_status': 'skipped_no_material_change'}), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({'delta': 0.0, 'reward_gate': {'status': 'suppressed'}}), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({'status': 'terminal_noop'}), encoding='utf-8')
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    analytics = _call_json(app, '/api/analytics')['analytics']
    system = _call_json(app, '/api/system')

    assert analytics['current_streak']['status'] == 'PASS'
    assert analytics['autonomy_verdict']['state'] == 'stagnant'
    assert 'same_task_streak' in analytics['autonomy_verdict']['reasons']
    assert 'discarded_experiment' in analytics['autonomy_verdict']['reasons']
    assert system['autonomy_verdict']['state'] == 'stagnant'


def test_dashboard_api_surfaces_shared_autonomy_verdict_and_material_progress(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True)
    (state_root / 'credits').mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    for i in range(10):
        _seed_pass_cycle(db, i)
    _write_control_plane_summary(
        project_root,
        material_progress={
            'schema_version': 'material-progress-v1',
            'state': 'proven',
            'available': True,
            'healthy_autonomy_allowed': True,
            'proof_count': 1,
            'proofs': ['workspace/state/material/proof.md'],
            'qualifying_proofs': ['workspace/state/material/proof.md'],
            'blocking_reason': None,
        },
        task_plan={
            'current_task_id': 'subagent-verify-materialized-improvement',
            'current_task': 'subagent-verify-materialized-improvement',
        },
    )
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({'outcome': 'discard', 'revert_status': 'skipped_no_material_change'}), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({'delta': 0.0, 'reward_gate': {'status': 'suppressed'}}), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({'status': 'terminal_noop'}), encoding='utf-8')
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    analytics = _call_json(app, '/api/analytics')['analytics']
    system = _call_json(app, '/api/system')
    plan = _call_json(app, '/api/plan')
    experiments = _call_json(app, '/api/experiments')

    assert analytics['autonomy_verdict']['state'] == 'stagnant'
    assert system['autonomy_verdict'] == analytics['autonomy_verdict']
    assert system['control_plane']['autonomy_verdict'] == analytics['autonomy_verdict']
    assert system['control_plane']['material_progress']['state'] == 'proven'
    assert system['material_progress']['state'] == 'proven'
    assert plan['material_progress']['state'] == 'proven'
    assert plan['material_progress']['available'] is True
    assert experiments['material_progress']['state'] == 'proven'
    assert experiments['material_progress']['available'] is True


def test_dashboard_api_surfaces_unavailable_material_progress_when_missing(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    system = _call_json(app, '/api/system')
    plan = _call_json(app, '/api/plan')
    experiments = _call_json(app, '/api/experiments')

    for payload in (system, plan, experiments):
        material_progress = payload['material_progress']
        assert material_progress['state'] == 'unavailable'
        assert material_progress['available'] is False
        assert material_progress['reason'] == 'material_progress_unavailable'


def test_system_api_reports_legacy_reward_loop_parity(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    insert_collection(db, {
        'collected_at': '2026-04-24T12:00:00Z',
        'source': 'repo',
        'status': 'PASS',
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
        'raw_json': json.dumps({'current_plan': {'current_task_id': 'subagent-verify-materialized-improvement', 'feedback_decision': {'mode': 'handoff_to_next_candidate'}}}),
    })
    insert_collection(db, {
        'collected_at': '2026-04-24T12:00:01Z',
        'source': 'eeepc',
        'status': 'PASS',
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
        'raw_json': json.dumps({'current_plan': {'selected_tasks': 'Record cycle reward [task_id=record-reward]', 'task_selection_source': 'recorded_current_task', 'feedback_decision': None}}),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    system = _call_json(create_app(cfg), '/api/system')

    assert system['runtime_parity']['state'] == 'legacy_reward_loop'
    assert 'live_feedback_decision_missing' in system['runtime_parity']['reasons']
    assert 'current_task_drift' in system['runtime_parity']['reasons']
    assert 'live_hadi_artifacts_missing' in system['runtime_parity']['reasons']
    assert system['runtime_parity']['local_current_task_id'] == 'subagent-verify-materialized-improvement'
    assert system['runtime_parity']['live_current_task_id'] == 'Record cycle reward [task_id=record-reward]'


def test_api_subagents_materializes_terminal_telemetry_for_queued_request(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    req_dir = state_root / 'subagents' / 'requests'
    req_dir.mkdir(parents=True)
    req = req_dir / 'request-old.json'
    req.write_text(json.dumps({'request_status': 'queued', 'task_id': 'inspect-pass-streak'}), encoding='utf-8')
    old = time.time() - 3 * 3600
    os.utime(req, (old, old))
    terminal_result = state_root / 'subagents' / 'terminal-result.json'
    terminal_result.write_text(json.dumps({'status': 'done', 'task_id': 'inspect-pass-streak', 'summary': 'bounded review completed'}), encoding='utf-8')
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    payload = _call_json(create_app(cfg), '/api/subagents')

    assert payload['summary']['state'] == 'completed'
    assert payload['summary']['stale_request_count'] == 0
    assert payload['summary']['result_count'] == 1
    assert payload['subagent_rollup']['latest_request']['materialized_result_path'].endswith('terminal-result.json')


def test_runtime_parity_is_shared_by_system_control_plane_and_analytics(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    insert_collection(db, {
        'collected_at': '2026-04-24T12:00:00Z',
        'source': 'repo',
        'status': 'PASS',
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
        'raw_json': json.dumps({'current_plan': {'current_task_id': 'inspect-pass-streak', 'feedback_decision': {'mode': 'continue_active_lane'}}}),
    })
    insert_collection(db, {
        'collected_at': '2026-04-24T12:00:01Z',
        'source': 'eeepc',
        'status': 'PASS',
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
        'raw_json': json.dumps({'current_plan': {'selected_tasks': 'Record cycle reward [task_id=record-reward]', 'task_selection_source': 'recorded_current_task', 'feedback_decision': None}}),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    system = _call_json(app, '/api/system')
    analytics = _call_json(app, '/api/analytics')['analytics']

    assert system['runtime_parity']['state'] == 'legacy_reward_loop'
    assert system['control_plane']['runtime_parity'] == system['runtime_parity']
    assert analytics['runtime_parity'] == system['runtime_parity']
    assert analytics['autonomy_verdict']['state'] == 'stagnant'
    assert 'runtime_parity_blocked' in analytics['autonomy_verdict']['reasons']
