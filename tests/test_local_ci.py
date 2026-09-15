from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from nanobot.runtime.local_ci import (
    DEFAULT_TEST_TARGETS,
    run_and_record_local_ci,
    write_local_ci_result,
    write_local_ci_state_summary,
)


def test_write_local_ci_result_and_summary(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    write_local_ci_result(
        workspace=workspace,
        command=['python3', '-m', 'pytest', 'tests/test_local_ci.py', '-q'],
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


def test_run_and_record_local_ci_executes_default_targets_and_persists_state(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    state_dir = tmp_path / 'state'
    workspace.mkdir()
    state_dir.mkdir()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = '63 passed in 36.00s\n'
    mock_proc.stderr = ''

    with patch('subprocess.run', return_value=mock_proc) as mock_run:
        result = run_and_record_local_ci(
            workspace=workspace,
            state_dir=state_dir,
            pytest_bin='/opt/eeepc-agent/venv/bin/python',
        )
        assert result['ok'] is True
        assert result['exit_code'] == 0
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == '/opt/eeepc-agent/venv/bin/python'
        assert cmd[1:3] == ['-m', 'pytest']
        for target in DEFAULT_TEST_TARGETS:
            assert target in cmd

    latest_path = state_dir / 'local_ci' / 'latest.json'
    current_path = state_dir / 'local_ci' / 'current_state.json'
    assert latest_path.exists()
    assert current_path.exists()

    latest = json.loads(latest_path.read_text())
    assert latest['ok'] is True
    assert latest['exit_code'] == 0
    assert '63 passed in 36.00s' in latest['summary']
    assert latest['targets'] == list(DEFAULT_TEST_TARGETS)


def test_run_and_record_local_ci_handles_failure(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    state_dir = tmp_path / 'state'
    workspace.mkdir()
    state_dir.mkdir()

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = '1 failed, 62 passed in 35.00s\n'
    mock_proc.stderr = ''

    with patch('subprocess.run', return_value=mock_proc):
        result = run_and_record_local_ci(
            workspace=workspace,
            state_dir=state_dir,
            test_targets=['tests/dummy.py'],
        )
        assert result['ok'] is False
        assert result['exit_code'] == 1

    latest = json.loads((state_dir / 'local_ci' / 'latest.json').read_text())
    assert latest['ok'] is False
    assert latest['exit_code'] == 1
    assert '1 failed' in latest['summary']
