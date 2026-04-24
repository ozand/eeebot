from pathlib import Path
import json

import nanobot_ops_dashboard.app as dashboard_app
from nanobot_ops_dashboard.app import create_app
from nanobot_ops_dashboard.storage import init_db
from test_app import _call_app, _cfg, _seed_dashboard_data, _seed_experiment_telemetry, _seed_hypothesis_backlog


def test_system_page_shows_pr_and_merge_status(tmp_path: Path, monkeypatch):
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
            'selfevo_issue': {'number': 4, 'url': 'https://github.com/ozand/eeebot-self-evolving/issues/4'},
            'selfevo_branch': 'fix/issue-4-analyze-last-failed-candidate',
            'last_pr': {'number': 2, 'url': 'https://github.com/ozand/eeebot-self-evolving/pull/2'},
            'last_merge': {'pr_number': 2, 'merged': True},
        }
        return payload

    monkeypatch.setattr(dashboard_app, '_control_plane_summary', _patched_control_plane_summary)
    app = create_app(_cfg(tmp_path, db))
    status, body = _call_app(app, '/system')
    assert status.startswith('200')
    assert 'Self-evolving issue' in body
    assert 'fix/issue-4-analyze-last-failed-candidate' in body
    assert 'Last PR' in body
    assert 'Last merge' in body
