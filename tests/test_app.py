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
        'detail_json': '{"report_source": "/state/reports/evolution-1.json", "artifact_paths": ["prompts/diagnostics.md"], "approval": {"ok": true, "reason": "valid"}}',
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
    assert 'Report source' in cycles_body
    assert '/state/reports/evolution-1.json' in cycles_body
    assert 'Approval' in cycles_body

    status, api_body = _call_app(app, '/api/summary')
    assert status.startswith('200')
    assert 'goal-1' in api_body
    assert 'PASS' in api_body
    assert 'snapshot_count' in api_body

    status, promotions_body = _call_app(app, '/promotions')
    assert status.startswith('200')
    assert 'promotion-42 | reviewed | accept' in promotions_body
    assert '/workspace/state/promotions/promotion-42.json' in promotions_body
    assert 'Decision record' in promotions_body
    assert 'Accepted record' in promotions_body

    status, filtered_cycles = _call_app(app, '/cycles', 'source=repo&status=BLOCK')
    assert status.startswith('200')
    assert 'goal-2' in filtered_cycles
    assert '/workspace/state/reports/evolution-2.json' in filtered_cycles
    assert 'goal-1' not in filtered_cycles

    status, filtered_promotions = _call_app(app, '/promotions', 'source=repo&status=accept')
    assert status.startswith('200')
    assert 'promotion-42 | reviewed | accept' in filtered_promotions

    status, analytics_body = _call_app(app, '/analytics')
    assert status.startswith('200')
    assert 'Analytics' in analytics_body
    assert 'Total snapshots' in analytics_body
    assert 'Source breakdown' in analytics_body
    assert 'Cycle status breakdown' in analytics_body

    status, approvals_body = _call_app(app, '/approvals')
    assert status.startswith('200')
    assert 'Approvals' in approvals_body
    assert 'Source' in approvals_body
    assert 'Gate state' in approvals_body
    assert 'valid' in approvals_body

    status, deployments_body = _call_app(app, '/deployments')
    assert status.startswith('200')
    assert 'Deployments / Verification' in deployments_body
    assert 'Live eeepc proof' in deployments_body
    assert '/state/reports/evolution-1.json' in deployments_body
