"""ADR-043 operator resume CLI contract tests."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.runtime import operator_recovery

runner = CliRunner()


def _state(root: Path, **overrides) -> tuple[Path, dict]:
    path = root / "planner" / "no_plan_recovery.json"
    path.parent.mkdir(parents=True)
    raw = {
        "schema": "no-plan-recovery-v1", "consecutive_supply": 2,
        "consecutive_planner": 6, "minimal_mode": True, "stopped": True,
        "planner_degraded": True, "model_supply_degraded": False,
        "deferred_verdict_ids": ["hyp-1"], "future_field": {"keep": True},
    }
    raw.update(overrides)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path, raw


def _run(root: Path):
    return runner.invoke(app, ["recovery-resume", "--runtime-state-root", str(root)])


def _fake_fcntl(monkeypatch, *, busy=False):
    import builtins
    from types import SimpleNamespace
    real_import = builtins.__import__
    def fake_import(name, *args, **kwargs):
        if name == "fcntl":
            def flock(*_args):
                if busy:
                    raise BlockingIOError()
            return SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=flock)
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_recovery_resume_requires_explicit_root_and_reports_safe_result(tmp_path, monkeypatch):
    _fake_fcntl(monkeypatch)
    monkeypatch.setattr(operator_recovery, "_fsync_directory", lambda path: None)
    from nanobot.cli.eeebot import app as eeebot_app
    alias_help = runner.invoke(eeebot_app, ["recovery-resume", "--help"], color=False)
    assert alias_help.exit_code == 0 and "runtime-state-root" in alias_help.stdout
    missing = runner.invoke(app, ["recovery-resume"])
    assert missing.exit_code != 0
    state_path, original = _state(tmp_path)
    result = _run(tmp_path)
    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload == {"result": "resumed", "state_status": "verified", "state_durability": "verified", "audit_status": "verified", "category": "ok"}
    assert _run(tmp_path).exit_code != 0
    updated = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated == {**original, "stopped": False}
    events = [json.loads(line) for line in (tmp_path / "ledger" / "cycles.jsonl").read_text().splitlines()]
    assert len(events) == 1
    assert {k: events[0][k] for k in ("phase", "cycle_id", "family", "consecutive_count", "state")} == {
        "phase": "no_plan_recovery", "cycle_id": "operator-resume", "family": "planner", "consecutive_count": 6, "state": "normal",
    }
    assert "hyp-1" not in result.stdout and "future_field" not in result.stdout


@pytest.mark.parametrize("change", [
    {"schema": "wrong"}, {"consecutive_planner": True}, {"consecutive_supply": -1}, {"consecutive_planner": 1.5},
    {"stopped": False}, {"minimal_mode": 1}, {"deferred_verdict_ids": [1]},
])
def test_invalid_or_nonstopped_state_is_unchanged(tmp_path, change):
    state_path, raw = _state(tmp_path, **change)
    before = state_path.read_bytes()
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert state_path.read_bytes() == before
    assert not (tmp_path / "ledger").exists()
    assert raw.get("secret") not in result.stdout if raw.get("secret") else True


@pytest.mark.parametrize("symlinked", ["root", "lock", "state_directory", "state_file"])
def test_symlink_state_paths_refuse_without_mutation(tmp_path, symlinked):
    import pytest

    if symlinked == "root":
        target_root = tmp_path / "target-root"
        state_path, _ = _state(target_root)
        link_root = tmp_path / "root-link"
        try:
            link_root.symlink_to(target_root, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable")
        root = link_root
        target = state_path
    else:
        root = tmp_path / "root"
        state_path, _ = _state(root)
        target = root / "outside-target"
        target.write_bytes(state_path.read_bytes())
        try:
            if symlinked == "lock":
                (root / "bridge.lock").symlink_to(target)
            elif symlinked == "state_file":
                state_path.unlink()
                state_path.symlink_to(target)
            else:
                external_dir = root / "outside-planner"
                external_dir.mkdir()
                external_state = external_dir / state_path.name
                external_state.write_bytes(state_path.read_bytes())
                state_path.unlink()
                state_path.parent.rmdir()
                state_path.parent.symlink_to(external_dir, target_is_directory=True)
                target = external_state
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable")
    before = target.read_bytes()
    result = _run(root)
    assert result.exit_code != 0
    assert target.read_bytes() == before


def test_deeply_nested_json_refuses_without_mutation(tmp_path):
    state_path, _ = _state(tmp_path)
    state_path.write_text('{"schema":"no-plan-recovery-v1","nested":' + "[" * 5000 + "0" + "]" * 5000 + "}", encoding="utf-8")
    before = state_path.read_bytes()
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert json.loads(result.stdout)["result"] == "refused"
    assert state_path.read_bytes() == before
    assert not (tmp_path / "ledger").exists()


def test_missing_known_field_refuses_without_mutation(tmp_path):
    state_path, raw = _state(tmp_path)
    raw.pop("consecutive_planner")
    state_path.write_text(json.dumps(raw), encoding="utf-8")
    before = state_path.read_bytes()
    result = _run(tmp_path)
    assert result.exit_code != 0 and state_path.read_bytes() == before
    assert not (tmp_path / "ledger").exists()


def test_missing_malformed_and_invalid_utf8_state_refuse_without_mutation(tmp_path):
    (tmp_path / "planner").mkdir()
    result = _run(tmp_path)
    assert result.exit_code != 0
    path = tmp_path / "planner" / "no_plan_recovery.json"
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    result = _run(tmp_path)
    assert result.exit_code != 0 and path.read_bytes() == before
    path.write_bytes(b"\xff")
    before = path.read_bytes()
    result = _run(tmp_path)
    assert result.exit_code != 0 and path.read_bytes() == before


def test_resume_refuses_busy_or_unsupported_lock(tmp_path, monkeypatch):
    state_path, _ = _state(tmp_path)
    before = state_path.read_bytes()
    _fake_fcntl(monkeypatch, busy=True)
    result = _run(tmp_path)
    assert result.exit_code != 0 and state_path.read_bytes() == before


@pytest.mark.parametrize("failure", ["temporary_write", "replace"])
def test_state_write_failure_is_refused_without_mutation(tmp_path, monkeypatch, failure):
    _fake_fcntl(monkeypatch)
    state_path, _ = _state(tmp_path)
    before = state_path.read_bytes()
    if failure == "replace":
        monkeypatch.setattr(operator_recovery.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("injected replace")))
    else:
        original_dump = operator_recovery.json.dump
        def fail_temp_write(*args, **kwargs):
            original_dump(*args, **kwargs)
            raise OSError("injected temporary write")
        monkeypatch.setattr(operator_recovery.json, "dump", fail_temp_write)
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert json.loads(result.stdout)["state_status"] == "unchanged"
    assert state_path.read_bytes() == before
    assert not (tmp_path / "ledger").exists()


def test_state_directory_fsync_failure_is_partial_and_skips_ledger(tmp_path, monkeypatch):
    _fake_fcntl(monkeypatch)
    state_path, _ = _state(tmp_path)
    original = operator_recovery._fsync_directory
    calls = 0
    def fail_parent_sync(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected state directory sync")
        return original(path)
    monkeypatch.setattr(operator_recovery, "_fsync_directory", fail_parent_sync)
    result = _run(tmp_path)
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["result"] == "partial"
    assert payload["state_status"] == "verified"
    assert payload["state_durability"] == "unverified"
    assert payload["audit_status"] == "not_attempted"
    assert json.loads(state_path.read_text())["stopped"] is False
    assert not (tmp_path / "ledger").exists()


def test_unterminated_ledger_line_refuses_before_state_change(tmp_path):
    state_path, _ = _state(tmp_path)
    ledger = tmp_path / "ledger" / "cycles.jsonl"
    ledger.parent.mkdir()
    ledger.write_bytes(b'{"phase":"prior"}')
    state_before = state_path.read_bytes()
    ledger_before = ledger.read_bytes()
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert state_path.read_bytes() == state_before
    assert ledger.read_bytes() == ledger_before


def test_rotation_archive_failure_is_partial_and_not_success(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from nanobot.runtime import cycle_ledger

    _fake_fcntl(monkeypatch)
    state_path, _ = _state(tmp_path)
    ledger = tmp_path / "ledger" / "cycles.jsonl"
    ledger.parent.mkdir()
    ledger.write_bytes(b'{"phase":"prior"}' + bytes([10]))
    archive = ledger.parent / "cycles-2020-01-01.jsonl.gz"
    stale = datetime.now(timezone.utc) - timedelta(days=1)
    monkeypatch.setattr(cycle_ledger, "_file_mtime_local", lambda path: stale)
    monkeypatch.setattr(cycle_ledger.gzip, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected archive failure")))
    result = _run(tmp_path)
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["result"] == "partial"
    assert payload["state_status"] == "verified"
    assert payload["audit_status"] == "not_attempted"
    assert json.loads(state_path.read_text())["stopped"] is False
    assert not archive.exists()


def test_cutover_marker_failure_is_partial_and_not_success(tmp_path, monkeypatch):
    from nanobot.runtime import day_key

    _fake_fcntl(monkeypatch)
    state_path, _ = _state(tmp_path)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    archive = ledger_dir / "cycles-2020-01-01.jsonl.gz"
    archive.write_bytes(b"prior compressed archive placeholder")
    monkeypatch.setattr(day_key, "record_cutover_if_absent", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected cutover failure")))
    result = _run(tmp_path)
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["result"] == "partial"
    assert payload["state_status"] == "verified"
    assert payload["audit_status"] == "not_attempted"
    assert json.loads(state_path.read_text())["stopped"] is False
    assert not (ledger_dir / "cycles.jsonl").exists()


def test_ledger_rotation_and_cutover_preparation_precede_resume_transition(tmp_path, monkeypatch):
    import os
    from datetime import datetime, timedelta, timezone

    from nanobot.runtime import cycle_ledger

    _fake_fcntl(monkeypatch)
    state_path, _ = _state(tmp_path)
    ledger = tmp_path / "ledger" / "cycles.jsonl"
    ledger.parent.mkdir()
    ledger.write_text('{"phase":"old"}\n', encoding="utf-8")
    os.utime(ledger, ((datetime.now(timezone.utc) - timedelta(days=1)).timestamp(),) * 2)
    monkeypatch.setattr(operator_recovery, "_fsync_directory", lambda path: None)
    result = _run(tmp_path)
    assert result.exit_code == 0
    archives = list(ledger.parent.glob("cycles-*.jsonl.gz"))
    assert len(archives) == 1
    assert cycle_ledger.read_events(tmp_path)[-1]["cycle_id"] == "operator-resume"
    assert json.loads(state_path.read_text())["stopped"] is False


def test_ledger_failure_is_partial_and_never_rolls_back(tmp_path, monkeypatch):
    _fake_fcntl(monkeypatch)
    monkeypatch.setattr(operator_recovery, "_fsync_directory", lambda path: None)
    state_path, _ = _state(tmp_path)
    monkeypatch.setattr(operator_recovery, "_append_transition", lambda *args: (_ for _ in ()).throw(OSError("injected")))
    result = _run(tmp_path)
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["result"] == "partial" and payload["state_status"] == "verified"
    assert payload["state_durability"] == "verified"
    assert payload["audit_status"] == "unverified"
    assert json.loads(state_path.read_text())["stopped"] is False


def test_ledger_fsync_failure_reports_event_if_readback_proves_it(tmp_path, monkeypatch):
    _fake_fcntl(monkeypatch)
    monkeypatch.setattr(operator_recovery, "_fsync_directory", lambda path: None)
    state_path, _ = _state(tmp_path)
    original = operator_recovery.os.fsync
    calls = 0
    def fail_ledger_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected ledger fsync")
        return original(fd)
    monkeypatch.setattr(operator_recovery.os, "fsync", fail_ledger_sync)
    result = _run(tmp_path)
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["result"] == "partial" and payload["audit_status"] == "unverified"
    assert json.loads(state_path.read_text())["stopped"] is False
    monkeypatch.setattr(operator_recovery.os, "fsync", original)


@pytest.mark.parametrize("failure", ["directory", "file"])
def test_ledger_symlink_refuses_before_state_or_outside_file_changes(tmp_path, failure, monkeypatch):
    import os

    _fake_fcntl(monkeypatch)
    state_path, _ = _state(tmp_path)
    outside_dir = tmp_path.parent / f"outside-ledger-{failure}"
    outside_dir.mkdir()
    outside = outside_dir / "cycles.jsonl"
    outside.write_bytes(b'{"outside":true}\\n')
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    try:
        if failure == "directory":
            link = tmp_path / "ledger-link"
            link.symlink_to(outside_dir, target_is_directory=True)
            ledger_dir.rmdir()
            os.replace(link, ledger_dir)
        else:
            ledger = ledger_dir / "cycles.jsonl"
            link = tmp_path / "ledger-link"
            link.symlink_to(outside)
            os.replace(link, ledger)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    before = state_path.read_bytes()
    outside_before = outside.read_bytes()
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert state_path.read_bytes() == before
    assert outside.read_bytes() == outside_before


def test_existing_unterminated_ledger_is_not_verified_as_a_new_row(tmp_path, monkeypatch):
    _fake_fcntl(monkeypatch)
    monkeypatch.setattr(operator_recovery, "_fsync_directory", lambda path: None)
    state_path, _ = _state(tmp_path)
    ledger = tmp_path / "ledger" / "cycles.jsonl"
    ledger.parent.mkdir()
    prior = b'{"phase":"prior"}'
    ledger.write_bytes(prior)
    before = state_path.read_bytes()
    result = _run(tmp_path)
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["audit_status"] == "not_attempted"
    assert state_path.read_bytes() == before
    assert ledger.read_bytes() == prior


def test_ledger_write_that_cannot_be_verified_is_not_claimed_absent(tmp_path, monkeypatch):
    _fake_fcntl(monkeypatch)
    monkeypatch.setattr(operator_recovery, "_fsync_directory", lambda path: None)
    state_path, _ = _state(tmp_path)
    original = operator_recovery._append_transition
    def append_then_fail(root, count):
        original(root, count)
        raise operator_recovery.AuditWriteError(attempted=True, durable=True)
    monkeypatch.setattr(operator_recovery, "_append_transition", append_then_fail)
    result = _run(tmp_path)
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["result"] == "partial" and payload["audit_status"] == "verified"
    assert json.loads(state_path.read_text())["stopped"] is False


def test_real_bridge_flock_contention_refuses_without_state_change(tmp_path):
    import pytest
    fcntl = pytest.importorskip("fcntl")

    state_path, _ = _state(tmp_path)
    before = state_path.read_bytes()
    lock_path = tmp_path / "bridge.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(tmp_path)
        assert result.exit_code != 0
        assert state_path.read_bytes() == before


def test_unreadable_state_refuses_without_mutation(tmp_path, monkeypatch):
    import os

    import pytest

    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("permission test requires an unprivileged POSIX user")
    state_path, _ = _state(tmp_path)
    before = state_path.read_bytes()
    state_path.chmod(0)
    try:
        result = _run(tmp_path)
    finally:
        state_path.chmod(0o600)
    assert result.exit_code != 0
    assert state_path.read_bytes() == before


def test_permission_error_is_bounded_refusal_on_every_platform(tmp_path, monkeypatch):
    state_path, _ = _state(tmp_path)
    before = state_path.read_bytes()
    original_read_text = Path.read_text

    def fail_state_read(path, *args, **kwargs):
        if path == state_path:
            raise PermissionError("injected permission denial")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_state_read)
    result = _run(tmp_path)
    assert result.exit_code != 0
    assert json.loads(result.stdout)["result"] == "refused"
    assert state_path.read_bytes() == before
    assert not (tmp_path / "ledger").exists()


def test_unsupported_lock_fails_closed(tmp_path, monkeypatch):
    state_path, _ = _state(tmp_path)
    before = state_path.read_bytes()
    _fake_fcntl(monkeypatch)
    import builtins
    real_import = builtins.__import__
    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("not available")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    result = _run(tmp_path)
    assert result.exit_code != 0 and state_path.read_bytes() == before
