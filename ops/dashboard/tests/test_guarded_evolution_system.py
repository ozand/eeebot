from pathlib import Path
import json

import nanobot_ops_dashboard.app as dashboard_app
from nanobot_ops_dashboard.app import create_app
from test_app import _call_app, _cfg, _seed_dashboard_data, _seed_experiment_telemetry, _seed_hypothesis_backlog
from nanobot_ops_dashboard.storage import init_db


def test_system_page_shows_guarded_evolution_status(tmp_path: Path, monkeypatch):
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    _seed_experiment_telemetry(tmp_path)
    _seed_hypothesis_backlog(tmp_path)

    def _patched_structured(path):
        path = str(path)
        if path.endswith('workspace/state/control_plane/current_summary.json'):
            return {'approval_gate': {'state': 'fresh'}}
        return original_structured(path)

    original_control_plane_summary = dashboard_app._control_plane_summary

    def _patched_control_plane_summary(repo_latest, eeepc_latest, current_experiment, current_blocker, cfg):
        payload = original_control_plane_summary(repo_latest, eeepc_latest, current_experiment, current_blocker, cfg)
        payload['guarded_evolution'] = {
            'schema_version': 'autoevolve-state-v1',
            'current_candidate': {'candidate_id': 'candidate-1'},
            'latest_request': {'request_id': 'request-1', 'objective': 'repair loop'},
            'last_apply': {'release_dir': '/tmp/release-1'},
            'last_rollback': {'rolled_back_to_release_dir': '/tmp/release-0'},
            'last_failure_learning': {'candidate_id': 'candidate-bad', 'learning_summary': 'repair first'},
        }
        return payload

    monkeypatch.setattr(dashboard_app, '_control_plane_summary', _patched_control_plane_summary)

    cfg = _cfg(tmp_path, db)
    app = create_app(cfg)
    status, body = _call_app(app, '/system')
    assert status.startswith('200')
    assert 'Guarded evolution' in body
    assert 'candidate-1' in body
    assert 'request-1' in body
    assert '/tmp/release-1' in body
    assert '/tmp/release-0' in body
    assert 'repair first' in body
