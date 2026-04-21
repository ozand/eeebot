import json
from pathlib import Path

from nanobot.runtime.state import load_runtime_state


def test_runtime_state_exposes_capabilities_snapshot(tmp_path: Path):
    state = tmp_path / 'state'
    (state / 'goals').mkdir(parents=True)
    (state / 'goals' / 'active.json').write_text(json.dumps({'active_goal': 'goal-bootstrap'}), encoding='utf-8')
    (state / 'reports').mkdir(parents=True)
    (state / 'reports' / 'evolution-20260422T000000Z.json').write_text(json.dumps({'result_status': 'BLOCK'}), encoding='utf-8')
    (state / 'outbox').mkdir(parents=True)
    (state / 'outbox' / 'latest.json').write_text(json.dumps({'approval_gate': {'state': 'missing'}, 'next_hint': 'approval gate missing; refresh manually'}), encoding='utf-8')

    runtime = load_runtime_state(tmp_path)
    caps = runtime['capabilities']
    assert caps['bounded_apply']['state'] == 'blocked'
    assert caps['bounded_apply']['reason'] == 'approval_gate_missing'
    assert caps['runtime_state']['state'] == 'available'
    assert caps['runtime_state']['reason'] == 'loaded'
