"""#1303: a deploy must tell "this release cannot run" from "this one cycle
could not reach the gateway".

On 2026-09-05 06:38 the first post-flip bridge cycle of `a64470e5` drew a
transient gateway 404, exited EXIT_EXECUTOR_LLM_ERROR (3) as #1280 requires,
systemd reported the oneshot start job failed, and deploy_release.sh rolled the
release back — un-deploying the fix for the lie it had just recorded honestly.

Arm 1: the exit status is classified at both rollback points. Status 3 keeps
the release; every other failure still rolls back. Both halves are DRIVEN here,
not asserted: the remote activation stanza is executed with a scripted
`systemctl`, and the local health gate is run through the full script with
journal/streak fixtures, exactly as tests/test_deploy_release.py does.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from nanobot.runtime import bridge
from tests import test_deploy_release as _harness
from tests.test_deploy_release import (
    DEPLOY_SCRIPT,
    _journal_replay_mock,
    _streak_mock,
    _write_mock,
    run_deploy,
    set_ssh_mock,
)

# The harness fixtures, re-exported under their own names so pytest finds them
# here (an assignment, not an import, so the test parameters do not read as a
# redefinition to the linter).
repo = _harness.repo
mock_bin = _harness.mock_bin

LIB = DEPLOY_SCRIPT.with_name("lib_bridge_exit.sh")
BRIDGE_UNIT_LINE = "Sep 05 03:40:10 eeepc systemd[1]: eeepc-self-evolving-subagent-bridge.service: Main process exited, code=exited, status={status}/{name}"


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full_env)


def test_lib_constant_is_pinned_to_the_bridge_exit_code():
    text = LIB.read_text(encoding="utf-8")
    value = int(re.search(r"^BRIDGE_EXIT_EXECUTOR_LLM_ERROR=(\d+)$", text, re.M).group(1))
    assert value == bridge.EXIT_EXECUTOR_LLM_ERROR == 3
    overflow = int(re.search(r"^BRIDGE_EXIT_SYSTEM_PROMPT_OVERFLOW=(\d+)$", text, re.M).group(1))
    assert overflow == bridge.EXIT_SYSTEM_PROMPT_OVERFLOW == 4


def test_describe_bridge_exit_status_names_known_codes_and_the_remedy():
    lib = str(LIB).replace("\\", "/")
    for status, needle in (("3", "EXIT_EXECUTOR_LLM_ERROR"), ("4", "EXIT_SYSTEM_PROMPT_OVERFLOW"), ("4", "prompt-fit: droppable"), ("1", ""), ("", "")):
        res = _bash(f'. "{lib}"; describe_bridge_exit_status "{status}"')
        assert res.returncode == 0, res.stderr
        assert needle in res.stdout.strip() if needle else res.stdout.strip() == ""


def test_activation_overflow_rollback_names_itself_and_the_remedy(tmp_path):
    """#1300 deployed before the instance markers: the rollback still happens (exit 4 is a
    release-plus-data failure, not a transient) and the deploy output says why and what to do."""
    res, rolled_back = _drive_activation(tmp_path, restart_rc=4, result="exit-code", status="4")
    assert res.returncode != 0 and rolled_back is True
    assert "CRITICAL: bridge activation failed" in res.stderr
    assert "EXIT_SYSTEM_PROMPT_OVERFLOW" in res.stderr and "prompt-fit: droppable" in res.stderr and "#1300" in res.stderr


@pytest.mark.parametrize("rc, result, status, expected", [
    ("0", "success", "0", "ok"),
    ("3", "exit-code", "3", "transport"),          # the 2026-09-05 case
    ("1", "exit-code", "1", "failed"),             # internal error
    ("4", "exit-code", "4", "failed"),             # #1302 system-prompt overflow: a release/config failure, stays fatal
    ("203", "exit-code", "203", "failed"),         # missing interpreter / venv
    ("1", "signal", "9", "failed"),                # killed
    ("1", "exit-code", "", "failed"),              # status unreadable: never assume transport
    ("1", "", "3", "failed"),                      # status 3 but Result not exit-code: not the #1280 path
])
def test_classify_bridge_run(rc, result, status, expected):
    lib = str(LIB).replace("\\", "/")
    res = _bash(f'. "{lib}"; classify_bridge_run "{rc}" "{result}" "{status}"')
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == expected


def _activation_stanza() -> str:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    begin, end = "# --- #1303 bridge activation begin ---", "# --- #1303 bridge activation end ---"
    assert begin in text and end in text, "the activation stanza must keep its markers so this test drives the real text"
    return text.split(begin, 1)[1].split(end, 1)[0]


def _drive_activation(tmp_path: Path, *, restart_rc: int, result: str, status: str) -> tuple[subprocess.CompletedProcess, bool]:
    """Run the script's own activation stanza under `set -eEuo pipefail` with the
    real ERR trap wiring (die → return 1 → trap → rollback_remote) and a scripted
    systemctl. Returns the process and whether rollback_remote fired."""
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    _write_mock(shims / "sudo", 'exec "$@"')
    _write_mock(shims / "systemctl", f'''
    if [ "$1" = "restart" ]; then exit {restart_rc}; fi
    if [ "$1" = "show" ] && [[ "$*" == *"-p Result"* ]]; then echo "{result}"; exit 0; fi
    if [ "$1" = "show" ] && [[ "$*" == *"-p ExecMainStatus"* ]]; then echo "{status}"; exit 0; fi
    exit 0
    ''')
    marker = tmp_path / "rolled_back"
    release_dir = str(DEPLOY_SCRIPT.parents[3]).replace("\\", "/")
    prelude = f'''
    set -eEuo pipefail
    die() {{ echo "CRITICAL: $*" >&2; return 1; }}
    rollback_remote() {{ if [ "${{BASH_SUBSHELL:-0}}" -gt 0 ]; then return; fi; touch "{str(marker).replace(chr(92), "/")}"; echo "[remote] rollback after activation failure" >&2; }}
    trap rollback_remote ERR
    VERIFY_ONLY=0
    RELEASE_DIR="{release_dir}"
    '''
    env = {"PATH": str(shims).replace("\\", "/") + ":" + os.environ["PATH"]}
    res = _bash(prelude + _activation_stanza() + "\necho STANZA-COMPLETED\n", env=env)
    return res, marker.exists()


def test_activation_transport_exit_keeps_the_release(tmp_path):
    res, rolled_back = _drive_activation(tmp_path, restart_rc=3, result="exit-code", status="3")
    assert res.returncode == 0, res.stderr
    assert rolled_back is False
    assert "STANZA-COMPLETED" in res.stdout
    assert "not an activation failure (#1303)" in res.stdout and "EXIT_EXECUTOR_LLM_ERROR (3)" in res.stdout


@pytest.mark.parametrize("restart_rc, result, status, why", [
    (1, "exit-code", "1", "import error / internal error"),
    (203, "exit-code", "203", "missing venv or interpreter"),
    (1, "exit-code", "4", "#1302 system-prompt overflow"),
    (1, "signal", "9", "killed"),
    (1, "exit-code", "", "unit that never produced a status"),
])
def test_activation_genuine_failure_still_rolls_back(tmp_path, restart_rc, result, status, why):
    res, rolled_back = _drive_activation(tmp_path, restart_rc=restart_rc, result=result, status=status)
    assert res.returncode != 0, why
    assert rolled_back is True, why
    assert "STANZA-COMPLETED" not in res.stdout, why
    assert "CRITICAL: bridge activation failed" in res.stderr, why


def test_activation_clean_exit_continues(tmp_path):
    res, rolled_back = _drive_activation(tmp_path, restart_rc=0, result="success", status="0")
    assert res.returncode == 0 and rolled_back is False and "STANZA-COMPLETED" in res.stdout


# ── the local health gate: journal and exit-streak reads ────────────────────

def test_gate_transport_exit_in_journal_does_not_roll_back(repo, mock_bin):
    """The exact line systemd logged on 2026-09-05 06:38."""
    set_ssh_mock(mock_bin, _journal_replay_mock(BRIDGE_UNIT_LINE.format(status=3, name="NOTIMPLEMENTED")))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode == 0, combined
    assert "rolling back to" not in combined and "bridge process crashed" not in combined
    assert "transport failure recorded as blocked" in combined and "not a release failure (#1303)" in combined
    assert "health gate: unknown" in combined, "no clean run yet — the gate says so instead of certifying"


@pytest.mark.parametrize("status, name", [(1, "FAILURE"), (4, "NOTIMPLEMENTED"), (203, "EXEC")])
def test_gate_other_nonzero_exits_still_roll_back(repo, mock_bin, status, name):
    set_ssh_mock(mock_bin, _journal_replay_mock(BRIDGE_UNIT_LINE.format(status=status, name=name)))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode != 0
    assert "rolling back" in combined and "bridge process crashed" in combined, combined


def test_gate_transport_streak_failure_waits_instead_of_rolling_back(repo, mock_bin):
    streak = ('{"schema_version": "bridge-exit-streak-v1", "consecutive_failures": 1, '
              '"last_failure_ts": "2099-01-01T00:00:00Z", "last_exit_status": 3, "last_outcome": "failure"}')
    set_ssh_mock(mock_bin, _streak_mock(streak))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode == 0, combined
    assert "rolling back to" not in combined and "exit recorder shows a failure" not in combined
    assert "transport failure after the flip" in combined and "streak advanced as #1280 intends" in combined
    assert "health gate: unknown" in combined


def test_gate_other_streak_failure_still_rolls_back(repo, mock_bin):
    streak = ('{"schema_version": "bridge-exit-streak-v1", "consecutive_failures": 1, '
              '"last_failure_ts": "2099-01-01T00:00:00Z", "last_exit_status": 4, "last_error": "SystemPromptOverflowError"}')
    set_ssh_mock(mock_bin, _streak_mock(streak))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode != 0
    assert "rolling back" in combined and "exit recorder shows a failure" in combined, combined


def test_transport_then_clean_run_is_clean_exit(repo, mock_bin):
    """The streak after a transient: consecutive_failures back to 0 with a post-flip success."""
    streak = ('{"schema_version": "bridge-exit-streak-v1", "consecutive_failures": 0, '
              '"last_failure_ts": "2099-01-01T00:00:00Z", "last_exit_status": 0, "last_success_ts": "2099-01-01T00:15:00Z"}')
    set_ssh_mock(mock_bin, _streak_mock(streak, BRIDGE_UNIT_LINE.format(status=3, name="NOTIMPLEMENTED")))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode == 0, combined
    assert "health gate: clean-exit" in combined and "rolling back to" not in combined
