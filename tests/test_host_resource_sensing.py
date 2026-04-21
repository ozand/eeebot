import json
from pathlib import Path

from nanobot.runtime.state import load_runtime_state


def test_runtime_state_exposes_host_resource_sensing(tmp_path: Path):
    state = tmp_path / 'state'
    (state / 'goals').mkdir(parents=True)
    (state / 'goals' / 'active.json').write_text(json.dumps({'active_goal': 'goal-bootstrap'}), encoding='utf-8')
    (state / 'reports').mkdir(parents=True)
    (state / 'reports' / 'evolution-20260422T000000Z.json').write_text(json.dumps({'result_status': 'BLOCK'}), encoding='utf-8')

    runtime = load_runtime_state(tmp_path)
    assert 'host_resources' in runtime
    host = runtime['host_resources']
    assert 'loadavg' in host
    assert 'disk_free_bytes' in host
    assert 'memory_available_bytes' in host
    assert 'weak_host_signals' in host
