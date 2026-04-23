from pathlib import Path

from nanobot_ops_dashboard.app import create_app
from nanobot_ops_dashboard.storage import init_db, upsert_event
from test_app import _call_app, _cfg


def test_promotions_page_distinguishes_ready_reviewed_and_accepted(tmp_path: Path):
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    upsert_event(db, {
        'collected_at': '2026-04-23T16:00:00Z',
        'source': 'repo',
        'event_type': 'promotion',
        'identity_key': 'promotion-ready',
        'title': 'promotion-ready',
        'status': 'ready_for_policy_review',
        'detail_json': '{"candidate_path": "/workspace/state/promotions/promotion-ready.json", "decision_record": "pending_operator_review_packet", "accepted_record": null, "readiness_checks": ["artifact_present"], "readiness_reasons": ["artifact complete"]}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-23T16:01:00Z',
        'source': 'repo',
        'event_type': 'promotion',
        'identity_key': 'promotion-reviewed',
        'title': 'promotion-reviewed',
        'status': 'reviewed',
        'detail_json': '{"candidate_path": "/workspace/state/promotions/promotion-reviewed.json", "decision_record": "present", "accepted_record": null}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-23T16:02:00Z',
        'source': 'repo',
        'event_type': 'promotion',
        'identity_key': 'promotion-accepted',
        'title': 'promotion-accepted',
        'status': 'accept',
        'detail_json': '{"candidate_path": "/workspace/state/promotions/promotion-accepted.json", "decision_record": "present", "accepted_record": "present"}',
    })

    app = create_app(_cfg(tmp_path, db))
    status, body = _call_app(app, '/promotions')
    assert status.startswith('200')
    assert 'ready_for_policy_review' in body
    assert 'promotion-ready' in body
    assert 'pending_operator_review_packet' in body
    assert 'promotion-reviewed' in body
    assert 'reviewed' in body
    assert 'promotion-accepted' in body
    assert 'accept' in body
    assert 'artifact_present' in body
    assert 'artifact complete' in body
