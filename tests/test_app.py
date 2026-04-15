from __future__ import annotations

from pathlib import Path
from wsgiref.util import setup_testing_defaults

from nanobot_ops_dashboard.app import create_app
from nanobot_ops_dashboard.config import DashboardConfig
from nanobot_ops_dashboard.storage import init_db, insert_collection, upsert_event


def _call_app(app, path='/'):
    captured = {}
    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = headers
    environ = {}
    setup_testing_defaults(environ)
    environ['PATH_INFO'] = path
    body = b''.join(app(environ, start_response)).decode('utf-8')
    return captured['status'], body


def test_app_overview_renders(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
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
        'raw_json': '{}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:00Z',
        'source': 'eeepc',
        'event_type': 'cycle',
        'identity_key': '/state/reports/evolution-1.json',
        'title': 'goal-1',
        'status': 'PASS',
        'detail_json': '{"artifact_paths": ["prompts/diagnostics.md"]}',
    })
    cfg = DashboardConfig(
        project_root=Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard'),
        db_path=db,
        nanobot_repo_root=tmp_path / 'nanobot',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=Path('/tmp/fake'),
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )
    app = create_app(cfg)
    status, body = _call_app(app, '/')
    assert status.startswith('200')
    assert 'Nanobot Ops Dashboard' in body
    assert 'Last collected' in body
    assert 'Stored snapshots' in body
    assert 'prompts/diagnostics.md' in body
    assert 'http-equiv="refresh"' in body

    status, cycles_body = _call_app(app, '/cycles')
    assert status.startswith('200')
    assert 'Detail' in cycles_body
    assert 'PASS' in cycles_body
    assert 'prompts/diagnostics.md' in cycles_body

    status, api_body = _call_app(app, '/api/summary')
    assert status.startswith('200')
    assert 'goal-1' in api_body
    assert 'PASS' in api_body
    assert 'snapshot_count' in api_body
