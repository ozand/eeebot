"""#1197: every bridge invocation exit leaves a durable, countable record.

2026-09-01: the bridge crash-looped for 9 h 20 min (140 consecutive failed
invocations, ``NameError`` at import) and systemd, the ledger and the deploy
gate all reported healthy. Every test here fails against the pre-#1197 tree
for the reason in its docstring.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import scorecard

REPO = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)


def _streak(state: Path) -> dict:
    return json.loads((state / "bridge" / "exit_streak.json").read_text(encoding="utf-8"))


def _rows(state: Path) -> list[dict]:
    return [json.loads(line) for line in (state / "bridge" / "exits.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(code: str, state: Path, *, marker: str = "1", extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """A fresh interpreter that imports ``nanobot`` the way ``python -m`` would:
    the recorder is armed (or not) purely by the package import."""
    env = {**os.environ, "PYTHONPATH": str(REPO), "STATE_DIR": str(state), "NANOBOT_BRIDGE_EXIT_RECORD": marker, "PYTHONIOENCODING": "utf-8"}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)


# ─── the record and the streak ───────────────────────────────────────────────

def test_consecutive_failures_count_up_and_a_success_resets(tmp_path):
    """Pre-fix: no ``nanobot.crash_record`` module and nothing under state/bridge/."""
    from nanobot import crash_record  # module absent on the pre-#1197 tree

    state = tmp_path / "state"
    for i in range(3):
        streak = crash_record.record_exit(state, outcome="failure", exit_status=1,
                                          error="NameError: name '_parse_explore_mode' is not defined",
                                          where="nanobot/runtime/bridge.py:4987", now=NOW + timedelta(minutes=3 * i))
    assert streak["consecutive_failures"] == 3 and streak["total_failures"] == 3
    assert streak["first_failure_ts"] == crash_record._now_iso(NOW)
    assert streak["last_failure_ts"] == crash_record._now_iso(NOW + timedelta(minutes=6))
    assert streak["last_error"].startswith("NameError: name '_parse_explore_mode'") and streak["last_where"].endswith("bridge.py:4987")
    assert streak["last_exit_status"] == 1 and streak["schema_version"] == "bridge-exit-streak-v1"

    streak = crash_record.record_exit(state, outcome="success", exit_status=0, now=NOW + timedelta(minutes=9))
    assert streak["consecutive_failures"] == 0 and streak["total_failures"] == 3
    assert streak["last_success_ts"] == crash_record._now_iso(NOW + timedelta(minutes=9)) and "first_failure_ts" not in streak
    rows = _rows(state)
    assert [r["outcome"] for r in rows] == ["failure", "failure", "failure", "success"]
    assert rows[0]["source"] == "process" and rows[0]["error"].startswith("NameError")


def test_systemd_record_for_the_same_invocation_is_merged_not_double_counted(tmp_path):
    from nanobot import crash_record  # module absent on the pre-#1197 tree

    state = tmp_path / "state"
    crash_record.record_exit(state, outcome="failure", exit_status=1, error="NameError: x", now=NOW)
    streak = crash_record.record_exit(state, outcome="failure", exit_status=1, source="systemd",
                                      service_result="exit-code", exit_code="exited", now=NOW + timedelta(seconds=20))
    assert streak["consecutive_failures"] == 1 and streak["last_service_result"] == "exit-code"
    assert _rows(state)[-1]["merged_with_previous"] is True
    # a later systemd-only record (signal, OOM: no process record precedes it) counts on its own
    streak = crash_record.record_exit(state, outcome="failure", exit_status="KILL", source="systemd",
                                      service_result="signal", exit_code="killed", now=NOW + timedelta(minutes=10))
    assert streak["consecutive_failures"] == 2 and streak["last_exit_status"] == "KILL"


def test_write_failure_is_printed_and_raised_never_swallowed(tmp_path, capsys):
    """Pre-fix: n/a (no writer); the AC forbids a silent fallback in the one that exists now."""
    from nanobot import crash_record  # module absent on the pre-#1197 tree

    blocker = tmp_path / "state" / "bridge"
    blocker.parent.mkdir()
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        crash_record.record_exit(tmp_path / "state", outcome="failure", exit_status=1, error="boom")
    err = capsys.readouterr().err
    assert "bridge-exit-record: FAILED to write" in err and '"error": "boom"' in err


# ─── arming: upstream of the failing import, inert everywhere else ───────────

def test_import_time_crash_is_recorded_when_armed_by_the_package_import(tmp_path):
    """Pre-fix: ``import nanobot`` armed nothing; a crash at import left only a journal traceback."""

    state = tmp_path / "state"
    crash = "import nanobot\nraise NameError(\"name '_parse_explore_mode' is not defined\")\n"
    for _ in range(2):
        proc = _run(crash, state)
        assert proc.returncode == 1 and "NameError: name '_parse_explore_mode'" in proc.stderr, proc.stderr
    streak = _streak(state)
    assert streak["consecutive_failures"] == 2 and streak["last_exit_status"] == 1
    assert streak["last_error"] == "NameError: name '_parse_explore_mode' is not defined" and streak["last_where"].endswith("<string>:2")
    assert _rows(state)[-1]["source"] == "process"

    ok = _run("import nanobot\nfrom nanobot import crash_record\ncrash_record.record_exit(crash_record.state_dir(), outcome='success', exit_status=0)\n", state)
    assert ok.returncode == 0, ok.stderr
    assert _streak(state)["consecutive_failures"] == 0


def test_recorder_arms_on_python_dash_m_bridge_argv_and_stays_inert_elsewhere(tmp_path):
    from nanobot import crash_record  # module absent on the pre-#1197 tree

    state = tmp_path / "state"
    armed_by_argv = (
        "import sys\nsys.orig_argv[:] = ['python', '-m', 'nanobot.runtime.bridge']\n"
        "import nanobot\nraise RuntimeError('boom at import')\n"
    )
    proc = _run(armed_by_argv, state, marker="")
    assert proc.returncode == 1 and (state / "bridge" / "exit_streak.json").is_file(), proc.stderr
    assert _streak(state)["last_error"] == "RuntimeError: boom at import"

    other = tmp_path / "other"
    inert = _run("import nanobot\nraise RuntimeError('not the bridge')\n", other, marker="")
    assert inert.returncode == 1 and not (other / "bridge").exists(), "a non-bridge process must leave no record"
    disabled = _run("import sys\nsys.orig_argv[:] = ['python', '-m', 'nanobot.runtime.bridge']\nimport nanobot\nraise RuntimeError('x')\n", other, marker="0")
    assert disabled.returncode == 1 and not (other / "bridge").exists(), "NANOBOT_BRIDGE_EXIT_RECORD=0 disables"
    assert crash_record.is_bridge_invocation(["python", "-m", "nanobot.crash_record"], {}) is False
    assert crash_record.is_bridge_invocation(["/opt/eeepc-agent/venv/bin/python", "-m", "nanobot.runtime.bridge"], {}) is True


def test_state_dir_resolution_matches_the_unit_environment():
    from nanobot import crash_record  # module absent on the pre-#1197 tree

    assert crash_record.state_dir({"STATE_DIR": "/x/state", "NANOBOT_RUNTIME_STATE_ROOT": "/y"}) == Path("/x/state")
    assert crash_record.state_dir({"NANOBOT_RUNTIME_STATE_ROOT": "/y"}) == Path("/y")
    assert crash_record.state_dir({}) == Path("/var/lib/eeepc-agent/self-evolving-agent/state")
    bridge_src = (REPO / "nanobot" / "runtime" / "bridge.py").read_text(encoding="utf-8")
    assert f"os.environ.get('STATE_DIR', '{crash_record.DEFAULT_STATE_DIR}')" in bridge_src, "recorder default must mirror the bridge's"


def test_recorder_is_stdlib_only():
    """A recorder that imports package code can fail exactly when it is needed."""

    import ast

    tree = ast.parse((REPO / "nanobot" / "crash_record.py").read_text(encoding="utf-8"))
    imported = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    assert not {name for name in imported if name.startswith("nanobot")}, imported


# ─── the systemd route (ExecStopPost) and the bridge guard ───────────────────

def test_execstoppost_cli_records_success_and_failure(tmp_path, capsys):
    from nanobot import crash_record  # module absent on the pre-#1197 tree

    state = tmp_path / "state"
    assert crash_record.main(["--source", "systemd", "--exit-code", "exited", "--exit-status", "1", "--service-result", "exit-code", "--state-dir", str(state)]) == 0
    assert crash_record.main(["--source", "systemd", "--exit-code", "killed", "--exit-status", "KILL", "--service-result", "signal", "--state-dir", str(state)]) == 0
    assert _streak(state)["consecutive_failures"] == 2
    assert crash_record.main(["--source", "systemd", "--exit-code", "exited", "--exit-status", "0", "--service-result", "success", "--state-dir", str(state)]) == 0
    assert _streak(state)["consecutive_failures"] == 0
    out = capsys.readouterr().out.splitlines()
    assert json.loads(out[-1]) == {"outcome": "success", "consecutive_failures": 0}
    blocker = tmp_path / "blocked" / "bridge"
    blocker.parent.mkdir()
    blocker.write_text("file", encoding="utf-8")
    assert crash_record.main(["--source", "systemd", "--exit-status", "1", "--service-result", "exit-code", "--state-dir", str(tmp_path / "blocked")]) == 2


def test_tracked_drop_in_carries_the_execstoppost_line_and_says_it_is_inert():
    """Pre-fix: the tracked drop-in had no ExecStopPost."""

    conf = (REPO / "host" / "eeepc" / "systemd" / "drop-ins" / "eeepc-self-evolving-subagent-bridge.service.d" / "override.conf").read_text(encoding="utf-8")
    line = next(line for line in conf.splitlines() if line.startswith("ExecStopPost="))
    assert "-m nanobot.crash_record --source systemd --exit-code ${EXIT_CODE} --exit-status ${EXIT_STATUS} --service-result ${SERVICE_RESULT}" in line
    assert "INERT" in conf and "install.sh" in conf


def test_bridge_guard_records_the_exit_code_and_stays_last():
    """Pre-fix: the guard was ``raise SystemExit(cli_main())`` — no record of the exit."""

    import ast

    src = (REPO / "nanobot" / "runtime" / "bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    guard = tree.body[-1]
    assert isinstance(guard, ast.If) and "__main__" in ast.dump(guard.test)
    body_src = ast.get_source_segment(src, guard)
    assert "_crash_record.record_exit(" in body_src and "if BRIDGE_ENABLED:" in body_src
    assert "raise SystemExit(_exit_code)" in body_src


# ─── the health surface ──────────────────────────────────────────────────────

def test_scorecard_reports_the_streak_and_distinguishes_absent_from_zero(tmp_path):
    """Pre-fix: the scorecard had no bridge section; 140 failures and a healthy loop looked identical."""
    from nanobot import crash_record  # module absent on the pre-#1197 tree

    state = tmp_path / "state"
    state.mkdir()
    absent = scorecard._bridge_section(state, [])
    assert absent["reader_status"] == "absent" and absent["consecutive_failures"] is None
    for i in range(16):
        crash_record.record_exit(state, outcome="failure", exit_status=1, error="NameError: x", now=NOW - timedelta(hours=1, minutes=48 - 3 * i))
    section = scorecard._bridge_section(state, [{"ts": crash_record._now_iso(NOW - timedelta(hours=1))}])
    assert (section["reader_status"], section["consecutive_failures"], section["total_failures"]) == ("present", 16, 16)
    assert section["last_error"] == "NameError: x" and section["last_exit_status"] == 1
    # the recorder stopped writing while the ledger kept moving: visible, not "0 failures"
    stale = scorecard._bridge_section(state, [{"ts": crash_record._now_iso(NOW + timedelta(hours=3))}])
    assert stale["reader_status"] == "stale" and stale["consecutive_failures"] == 16
    (state / "bridge" / "exit_streak.json").write_text("{not json", encoding="utf-8")
    assert scorecard._bridge_section(state, [])["reader_status"] == "corrupt"
