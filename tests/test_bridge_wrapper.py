"""Regression test for #599: bridge moved into nanobot/runtime/bridge.py.

scripts/eeepc_self_evolving_subagent_bridge.py must remain a thin wrapper
(the systemd unit and deploy_release.sh still reference that path unchanged —
see docs/changes/599-bridge-into-package/proposal.md) that delegates to
nanobot.runtime.bridge.cli_main. This test exercises the wrapper as a real
subprocess (not just an AST/text check) so a future edit that reintroduces
top-level logic into the wrapper, or that breaks the import path, fails CI.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
WRAPPER_PATH = REPO_ROOT / "scripts" / "eeepc_self_evolving_subagent_bridge.py"
BRIDGE_MODULE_PATH = REPO_ROOT / "nanobot" / "runtime" / "bridge.py"


def test_wrapper_script_is_thin():
    """The wrapper should just delegate — no business logic left behind."""
    source = WRAPPER_PATH.read_text(encoding="utf-8")
    assert "from nanobot.runtime.bridge import cli_main" in source
    assert "cli_main()" in source
    # Thin: well under the ~30 line budget for a delegate-only wrapper.
    assert len(source.splitlines()) < 30


def test_bridge_module_exposes_cli_main():
    source = BRIDGE_MODULE_PATH.read_text(encoding="utf-8")
    assert "def cli_main() -> int:" in source
    assert "async def main(" in source


def test_wrapper_runs_disabled_without_error():
    """Real subprocess invocation: SUBAGENT_BRIDGE_ENABLED=0 short-circuits
    to `bridge_disabled` without touching STATE_DIR — cheapest possible
    end-to-end smoke of the wrapper -> nanobot.runtime.bridge.cli_main path.
    """
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["SUBAGENT_BRIDGE_ENABLED"] = "0"
    result = subprocess.run(
        [sys.executable, str(WRAPPER_PATH)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "bridge_disabled" in result.stdout
