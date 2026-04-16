from __future__ import annotations

from pathlib import Path
from wsgiref.util import setup_testing_defaults

from nanobot_ops_dashboard.app import create_app
from nanobot_ops_dashboard.config import DashboardConfig
from nanobot_ops_dashboard.storage import init_db, insert_collection, upsert_event


def _call_app(app, path='/', query_string=''):
    captured = {}

    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = headers

    environ = {}
    setup_testing_defaults(environ)
    environ['PATH_INFO'] = path
    environ['QUERY_STRING'] = query_string
    body = b''.join(app(environ, start_response)).decode('utf-8')
    return captured['status'], body


def _seed_dashboard_data(db: Path) -> None:
    insert_collection(db, {
        'collected_at': '2026-04-16T12:00:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-1',
        'approval_gate': '{"ok": true, "reason": "valid"}',
        'gate_state': 'valid',
        'report_source': '/state/reports/evolution-1.json',
        'outbox_source': '/state/outbox/report.index.json',
        'artifact_paths_json': '["prompts/diagnostics.md"]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': '{"outbox": {"status": "BLOCK", "process_reflection": {"failure_class": "no_concrete_change", "improvement_score": 30}, "goal": {"follow_through": {"blocked_next_step": "Rewrite the cycle around one file-level action or an explicit blocked next step."}}}}',
    })
    insert_collection(db, {
        'collected_at': '2026-04-16T12:05:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-1',
        'approval_gate': '{"ok": true, "reason": "valid"}',
        'gate_state': 'valid',
        'report_source': '/state/reports/evolution-1.json',
        'outbox_source': '/state/outbox/report.index.json',
        'artifact_paths_json': '["prompts/diagnostics.md"]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': '{"outbox": {"status": "BLOCK", "process_reflection": {"failure_class": "no_concrete_change", "improvement_score": 30}, "goal": {"follow_through": {"blocked_next_step": "Rewrite the cycle around one file-level action or an explicit blocked next step."}}}}',
    })
    insert_collection(db, {
        'collected_at': '2026-04-16T12:10:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-1',
        'approval_gate': '{"ok": true, "reason": "valid"}',
        'gate_state': 'valid',
        'report_source': '/state/reports/evolution-1.json',
        'outbox_source': '/state/outbox/report.index.json',
        'artifact_paths_json': '["prompts/diagnostics.md"]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': '{"outbox": {"status": "BLOCK", "process_reflection": {"failure_class": "no_concrete_change", "improvement_score": 30}, "goal": {"follow_through": {"blocked_next_step": "Rewrite the cycle around one file-level action or an explicit blocked next step."}}}}',
    })
    insert_collection(db, {
        'collected_at': '2026-04-16T12:00:01Z',
        'source': 'repo',
        'status': 'unknown',
        'active_goal': None,
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': 'promotion-42 | reviewed | accept',
        'promotion_candidate_path': '/workspace/state/promotions/promotion-42.json',
        'promotion_decision_record': 'present',
        'promotion_accepted_record': 'present',
        'raw_json': '{}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:00Z',
        'source': 'eeepc',
        'event_type': 'cycle',
        'identity_key': '/state/reports/evolution-1.json',
        'title': 'goal-1',
        'status': 'PASS',
        'detail_json': '{"report_source": "/state/reports/evolution-1.json", "artifact_paths": ["prompts/diagnostics.md"], "approval": {"ok": true, "reason": "valid"}, "failure_class": "no_concrete_change", "blocked_next_step": "Rewrite the cycle around one file-level action or an explicit blocked next step."}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:01Z',
        'source': 'repo',
        'event_type': 'promotion',
        'identity_key': 'promotion-42',
        'title': 'promotion-42 | reviewed | accept',
        'status': 'accept',
        'detail_json': '{"candidate_path": "/workspace/state/promotions/promotion-42.json", "decision_record": "present", "accepted_record": "present"}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:02Z',
        'source': 'repo',
        'event_type': 'cycle',
        'identity_key': '/workspace/state/reports/evolution-2.json',
        'title': 'goal-2',
        'status': 'BLOCK',
        'detail_json': '{"report_source": "/workspace/state/reports/evolution-2.json", "artifact_paths": [], "approval": {"ok": false, "reason": "missing"}}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:03Z',
        'source': 'repo',
        'event_type': 'subagent',
        'identity_key': 'sub-1',
        'title': 'widget-fix',
        'status': 'ok',
        'detail_json': '{"task": "fix the widget", "label": "widget-fix", "started_at": "2026-04-16T12:00:00Z", "finished_at": "2026-04-16T12:01:00Z", "goal_id": "goal-1", "cycle_id": "cycle-1", "report_path": "/workspace/state/reports/evolution-1.json", "origin": {"channel": "cli", "chat_id": "direct"}, "parent_context": {"session_key": "session-1", "origin": {"channel": "cli", "chat_id": "direct"}}, "summary": "done", "result": "done", "source_path": "/workspace/state/subagents/sub-1.json"}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:04Z',
        'source': 'eeepc',
        'event_type': 'subagent',
        'identity_key': 'sub-2',
        'title': 'browser-report',
        'status': 'BLOCK',
        'detail_json': '{"task": "prepare browser report", "label": "browser-report", "started_at": "2026-04-16T12:00:02Z", "finished_at": "2026-04-16T12:00:05Z", "origin": {"channel": "browser", "chat_id": "ops"}, "summary": "needs more evidence", "result": "needs more evidence", "source_path": "/workspace/state/subagents/sub-2.json"}',
    })


