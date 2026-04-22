from pathlib import Path

from nanobot.runtime.action_registry import build_action_registry_snapshot


def test_action_registry_snapshot_exposes_truth_check(tmp_path: Path):
    (tmp_path / 'state' / 'reports').mkdir(parents=True)
    snapshot = build_action_registry_snapshot(tmp_path)
    assert snapshot['version'] == 'action-registry-v1'
    assert 'capability.truth-check' in snapshot['actions']
    assert snapshot['actions']['capability.truth-check']['kind'] == 'read_only'
