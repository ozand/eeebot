from __future__ import annotations

import json
from pathlib import Path

import nanobot_ops_dashboard.app as dashboard_app
from nanobot_ops_dashboard.app import create_app
from nanobot_ops_dashboard.storage import init_db
from test_app import _call_app, _cfg, _seed_dashboard_data, _seed_experiment_telemetry, _seed_hypothesis_backlog


def test_overview_uses_canonical_gate_truth_and_derived_decision_trail_and_subagent_cycle(tmp_path: Path, monkeypatch):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    _seed_experiment_telemetry(tmp_path)
    _seed_hypothesis_backlog(tmp_path)

    producer_summary = {
        'approval_gate': {
            'state': 'fresh',
            'ttl_minutes': 15,
            'expires_at_utc': '2026-04-23T16:19:50Z',
            'source': '/workspace/state/approvals/apply.ok'
        },
        'cycle_id': 'cycle-1',
        'task_boundary': {
            'task_id': 'subagent-verify-materialized-improvement',
            'title': 'Use one bounded subagent-assisted review to verify the materialized improvement artifact'
        },
        'experiment': {
            'current_task_id': 'subagent-verify-materialized-improvement',
            'review_status': 'pending_policy_review',
            'decision': 'pending_policy_review'
        }
    }

    original_control_plane_summary = dashboard_app._control_plane_summary

    def _patched_control_plane_summary(repo_latest, eeepc_latest, current_experiment, current_blocker, cfg):
        payload = original_control_plane_summary(repo_latest, eeepc_latest, current_experiment, current_blocker, cfg)
        payload['approval'] = producer_summary['approval_gate']
        payload['producer_summary'] = producer_summary
        payload['experiment'] = {
            **(payload.get('experiment') or {}),
            **producer_summary['experiment'],
        }
        return payload

    monkeypatch.setattr(dashboard_app, '_control_plane_summary', _patched_control_plane_summary)

    cfg = _cfg(tmp_path, db)
    app = create_app(cfg)
    status, body = _call_app(app, '/')
    assert status.startswith('200')
    assert 'fresh' in body
    assert 'promotion-42 | reviewed | accept' in body or 'accept' in body
    assert 'Cycle</span><span class="mono">cycle-1</span>' in body