def _cfg(tmp_path: Path, db: Path) -> DashboardConfig:
    return DashboardConfig(
        project_root=Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard'),
        db_path=db,
        nanobot_repo_root=tmp_path / 'nanobot',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=Path('/tmp/fake'),
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )


def test_app_collect_endpoint_surfaces_diagnostic_errors(tmp_path: Path, monkeypatch):
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    app = create_app(_cfg(tmp_path, db))

    monkeypatch.setattr(
        'nanobot_ops_dashboard.app.collect_once',
        lambda _cfg: {
            'repo_status': 'PASS',
            'repo_goal': 'goal-1',
            'repo_collection_status': 'ok',
            'repo_error': None,
            'eeepc_status': 'error',
            'eeepc_goal': None,
            'eeepc_collection_status': 'error',
            'eeepc_error': {
                'source': 'eeepc',
                'stage': 'ssh:/state/outbox/report.index.json',
                'message': 'ssh: connect to host 192.168.1.44 port 22: No route to host',
                'error_type': 'CalledProcessError',
                'returncode': 255,
            },
            'eeepc_reachability': {
                'reachable': False,
                'ssh_host': 'eeepc',
                'target': 'eeepc',
                'error': 'ssh: connect to host 192.168.1.44 port 22: No route to host',
                'returncode': 255,
                'recommended_next_action': 'Treat as a control-plane incident; verify eeepc power/network access, then retry collection.',
                'control_artifact_path': '/tmp/eeepc_reachability.json',
            },
            'collection_status': {'repo': 'ok', 'eeepc': 'error'},
        },
    )

    status, body = _call_app(app, '/collect')
    assert status.startswith('200')
    assert 'eeepc_collection_status' in body
    assert 'eeepc_reachability' in body
    assert 'No route to host' in body
    assert 'collection_status' in body


