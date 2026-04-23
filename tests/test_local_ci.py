from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.local_ci import write_local_ci_result, write_local_ci_state_summary


def test_write_local_ci_result_and_summary(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    result = write_local_ci_result(
        workspace=workspace,
        command=['python3', '-m', 'pytest', 'tests/test_autoevolve.py', '-q'],
        exit_code=0,
        output='4 passed in 0.60s',
        summary='PASS 4 passed in 0.60s',
    )
    latest = json.loads((workspace / 'state' / 'local_ci' / 'latest.json').read_text())
    assert latest['ok'] is True
    assert latest['exit_code'] == 0
    assert latest['summary'] == 'PASS 4 passed in 0.60s'
    state = write_local_ci_state_summary(workspace=workspace)
    assert state['latest_result']['summary'] == 'PASS 4 passed in 0.60s'
    current = json.loads((workspace / 'state' / 'local_ci' / 'current_state.json').read_text())
    assert current['latest_result']['ok'] is True
