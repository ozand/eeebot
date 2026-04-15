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


def _cfg(tmp_path: Path, db: Path) -> DashboardConfig:
    return DashboardConfig(
        project_root=Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard'),
        db_path=db,
        nanobot_repo_root=tmp_path / 'nanobot',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=Path('/tmp/fake'),
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )


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
    assert 'Stored snapshots' in body
    assert 'prompts/diagnostics.md' in body
    assert 'http-equiv="refresh"' in body
    assert 'Current blocker' in body
    assert 'no_concrete_change' in body
    assert 'Rewrite the cycle around one file-level action' in body
    assert 'Collection Summary' in body
    assert 'Outbox:' in body


def test_app_cycles_filters_and_api_render(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, cycles_body = _call_app(app, '/cycles')
    assert status.startswith('200')
    assert 'Detail' in cycles_body
    assert 'PASS' in cycles_body
    assert 'prompts/diagnostics.md' in cycles_body
    assert 'Report source' in cycles_body
    assert '/state/reports/evolution-1.json' in cycles_body
    assert 'Approval' in cycles_body

    status, filtered_cycles = _call_app(app, '/cycles', 'source=repo&status=BLOCK')
    assert status.startswith('200')
    assert 'goal-2' in filtered_cycles
    assert '/workspace/state/reports/evolution-2.json' in filtered_cycles
    assert 'goal-1' not in filtered_cycles
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

    status, promotions_body = _call_app(app, '/promotions')
    assert status.startswith('200')
    assert 'promotion-42 | reviewed | accept' in promotions_body
    assert '/workspace/state/promotions/promotion-42.json' in promotions_body
    assert 'Decision record' in promotions_body
    assert 'Accepted record' in promotions_body

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

    status, deployments_body = _call_app(app, '/deployments')
    assert status.startswith('200')
    assert 'Deployments / Verification' in deployments_body
    assert 'Live eeepc proof' in deployments_body
    assert '/state/reports/evolution-1.json' in deployments_body


def test_app_analytics_renders_failure_breakdown(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/analytics')
    assert status.startswith('200')
    assert 'Analytics' in body
    assert 'Total snapshots' in body
    assert 'Source breakdown' in body
    assert 'Cycle status breakdown' in body
    assert 'Recent snapshots' in body
    assert 'Recent cycles' in body
    assert 'no_concrete_change' in body
    assert 'Current PASS streak' in body
    assert 'Current BLOCK streak' in body
    assert 'Latest PASS' in body
    assert 'Latest BLOCK' in body
    assert 'Top goals' in body
    assert 'Top BLOCK reasons' in body
    assert 'Latest artifact history' in body
    assert 'goal-1' in body
    assert 'prompts/diagnostics.md' in body

    status, api_body = _call_app(app, '/api/analytics')
    assert status.startswith('200')
    assert 'current_pass_streak' in api_body
    assert 'current_block_streak' in api_body
    assert 'latest_pass_at' in api_body
    assert 'latest_block_at' in api_body
    assert 'top_goals' in api_body
    assert 'top_block_reasons' in api_body
    assert 'artifact_history' in api_body
