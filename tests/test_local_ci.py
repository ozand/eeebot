from __future__ import annotations

import json
import sys
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
    # The runner refuses to start pytest over absent targets, so the fixture has
    # to produce the files production would be pointed at.
    (workspace / 'tests').mkdir()
    for target in DEFAULT_TEST_TARGETS:
        (workspace / target).write_text('', encoding='utf-8')

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
    (workspace / 'tests').mkdir()
    (workspace / 'tests' / 'dummy.py').write_text('', encoding='utf-8')

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


def test_absent_targets_are_not_reported_as_a_red_suite(tmp_path):
    """#1593: the unit pointed at a checkout holding none of its target files.

    pytest exited 4 with "no tests ran", which the result file rendered the same
    way as an ordinary failing run. A guard that never executed must not be
    readable as a guard that executed and found problems.
    """
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_present.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")

    result = run_and_record_local_ci(
        workspace=workspace,
        state_dir=tmp_path / "state",
        test_targets=["tests/test_present.py", "tests/test_absent.py"],
    )

    assert result["state"] == "targets_missing"
    assert result["ok"] is False
    assert result["exit_code"] is None
    assert result["command"] == [], "no pytest process should have been started"
    assert "tests/test_absent.py" in result["summary"]
    assert "tests/test_present.py" not in result["summary"]

    written = json.loads((tmp_path / "state" / "local_ci" / "latest.json").read_text(encoding="utf-8"))
    assert written["state"] == "targets_missing", "the distinction must survive to disk"


def test_a_suite_that_actually_ran_is_labelled_ran(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_present.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")

    result = run_and_record_local_ci(
        workspace=workspace,
        state_dir=tmp_path / "state",
        pytest_bin=sys.executable,
        test_targets=["tests/test_present.py"],
    )

    assert result["state"] == "ran"
    assert result["exit_code"] == 0
