import json
from pathlib import Path

from nanobot.runtime.autoevolve import write_candidate_blocked_status


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_stale_candidate_blocked_status_is_durable_and_marks_latest_candidate_stale(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    candidate = {
        'schema_version': 'autoevolve-candidate-v1',
        'candidate_id': 'candidate-stale',
        'commit': 'abc123',
        'remote_name': 'origin',
        'branch': 'main',
        'remote_head': 'def456',
        'remote_commit_visible': False,
        'clean_worktree': True,
    }

    blocked = write_candidate_blocked_status(workspace, candidate, 'remote_commit_not_visible')

    assert blocked['status'] == 'blocked'
    assert blocked['reason'] == 'remote_commit_not_visible'
    assert blocked['stale_candidate'] is True
    assert 'regenerate candidate' in blocked['recommended_next_action']
    latest_blocked = _read_json(workspace / 'state' / 'self_evolution' / 'runtime' / 'latest_blocked.json')
    latest_candidate = _read_json(workspace / 'state' / 'self_evolution' / 'candidates' / 'latest.json')
    current_state = _read_json(workspace / 'state' / 'self_evolution' / 'current_state.json')
    assert latest_blocked['candidate_id'] == 'candidate-stale'
    assert latest_candidate['status'] == 'stale'
    assert latest_candidate['stale_reason'] == 'remote_commit_not_visible'
    assert current_state['current_candidate']['status'] == 'stale'