def test_app_overview_renders(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/')
    assert status.startswith('200')
    assert 'Nanobot Ops Dashboard' in body
    assert 'Last collected' in body
    assert 'Loaded snapshot window' in body
    assert 'Historical snapshots in DB' in body
    assert 'Freshness / reachability' in body
    assert 'View eeepc cycles' in body
    assert 'prompts/diagnostics.md' in body
    assert 'http-equiv="refresh"' in body
    assert 'Current blocker' in body
    assert 'no_concrete_change' in body
    assert 'Rewrite the cycle around one file-level action' in body
    assert 'Collection Summary' in body
    assert 'Outbox' in body
    assert 'Recent cycle timeline' in body
    assert 'Recent goal transitions' in body
    assert 'status-pill status-pass' in body
    assert 'status-pill status-block' in body
    assert 'timeline-item status-pass' in body
    assert 'timeline-item status-block' in body
    assert 'Observation cadence' in body
    assert 'Fresh report first seen' in body
    assert 'Snapshot age' in body


def test_app_cycles_filters_and_api_render(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, cycles_body = _call_app(app, '/cycles')
    assert status.startswith('200')
    assert 'Detail' in cycles_body
    assert 'Loaded cycle rows' in cycles_body
    assert 'Unique eeepc reports' in cycles_body
    assert 'Observation groups' in cycles_body
    assert 'Repeated observations' in cycles_body
    assert 'PASS' in cycles_body
    assert 'prompts/diagnostics.md' in cycles_body
    assert 'Report source' in cycles_body
    assert '/state/reports/evolution-1.json' in cycles_body
    assert 'Approval' in cycles_body
    assert 'Observed eeepc collection cadence' in cycles_body
    assert 'Seen' in cycles_body
    assert '5.0 min' in cycles_body

    status, filtered_cycles = _call_app(app, '/cycles', 'source=repo&status=BLOCK')
    assert status.startswith('200')
    assert 'goal-2' in filtered_cycles
    assert '/workspace/state/reports/evolution-2.json' in filtered_cycles
    assert 'name="source"' in filtered_cycles
    assert 'name="status"' in filtered_cycles
    assert 'value="repo"' in filtered_cycles

    status, cycles_api = _call_app(app, '/api/cycles', 'source=repo&status=BLOCK')
    assert status.startswith('200')
    assert 'goal-2' in cycles_api
    assert 'promotion-42' not in cycles_api


def test_app_promotions_and_other_pages_render(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, api_body = _call_app(app, '/api/summary')
    assert status.startswith('200')
    assert 'goal-1' in api_body
    assert 'PASS' in api_body
    assert 'snapshot_count' in api_body
    assert 'loaded_snapshot_count' in api_body
    assert 'total_snapshot_count' in api_body

    status, promotions_body = _call_app(app, '/promotions')
    assert status.startswith('200')
    assert 'promotion-42 | reviewed | accept' in promotions_body
    assert '/workspace/state/promotions/promotion-42.json' in promotions_body
    assert 'Decision record' in promotions_body
    assert 'Accepted record' in promotions_body
    assert 'status-pill status-pass' in promotions_body or 'status-pill status-unknown' in promotions_body

    status, filtered_promotions = _call_app(app, '/promotions', 'source=repo&status=accept')
    assert status.startswith('200')
    assert 'promotion-42 | reviewed | accept' in filtered_promotions
    assert 'name="source"' in filtered_promotions
    assert 'name="status"' in filtered_promotions

    status, promotions_api = _call_app(app, '/api/promotions', 'source=repo&status=accept')
    assert status.startswith('200')
    assert 'promotion-42' in promotions_api
    assert 'accepted_record' in promotions_api

    status, approvals_api = _call_app(app, '/api/approvals')
    assert status.startswith('200')
    assert 'valid' in approvals_api

    status, approvals_body = _call_app(app, '/approvals')
    assert status.startswith('200')
    assert 'Approvals' in approvals_body
    assert 'Source' in approvals_body
    assert 'Gate state' in approvals_body
    assert 'valid' in approvals_body

    status, deployments_api = _call_app(app, '/api/deployments')
    assert status.startswith('200')
    assert '/state/reports/evolution-1.json' in deployments_api
    assert 'eeepc_latest_observation' in deployments_api

    status, deployments_body = _call_app(app, '/deployments')
    assert status.startswith('200')
    assert 'Deployments / Verification' in deployments_body
    assert 'Live eeepc proof' in deployments_body
    assert '/state/reports/evolution-1.json' in deployments_body
    assert 'Observation cadence' in deployments_body
    assert 'Fresh report first seen' in deployments_body


def test_app_analytics_renders_failure_breakdown(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/analytics')
    assert status.startswith('200')
    assert 'Analytics' in body
    assert 'Historical snapshots in DB' in body
    assert 'Loaded snapshot window' in body
    assert 'Source breakdown' in body
    assert 'Cycle status breakdown' in body
    assert 'Freshness' in body
    assert 'Recent snapshots' in body
    assert 'Observed eeepc collections' in body
    assert 'Recent unique cycle reports' in body
    assert 'Recent goal transitions' in body

    status, analytics_api = _call_app(app, '/api/analytics')
    assert status.startswith('200')
    assert 'eeepc_observation_groups' in analytics_api
    assert 'approx_cadence_minutes' in analytics_api


def test_app_subagents_renders_durable_history(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/subagents')
    assert status.startswith('200')
    assert 'Subagents' in body
    assert 'Durable rows' in body
    assert 'Goal / cycle' in body
    assert 'Apply filters' in body
    assert 'name="origin"' in body
    assert 'name="status"' in body
    assert 'browser-report' in body.split('widget-fix')[0]
    assert 'browser-report' in body
    assert 'widget-fix' in body
    assert 'goal-1' in body
    assert 'cycle-1' in body
    assert '/workspace/state/reports/evolution-1.json' in body
    assert 'prepare browser report' in body
    assert 'fix the widget' in body
    assert 'session-1' in body
    assert 'state/subagents/sub-1.json' in body
    assert 'state/subagents/sub-2.json' in body

    status, filtered_body = _call_app(app, '/subagents', 'source=repo&origin=cli:direct&status=ok')
    assert status.startswith('200')
    assert 'widget-fix' in filtered_body
    assert 'browser-report' not in filtered_body
    assert 'name="origin"' in filtered_body
    assert 'selected' in filtered_body


def test_app_subagents_handles_missing_telemetry(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/subagents')
    assert status.startswith('200')
    assert 'No durable subagent telemetry has been collected yet.' in body
    assert 'state/subagents/*.json' in body
    assert 'Apply filters' not in body
    assert 'No subagent rows match the selected filters.' not in body
