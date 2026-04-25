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


def test_system_api_exposes_bounded_selfevo_current_proof_summary(tmp_path: Path):
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    _seed_experiment_telemetry(tmp_path)
    _seed_hypothesis_backlog(tmp_path)

    state_root = tmp_path / 'nanobot' / 'workspace' / 'state' / 'self_evolution'
    runtime_root = state_root / 'runtime'
    runtime_root.mkdir(parents=True, exist_ok=True)

    current_state = {
        'schema_version': 'autoevolve-state-v1',
        'current_candidate': {'candidate_id': 'candidate-26'},
        'latest_request': {'request_id': 'request-26', 'objective': 'record cycle reward'},
        'selfevo_issue': {'number': 24, 'title': 'Record cycle reward', 'url': 'https://github.com/ozand/eeebot-self-evolving/issues/24'},
        'selfevo_branch': 'fix/issue-24-record-cycle-reward',
        'last_pr': {'number': 26, 'url': 'https://github.com/ozand/eeebot-self-evolving/pull/26', 'title': 'Record cycle reward'},
        'last_merge': {'pr_number': 26, 'merged': True, 'dry_run': False},
        'last_noop': None,
        'last_issue_lifecycle': {
            'schema_version': 'autoevolve-issue-lifecycle-v1',
            'status': 'terminal_merged',
            'selfevo_issue': {'number': 24, 'title': 'Record cycle reward', 'url': 'https://github.com/ozand/eeebot-self-evolving/issues/24'},
            'issue_number': 24,
            'issue_title': 'Record cycle reward',
            'selfevo_branch': 'fix/issue-24-record-cycle-reward',
            'pr': {'number': 26, 'url': 'https://github.com/ozand/eeebot-self-evolving/pull/26', 'title': 'Record cycle reward'},
            'pr_number': 26,
            'linked_issue_action': 'merged',
            'github_issue_state': 'CLOSED',
            'retry_allowed': False,
        },
    }
    latest_issue_lifecycle = current_state['last_issue_lifecycle']
    (state_root / 'current_state.json').write_text(json.dumps(current_state, indent=2), encoding='utf-8')
    (runtime_root / 'latest_issue_lifecycle.json').write_text(json.dumps(latest_issue_lifecycle, indent=2), encoding='utf-8')

    repo_latest = {
        'raw_json': json.dumps(
            {
                'selfevo_remote_freshness': {
                    'remote_name': 'selfevo',
                    'default_branch': 'main',
                    'remote_ref': 'selfevo/main',
                    'remote_head': 'abc123',
                    'default_branch_head': 'def456',
                    'ahead_count': 0,
                    'behind_count': 1,
                    'remote_ref_stale': True,
                    'state': 'stale',
                    'source': 'local_git_refs',
                    'refresh_status': 'collected',
                }
            }
        )
    }

    control_plane = dashboard_app._control_plane_summary(repo_latest, {}, {}, {}, _cfg(tmp_path, db))
    proof = control_plane['selfevo_current_proof']

    assert proof['schema_version'] == 'selfevo-current-proof-v1'
    assert proof['state'] == 'available'
    assert proof['mode'] == 'bounded_local_reader'
    assert proof['source'] == 'local_runtime_artifacts'
    assert proof['live_github_api'] == 'out_of_scope'
    assert proof['evidence_kind'] == 'latest_issue_lifecycle'
    assert proof['summary'].startswith('latest issue lifecycle terminal_merged')
    assert 'issue #24' in proof['summary']
    assert 'PR #26' in proof['summary']
    assert 'fix/issue-24-record-cycle-reward' in proof['summary']
    assert proof['latest_issue_lifecycle']['status'] == 'terminal_merged'
    assert proof['latest_issue_lifecycle']['issue_number'] == 24
    assert proof['latest_issue_lifecycle']['pr_number'] == 26
    assert proof['latest_merge']['pr_number'] == 26
    assert proof['remote_freshness']['state'] == 'stale'
    assert any(path.endswith('workspace/state/self_evolution/current_state.json') for path in proof['evidence_paths'])

    app = create_app(_cfg(tmp_path, db))
    status, api_body = _call_app(app, '/api/system')
    assert status.startswith('200')
    assert 'selfevo_current_proof' in api_body
    assert 'selfevo-current-proof-v1' in api_body
    assert 'latest issue lifecycle terminal_merged' in api_body
    assert 'out_of_scope' in api_body
    status, body = _call_app(app, '/system')
    assert status.startswith('200')
    assert 'Current proof' in body
    assert 'latest issue lifecycle terminal_merged' in body
