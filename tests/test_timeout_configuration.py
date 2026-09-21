"""Regression tests for the repository-wide pytest timeout (#1836)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_repository_timeout_is_configured(pytestconfig) -> None:
    assert pytestconfig.getini("timeout") == "300"
    assert pytestconfig.getini("timeout_method") == "thread"


def test_git_subprocesses_cannot_prompt_for_terminal_input() -> None:
    assert os.environ["GIT_TERMINAL_PROMPT"] == "0"


def test_blocking_fixture_fails_by_timeout(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\ntimeout = 0.2\ntimeout_method = thread\n",
        encoding="utf-8",
    )
    (tmp_path / "test_blocking.py").write_text(
        "import time\n"
        "def test_intentionally_blocks():\n"
        "    time.sleep(2)\n",
        encoding="utf-8",
    )
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert result.stderr == ""
    assert "Timeout" in output
