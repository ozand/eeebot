"""Regression test for #620: bridge stdout/stderr must be line-buffered.

Under systemd, the bridge's stdout/stderr are a pipe to the journal, which
Python fully-buffers by default — delaying flush of `print()` output by
minutes and making journal timestamps untrustworthy (see incident note in
docs/specs/subagent-bridge/spec.md). `_ensure_line_buffered_streams` in
nanobot/runtime/bridge.py forces line buffering via `reconfigure`.

Run in a subprocess rather than in-process so the test never mutates the
test runner's own stdout/stderr streams.
"""
from __future__ import annotations

import subprocess
import sys


def test_ensure_line_buffered_streams_sets_line_buffering():
    code = (
        "import sys\n"
        "from nanobot.runtime.bridge import _ensure_line_buffered_streams\n"
        "_ensure_line_buffered_streams()\n"
        "assert sys.stdout.line_buffering is True\n"
        "assert sys.stderr.line_buffering is True\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
