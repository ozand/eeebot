from pathlib import Path
import json

import nanobot_ops_dashboard.app as dashboard_app
from nanobot_ops_dashboard.app import create_app
from nanobot_ops_dashboard.storage import init_db
from test_app import _call_app, _cfg, _seed_dashboard_data, _seed_experiment_telemetry, _seed_hypothesis_backlog


def test_system_page_shows_local_ci_and_export_status(tmp_path: Path, monkeypatch):
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    _seed_experiment_telemetry(tmp_path)
    _seed_hypothesis_backlog(tmp_path)

    original_control_plane_summary = dashboard_app._control_plane_summary

    def _patched_control_plane_summary(repo_latest, eeepc_latest, current_experiment, current_blocker, cfg):
        payload = original_control_plane_summary(repo_latest, eeepc_latest, current_experiment, current_blocker, cfg)
        payload['guarded_evolution'] = {
            'current_candidate': {'candidate_id': 'candidate-1'},
            'latest_request': {'request_id': 'request-1', 'objective': 'repair loop'},
            'last_apply': {'release_dir': '/tmp/release-1'},
            'last_rollback': {'rolled_back_to_release_dir': '/tmp/release-0'},
            'last_failure_learning': {'learning_summary': 'repair first'},
            'last_export': {'ok': True, 'exit_code': 0},
        }
        payload['local_ci'] = {
            'latest_result': {'ok': True, 'exit_code': 0, 'summary': 'PASS exit=0 | 13 passed in 1.25s'}
        }
        payload['service_guards'] = {
            'collector': {
                'ActiveState': 'active',
                'SubState': 'running',
                'MemoryCurrent': '54317056',
                'MemoryMax': '536870912',
                'RuntimeMaxUSec': '12h',
            }
        }
        return payload

    monkeypatch.setattr(dashboard_app, '_control_plane_summary', _patched_control_plane_summary)

    app = create_app(_cfg(tmp_path, db))
    status, body = _call_app(app, '/system')
    assert status.startswith('200')
    assert 'Local CI' in body
    assert 'PASS exit=0 | 13 passed in 1.25s' in body
    assert 'Last export' in body
    assert 'ok / exit=0' in body
    assert 'Collector runtime guard' in body
    assert 'MemoryMax' in body
    assert '536870912' in body
    assert 'RuntimeMaxUSec' in body
