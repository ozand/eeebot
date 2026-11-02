"""Per-task H5 warning evidence contract (ADR-039 / #2143)."""

from __future__ import annotations

import gzip
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

import nanobot.runtime.health as health
from nanobot.cli.commands import app
from nanobot.runtime.bridge import _retry_key_for
from nanobot.runtime.cycle_ledger import append_event, record_cycle_started, record_diary_open_entry, record_cycle_outcome
from nanobot.runtime.health import (
    build_cycle_health_summary,
    format_cycle_health_summary,
    read_task_attempt_warning,
)


def _iso_at(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _append(state, phase, cycle, key=None, outcome=None, ts=None):
    path = state / "ledger" / "cycles.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"phase": phase, "cycle_id": cycle, "ts": ts or "2026-11-01T12:00:00Z"}
    if key is not None:
        row["retry_key"] = key
    if outcome is not None:
        row["outcome"] = outcome
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _write_archive(state, name, rows):
    path = state / "ledger" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _anchored_attempts(state, key, count):
    _append(state, "outcome", f"reset-{key}", key, "success", "2026-11-01T11:00:00Z")
    for index in range(count):
        _append(state, "started", f"{key}-{index}", key, ts=f"2026-11-01T11:{index + 1:02d}:00Z")


def test_task_warning_threshold_exact_key_success_reset_and_privacy(tmp_path):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "state"
    _anchored_attempts(state, "private-alpha", 11)
    clear = read_task_attempt_warning(state, now=now)
    assert clear["status"] == "unknown"
    assert clear["warning"] is None
    assert clear["warning_tasks"] is None
    assert format_cycle_health_summary({"task_attempt_warning": clear})[7] == (
        "  Task-attempt warning: UNKNOWN (coverage incomplete)"
    )
    _append(state, "started", "alpha-11", "private-alpha", ts="2026-11-01T11:12:00Z")
    at_twelve = read_task_attempt_warning(state, now=now)
    assert at_twelve["status"] == "warning"
    assert at_twelve["warning"] is True
    assert at_twelve["warning_tasks"] is None
    _append(state, "started", "alpha-12", "private-alpha", ts="2026-11-01T11:13:00Z")
    at_thirteen = read_task_attempt_warning(state, now=now)
    assert at_thirteen["status"] == "warning"
    assert at_thirteen["warning_tasks"] is None
    for index in range(12):
        _append(state, "started", f"alpha-skip-{index}", "private-alpha", ts=f"2026-11-01T11:{14 + index:02d}:00Z")
        _append(state, "outcome", f"alpha-skip-{index}", "private-alpha", "skipped-duplicate", f"2026-11-01T11:{14 + index:02d}:30Z")
    after_duplicate_skips = read_task_attempt_warning(state, now=now)
    assert after_duplicate_skips["status"] == "warning"
    assert after_duplicate_skips["warning_tasks"] is None

    interleaved = tmp_path / "interleaved"
    _anchored_attempts(interleaved, "private-alpha", 11)
    _append(interleaved, "outcome", "beta-reset", "private-beta", "success", "2026-11-01T11:19:00Z")
    for index in range(11):
        _append(interleaved, "started", f"beta-{index}", "private-beta", ts=f"2026-11-01T11:{20 + index:02d}:00Z")
    before_beta_twelve = read_task_attempt_warning(interleaved, now=now)
    assert before_beta_twelve["status"] == "unknown"
    _append(interleaved, "started", "beta-11", "private-beta", ts="2026-11-01T11:31:00Z")
    beta_twelve = read_task_attempt_warning(interleaved, now=now)
    assert beta_twelve["status"] == "warning"
    assert beta_twelve["warning_tasks"] is None

    reset = tmp_path / "reset"
    _anchored_attempts(reset, "private-alpha", 12)
    _append(reset, "outcome", "alpha-success", "private-alpha", "success", "2026-11-01T11:22:00Z")
    after_success = read_task_attempt_warning(reset, now=now)
    assert after_success["status"] == "unknown"
    assert after_success["warning"] is None
    assert after_success["warning_tasks"] is None

    result = read_task_attempt_warning(state, now=now)
    assert result == {
        "threshold": 12,
        "coverage_days": 90,
        "status": "warning",
        "coverage": "partial",
        "warning": True,
        "warning_tasks": None,
    }
    assert "private-alpha" not in json.dumps(result)
    assert "private-beta" not in json.dumps(result)


def test_task_warning_ignores_valid_diary_phase_rows_from_real_writer(tmp_path):
    state = tmp_path / "real-writers"
    reset_at = datetime.now(timezone.utc) - timedelta(hours=1)
    _append(state, "outcome", "reset", "private-alpha", "success", _iso_at(reset_at))
    for index in range(12):
        cycle = f"real-cycle-{index}"
        record_cycle_started(state, cycle, f"request-{index}", None, retry_key="private-alpha")
        record_diary_open_entry(state, cycle, "integrated", None, "recorded before cycle")
        append_event(state, {
            "phase": "open_increment", "cycle_id": cycle, "retry_key": "private-alpha",
            "status": "interrupted_supply", "consecutive_supply_interrupts": index + 1,
        })

    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "warning"
    assert result["warning"] is True
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)


def test_self_title_edit_creates_distinct_warning_identity():
    first = _retry_key_for(None, "Review ADR design")
    edited = _retry_key_for(None, "Review ADR design revised")
    assert first != edited


def test_task_warning_joins_actual_success_outcome_by_cycle_id_and_retry_key(tmp_path):
    now = datetime.now(timezone.utc)
    state = tmp_path / "actual-outcome-writer"
    reset_ts = _iso_at(now - timedelta(hours=2))
    _append(state, "outcome", "prior-success", "private-alpha", "success", reset_ts)
    for index in range(11):
        cycle = f"writer-cycle-{index}"
        record_cycle_started(state, cycle, f"request-{index}", None, retry_key="private-alpha")
        record_diary_open_entry(state, cycle, "integrated", None, "real writer row")
    terminal = "writer-cycle-terminal"
    record_cycle_started(state, terminal, "terminal-request", None, retry_key="private-alpha")
    record_cycle_outcome(
        state, terminal, "success", None, [], None, retry_key="private-alpha",
    )

    result = read_task_attempt_warning(state, now=now + timedelta(seconds=1))
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)


def test_task_warning_unknown_legacy_and_keyless_late_push(tmp_path):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    legacy = tmp_path / "legacy"
    _append(legacy, "started", "old-start", ts="2026-11-01T11:00:00Z")
    assert read_task_attempt_warning(legacy, now=now)["status"] == "unknown"

    late = tmp_path / "late"
    _anchored_attempts(late, "private-alpha", 12)
    _append(late, "outcome", "late", None, "pushed_late", "2026-11-01T11:30:00Z")
    result = read_task_attempt_warning(late, now=now)
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)


def test_cycle_health_cli_reports_clear_and_unknown_warning_states(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    reset_ts = _iso_at(now - timedelta(minutes=60))
    start_ts = _iso_at(now - timedelta(minutes=30))
    state = tmp_path / "state"
    _append(state, "outcome", "reset", "private-clear", "success", reset_ts)
    _append(state, "started", "one", "private-clear", ts=start_ts)
    monkeypatch.setattr("nanobot.runtime.health.load_runtime_state_from_root", lambda *a, **k: {})
    monkeypatch.setattr("nanobot.runtime.health._ledger_age_seconds", lambda *_a, **_k: 0.0)
    monkeypatch.setattr("nanobot.runtime.health.read_cycle_progress", lambda *_a, **_k: {"state": "healthy", "alert": False})
    monkeypatch.setattr("nanobot.runtime.health.read_service_status", lambda *_a, **_k: {})
    monkeypatch.setattr("nanobot.runtime.health.read_failed_units_count", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health._promotion_readiness", lambda *_a, **_k: {"state": "ready"})
    monkeypatch.setattr("nanobot.runtime.health.read_autonomous_commits_24h", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_checkpoint_commits_24h", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_subagent_queue_depth", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_derived_priorities_queue", lambda *_a, **_k: {"depth": 0, "limit": 10})
    monkeypatch.setattr("nanobot.runtime.health._default_runner", lambda command: subprocess.CompletedProcess(command, 0, "", ""))

    runner = CliRunner()
    clear = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(state)])
    assert "Task-attempt warning: UNKNOWN (coverage incomplete)" in clear.stdout
    clear_json = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(state), "--json"])
    clear_payload = json.loads(clear_json.stdout)
    assert clear_json.exit_code == clear_payload["exit_code"]
    assert clear_payload["task_attempt_warning"]["status"] == "unknown"
    assert clear_payload["task_attempt_warning"]["warning"] is None

    unknown = tmp_path / "unknown"
    _append(unknown, "started", "legacy", ts="2026-11-01T11:00:00Z")
    unknown_text = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(unknown)])
    assert "Task-attempt warning: UNKNOWN (coverage incomplete)" in unknown_text.stdout
    result = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(unknown), "--json"])
    unknown_payload = json.loads(result.stdout)
    assert result.exit_code == unknown_payload["exit_code"]
    assert unknown_payload["task_attempt_warning"]["status"] == "unknown"
    assert unknown_payload["task_attempt_warning"]["warning"] is None
    assert "private-clear" not in result.stdout

    warning = tmp_path / "warning"
    _append(warning, "outcome", "reset", "private-warning", "success", reset_ts)
    for index in range(12):
        _append(warning, "started", f"attempt-{index}", "private-warning", ts=_iso_at(now - timedelta(minutes=12 - index)))
    warning_text = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(warning)])
    assert "Task-attempt warning: warning: at least one task has 12 evidenced starts" in warning_text.stdout
    assert "private-warning" not in warning_text.stdout
    result = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(warning), "--json"])
    warning_payload = json.loads(result.stdout)
    assert result.exit_code == warning_payload["exit_code"]
    assert warning_payload["task_attempt_warning"]["status"] == "warning"
    assert warning_payload["task_attempt_warning"]["warning"] is True
    assert "private-warning" not in result.stdout

    naive = tmp_path / "naive-cli"
    _append(naive, "outcome", "reset", "private-naive-cli", "success", "2026-11-01T10:00:00")
    for index in range(12):
        _append(naive, "started", f"attempt-{index}", "private-naive-cli", ts=f"2026-11-01T11:{index:02d}:00")
    naive_text = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(naive)])
    assert "Task-attempt warning: UNKNOWN (coverage incomplete)" in naive_text.stdout
    assert "private-naive-cli" not in naive_text.stdout
    naive_json = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(naive), "--json"])
    naive_payload = json.loads(naive_json.stdout)
    assert naive_payload["task_attempt_warning"]["status"] == "unknown"
    assert naive_payload["task_attempt_warning"]["warning"] is None
    assert "private-naive-cli" not in naive_json.stdout


def test_task_warning_naive_ledger_timestamps_are_unknown(tmp_path):
    """ADR-039: local-time-naive timestamps cannot establish task evidence."""
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "naive"
    _append(state, "outcome", "reset", "private-naive", "success", "2026-11-01T10:00:00")
    for index in range(12):
        _append(state, "started", f"naive-{index}", "private-naive", ts=f"2026-11-01T11:{index:02d}:00")
    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "private-naive" not in json.dumps(result)


def test_task_warning_future_dated_starts_are_unknown(tmp_path):
    """ADR-039: future starts are not current counted evidence."""
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "future"
    _append(state, "outcome", "reset", "private-future", "success", "2026-11-01T11:00:00Z")
    for index in range(12):
        _append(state, "started", f"future-{index}", "private-future", ts=f"2026-11-02T11:{index:02d}:00Z")
    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "private-future" not in json.dumps(result)


def test_task_warning_extreme_retention_returns_unknown(tmp_path, monkeypatch):
    """ADR-039: unrepresentable coverage horizons fail closed to UNKNOWN."""
    monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "999999999999")
    result = read_task_attempt_warning(tmp_path / "state", now=datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None


@pytest.mark.parametrize("compressed", [False, True])
def test_task_warning_invalid_utf8_retry_key_is_unknown(tmp_path, compressed):
    now = datetime.now(timezone.utc)
    state = tmp_path / ("invalid-gzip" if compressed else "invalid-plain")
    ledger = state / "ledger"
    ledger.mkdir(parents=True)
    rows = [
        {"phase": "outcome", "cycle_id": "reset", "retry_key": "private-alpha", "outcome": "success", "ts": _iso_at(now - timedelta(hours=1))},
    ]
    rows.extend(
        {"phase": "started", "cycle_id": f"attempt-{index}", "retry_key": "private-alpha", "ts": _iso_at(now - timedelta(minutes=50 - index))}
        for index in range(12)
    )
    newline = bytes([10])
    payload = b"".join(json.dumps(row).encode("utf-8") + newline for row in rows)
    payload += b'{"phase":"started","cycle_id":"corrupt","retry_key":"bad' + bytes([255]) + b'key","ts":"' + _iso_at(now).encode("ascii") + b'"}' + newline
    path = ledger / ("cycles-2026-11-01.jsonl.gz" if compressed else "cycles.jsonl")
    path.write_bytes(gzip.compress(payload) if compressed else payload)

    result = read_task_attempt_warning(state, now=now + timedelta(seconds=1))
    assert result["status"] == "unknown"
    assert result["coverage"] == "incomplete"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "bad" not in json.dumps(result)
    assert "private-alpha" not in json.dumps(result)


def test_task_warning_valid_unicode_replacement_character_is_not_rejected(tmp_path):
    now = datetime.now(timezone.utc)
    state = tmp_path / "valid-unicode"
    _append(state, "outcome", "reset", "task-�", "success", _iso_at(now - timedelta(hours=1)))
    for index in range(12):
        _append(state, "started", f"unicode-{index}", "task-�", ts=_iso_at(now - timedelta(minutes=50 - index)))

    result = read_task_attempt_warning(state, now=now + timedelta(seconds=1))
    assert result["status"] == "warning"
    assert result["warning"] is True
    assert result["warning_tasks"] is None


def test_task_warning_expired_and_pruned_other_task_history_never_proves_clear(tmp_path):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "expired-other-task"
    ledger = state / "ledger"
    ledger.mkdir(parents=True)
    expired = [
        {"phase": "started", "cycle_id": f"beta-{index}", "retry_key": "private-beta", "ts": f"2026-07-23T11:{index:02d}:00Z"}
        for index in range(12)
    ]
    unseen = [
        {"phase": "started", "cycle_id": f"gamma-{index}", "retry_key": "private-gamma", "ts": f"2026-05-01T11:{index:02d}:00Z"}
        for index in range(12)
    ]
    recent = [
        {"phase": "outcome", "cycle_id": "alpha-reset", "retry_key": "private-alpha", "outcome": "success", "ts": "2026-10-31T23:00:00Z"},
        {"phase": "started", "cycle_id": "alpha-start", "retry_key": "private-alpha", "ts": "2026-11-01T11:00:00Z"},
    ]
    active = ledger / "cycles.jsonl"
    active.write_text("".join(json.dumps(row) + "\n" for row in [*expired, *unseen, *recent]), encoding="utf-8")

    for label in ("expired rows retained", "expired rows physically pruned", "unseen prior history"):
        result = read_task_attempt_warning(state, now=now)
        assert result["status"] == "unknown"
        assert result["warning"] is None
        assert result["warning_tasks"] is None
        assert "private-beta" not in json.dumps(result)
        assert "private-gamma" not in json.dumps(result)
        if label == "expired rows retained":
            active.write_text("".join(json.dumps(row) + "\n" for row in recent), encoding="utf-8")


def test_task_warning_empty_or_unanchored_ledger_activity_is_unknown(tmp_path, monkeypatch):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "1")

    empty = tmp_path / "empty"
    _append(empty, "gate", "recent-gate", ts="2026-11-01T11:00:00Z")
    assert read_task_attempt_warning(empty, now=now)["status"] == "unknown"

    expired = tmp_path / "expired"
    _append(expired, "started", "old-start", "private-expired", ts="2026-10-20T11:00:00Z")
    _append(expired, "gate", "recent-gate", ts="2026-11-01T11:00:00Z")
    assert read_task_attempt_warning(expired, now=now)["status"] == "unknown"

    mixed = tmp_path / "mixed"
    _append(mixed, "outcome", "reset", "private-known", "success", "2026-11-01T10:00:00Z")
    _append(mixed, "started", "one", "private-known", ts="2026-11-01T11:00:00Z")
    _append(mixed, "started", "legacy", ts="2026-11-01T11:01:00Z")
    assert read_task_attempt_warning(mixed, now=now)["status"] == "unknown"


def test_task_warning_capped_prefix_does_not_ignore_later_success_reset(tmp_path, monkeypatch):
    """ADR-039: prefix truncation cannot prove no later success reset."""
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "capped"
    _append(state, "outcome", "anchor", "private-alpha", "success", "2026-11-01T10:00:00Z")
    for index in range(12):
        _append(state, "started", f"attempt-{index}", "private-alpha", ts=f"2026-11-01T10:{index:02d}:00Z")
    for index in range(50):
        _append(state, "gate", f"filler-{index}", ts=f"2026-11-01T11:{index:02d}:00Z")
    _append(state, "outcome", "late-success", "private-alpha", "success", "2026-11-01T11:59:00Z")

    original_reader = health.ledger_window
    def capped_reader(*args, **kwargs):
        kwargs["max_bytes"] = 2400
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(health, "ledger_window", capped_reader)
    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)


def test_task_warning_archive_only_history_is_unknown_in_reader_and_cli(tmp_path, monkeypatch):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "archive-only"
    archive_rows = [
        {"phase": "outcome", "cycle_id": "reset", "retry_key": "private-alpha", "outcome": "success", "ts": "2026-10-31T23:00:00Z"},
    ]
    archive_rows.extend(
        {"phase": "started", "cycle_id": f"attempt-{index}", "retry_key": "private-alpha", "ts": f"2026-11-01T00:{index:02d}:00Z"}
        for index in range(12)
    )
    _write_archive(state, "cycles-2026-10-31.jsonl.gz", archive_rows)

    direct = read_task_attempt_warning(state, now=now)
    assert direct["status"] == "unknown"
    assert direct["warning"] is None
    assert direct["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(direct)

    monkeypatch.setattr("nanobot.runtime.health.load_runtime_state_from_root", lambda *a, **k: {})
    monkeypatch.setattr("nanobot.runtime.health._ledger_age_seconds", lambda *_a, **_k: 0.0)
    monkeypatch.setattr("nanobot.runtime.health.read_cycle_progress", lambda *_a, **_k: {"state": "healthy", "alert": False})
    monkeypatch.setattr("nanobot.runtime.health.read_service_status", lambda *_a, **_k: {})
    monkeypatch.setattr("nanobot.runtime.health.read_failed_units_count", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health._promotion_readiness", lambda *_a, **_k: {"state": "ready"})
    monkeypatch.setattr("nanobot.runtime.health.read_autonomous_commits_24h", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_checkpoint_commits_24h", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_subagent_queue_depth", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_derived_priorities_queue", lambda *_a, **_k: {"depth": 0, "limit": 10})
    monkeypatch.setattr("nanobot.runtime.health._default_runner", lambda command: subprocess.CompletedProcess(command, 0, "", ""))
    result = CliRunner().invoke(app, ["cycle-health", "--runtime-state-root", str(state), "--json"])
    payload = json.loads(result.stdout)
    warning = payload["task_attempt_warning"]
    assert warning["status"] == "unknown"
    assert warning["warning"] is None
    assert warning["warning_tasks"] is None
    assert "private-alpha" not in result.stdout


def test_task_warning_includes_rotated_attempts_and_keeps_other_task_warning(tmp_path):
    """ADR-039: archive plus active-file attempts compose across rotation."""
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "rotated"
    archive_rows = [
        {"phase": "outcome", "cycle_id": "a-reset", "retry_key": "private-alpha", "outcome": "success", "ts": "2026-10-31T23:00:00Z"},
    ]
    archive_rows.extend(
        {"phase": "started", "cycle_id": f"a-{index}", "retry_key": "private-alpha", "ts": f"2026-10-31T23:{index:02d}:00Z"}
        for index in range(6)
    )
    _write_archive(state, "cycles-2026-10-31.jsonl.gz", archive_rows)
    for index in range(6):
        _append(state, "started", f"a-today-{index}", "private-alpha", ts=f"2026-11-01T00:{index:02d}:00Z")
    _append(state, "outcome", "b-reset", "private-beta", "success", "2026-11-01T01:00:00Z")

    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "warning"
    assert result["warning"] is True
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)


def test_task_warning_counts_same_cycle_keep_resume_start_writes(tmp_path):
    now = datetime.now(timezone.utc)
    state = tmp_path / "kept-resume"
    _append(state, "outcome", "prior-success", "private-alpha", "success", _iso_at(now - timedelta(hours=1)))

    results = []
    for attempt in range(12):
        record_cycle_started(
            state, "kept-cycle", f"resume-request-{attempt}", None,
            retry_key="private-alpha",
        )
        results.append(read_task_attempt_warning(state, now=datetime.now(timezone.utc) + timedelta(seconds=1)))

    assert results[10]["status"] == "unknown"
    assert results[10]["warning"] is None
    assert results[11]["status"] == "warning"
    assert results[11]["warning"] is True
    assert results[11]["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(results[11])


def test_task_warning_mismatched_or_unjoined_duplicate_skip_is_unknown(tmp_path):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    for case, skip_cycle, skip_key in (
        ("mismatched", "attempt-0", "private-beta"),
        ("unjoined", "not-started", "private-alpha"),
    ):
        state = tmp_path / case
        _append(state, "outcome", "anchor", "private-alpha", "success", "2026-11-01T10:00:00Z")
        for index in range(12):
            _append(state, "started", f"attempt-{index}", "private-alpha", ts=f"2026-11-01T11:{index:02d}:00Z")
        _append(state, "outcome", skip_cycle, skip_key, "skipped-duplicate", "2026-11-01T11:30:00Z")

        result = read_task_attempt_warning(state, now=now)
        assert result["status"] == "unknown"
        assert result["warning"] is None
        assert result["warning_tasks"] is None
        assert "private-alpha" not in json.dumps(result)
        assert "private-beta" not in json.dumps(result)


def test_task_warning_duplicate_skip_key_must_match_started_cycle_and_is_unique(tmp_path):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "duplicate-key"
    _append(state, "outcome", "anchor", "private-alpha", "success", "2026-11-01T10:00:00Z")
    for index in range(12):
        _append(state, "started", f"attempt-{index}", "private-alpha", ts=f"2026-11-01T11:{index:02d}:00Z")
    _append(state, "outcome", "attempt-0", "private-alpha", "skipped-duplicate", "2026-11-01T11:30:00Z")
    _append(state, "outcome", "attempt-0", "private-alpha", "skipped-duplicate", "2026-11-01T11:31:00Z")

    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)


@pytest.mark.parametrize("start_count", [12, 13])
@pytest.mark.parametrize("skip_key", [None, "private-alpha"])
def test_task_warning_reused_cycle_duplicate_skip_is_unknown(tmp_path, start_count, skip_key):
    now = datetime.now(timezone.utc)
    state = tmp_path / f"reused-cycle-{start_count}-{skip_key}"
    _append(state, "outcome", "prior-success", "private-alpha", "success", _iso_at(now - timedelta(hours=1)))
    for attempt in range(start_count):
        record_cycle_started(state, "kept-cycle", f"resume-request-{attempt}", None, retry_key="private-alpha")
    record_cycle_outcome(state, "kept-cycle", "skipped-duplicate", "already_done", [], None, retry_key=skip_key)

    result = read_task_attempt_warning(state, now=now + timedelta(seconds=1))
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)


@pytest.mark.parametrize(
    ("resolution", "reason"),
    [
        ("pushed_late", "delivery_unknown"),
        ("superseded", "push_pending_main_moved"),
        ("abandoned", "push_pending_branch_missing"),
        ("partial", "service_only"),
    ],
)
def test_task_warning_accepts_push_pending_resolution_and_counts_later_attempts(tmp_path, resolution, reason):
    now = datetime.now(timezone.utc)
    state = tmp_path / f"pending-{resolution}"
    record_cycle_started(state, "pending-cycle", "pending-request", None, retry_key="private-alpha")
    record_cycle_outcome(
        state, "pending-cycle", "push_pending", "push_failed", [], None,
        retry_key="private-alpha",
    )
    record_cycle_outcome(
        state, "pending-cycle", resolution, reason, [], None,
    )

    for index in range(12):
        record_cycle_started(
            state, f"later-{index}", f"later-request-{index}", None,
            retry_key="private-beta",
        )
    result = read_task_attempt_warning(state, now=now + timedelta(seconds=1))
    assert result["status"] == "warning"
    assert result["warning"] is True
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)
    assert "private-beta" not in json.dumps(result)


@pytest.mark.parametrize("resolution", ["pushed_late", "superseded", "abandoned"])
def test_task_warning_push_pending_resolution_allows_later_keyed_warning(tmp_path, resolution):
    now = datetime.now(timezone.utc)
    state = tmp_path / f"pending-resolution-{resolution}"
    record_cycle_started(state, "pending-cycle", "pending-request", None, retry_key="private-alpha")
    record_cycle_outcome(
        state, "pending-cycle", "push_pending", "push_failed", [], None,
        retry_key="private-alpha",
    )
    record_cycle_outcome(
        state, "pending-cycle", resolution, "resolved_pending_push", [], None,
    )
    for index in range(12):
        record_cycle_started(
            state, f"later-{index}", f"later-request-{index}", None,
            retry_key="private-beta",
        )

    result = read_task_attempt_warning(state, now=now + timedelta(seconds=1))
    assert result["status"] == "warning"
    assert result["warning"] is True
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)
    assert "private-beta" not in json.dumps(result)


@pytest.mark.parametrize("terminal_order", ["failed-then-skip", "skip-then-failed"])
def test_task_warning_conflicting_cycle_terminal_outcomes_are_unknown(tmp_path, terminal_order):
    now = datetime.now(timezone.utc)
    state = tmp_path / terminal_order
    _append(state, "outcome", "prior-success", "private-alpha", "success", _iso_at(now - timedelta(hours=1)))

    for attempt in range(13):
        cycle = f"attempt-{attempt}"
        record_cycle_started(state, cycle, f"request-{attempt}", None, retry_key="private-alpha")
        if attempt == 0 and terminal_order == "failed-then-skip":
            record_cycle_outcome(state, cycle, "failed", "executor_failed", [], None, retry_key="private-alpha")
            record_cycle_outcome(state, cycle, "skipped-duplicate", "duplicate", [], None, retry_key="private-alpha")
        elif attempt == 0 and terminal_order == "skip-then-failed":
            record_cycle_outcome(state, cycle, "skipped-duplicate", "duplicate", [], None, retry_key="private-alpha")
            record_cycle_outcome(state, cycle, "failed", "executor_failed", [], None, retry_key="private-alpha")

    result = read_task_attempt_warning(state, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)


@pytest.mark.parametrize("start_count", [12, 13])
@pytest.mark.parametrize("skip_key", [None, "private-alpha"])
def test_task_warning_conflicting_key_reused_cycle_is_unknown(tmp_path, start_count, skip_key):
    now = datetime.now(timezone.utc)
    state = tmp_path / "conflicting-cycle-key"
    _append(state, "outcome", "prior-success", "private-alpha", "success", _iso_at(now - timedelta(hours=1)))
    for attempt in range(12):
        record_cycle_started(
            state, "reused-cycle", f"alpha-request-{attempt}", None,
            retry_key="private-alpha",
        )
    append_event(state, {
        "phase": "started", "cycle_id": "reused-cycle", "request_id": "beta-request",
        "retry_key": "private-beta", "ts": _iso_at(now),
    })

    result = read_task_attempt_warning(state, now=now + timedelta(seconds=1))
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)
    assert "private-beta" not in json.dumps(result)


def test_task_warning_excludes_cycles_ending_in_duplicate_skip(tmp_path):
    """ADR-039: cycles ending as duplicate skips do not count as attempts."""
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "duplicate-skips"
    _append(state, "outcome", "anchor", "private-alpha", "success", "2026-11-01T10:00:00Z")
    for index in range(12):
        cycle = f"skip-{index}"
        _append(state, "started", cycle, "private-alpha", ts=f"2026-11-01T11:{index:02d}:00Z")
        _append(state, "outcome", cycle, "private-alpha", "skipped-duplicate", f"2026-11-01T11:{index:02d}:30Z")

    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None


def test_task_warning_unparseable_success_breaks_streak_before_recount(tmp_path):
    """ADR-039: an unjoinable reset invalidates starts on both sides."""
    now = datetime.now(timezone.utc)
    state = tmp_path / "unknown-reset"
    _append(state, "outcome", "anchor", "private-alpha", "success", _iso_at(now - timedelta(hours=1)))
    for index in range(6):
        _append(state, "started", f"before-{index}", "private-alpha", ts=_iso_at(now - timedelta(minutes=55 - index)))
    _append(state, "outcome", "naive-success", "private-alpha", "success", (now - timedelta(minutes=30)).replace(tzinfo=None).isoformat())
    for index in range(6):
        _append(state, "started", f"after-{index}", "private-alpha", ts=_iso_at(now - timedelta(minutes=20 - index)))

    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None


def test_task_warning_malformed_phase_success_resets_to_unknown(tmp_path):
    """ADR-039: JSON-typed malformed phases with success data fail closed."""
    now = datetime.now(timezone.utc)
    malformed_phases = (None, [], {}, 7, True, "outcom")
    for index, malformed_phase in enumerate(malformed_phases):
        state = tmp_path / f"bad-phase-{index}"
        _append(state, "outcome", "anchor", "private-alpha", "success", _iso_at(now - timedelta(minutes=60)))
        for start_index in range(12):
            _append(state, "started", f"before-{start_index}", "private-alpha", ts=_iso_at(now - timedelta(minutes=50 - start_index)))
        row = {
            "cycle_id": "hidden-success",
            "retry_key": "private-alpha",
            "outcome": "success",
            "ts": _iso_at(now - timedelta(minutes=20)),
            "phase": malformed_phase,
        }
        if malformed_phase is None:
            row.pop("phase")
        with (state / "ledger" / "cycles.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        result = read_task_attempt_warning(state, now=now)
        assert result["status"] == "unknown"
        assert result["warning"] is None
        assert result["warning_tasks"] is None
        assert "private-alpha" not in json.dumps(result)


@pytest.mark.parametrize(
    ("field", "value", "phase", "outcome"),
    [
        ("phase", [], "unused", "success"),
        ("phase", {}, "unused", "success"),
        ("phase", 7, "unused", "success"),
        ("phase", True, "unused", "success"),
        ("outcome", [], "outcome", "unused"),
        ("outcome", {}, "outcome", "unused"),
        ("outcome", 7, "outcome", "unused"),
        ("outcome", True, "outcome", "unused"),
        ("retry_key", [], "started", None),
        ("retry_key", {}, "started", None),
        ("retry_key", 7, "started", None),
        ("retry_key", True, "started", None),
        ("retry_key", [], "outcome", "success"),
        ("cycle_id", [], "started", None),
        ("cycle_id", {}, "started", None),
        ("cycle_id", [], "outcome", "skipped-duplicate"),
        ("cycle_id", {}, "outcome", "skipped-duplicate"),
        ("cycle_id", [], "outcome", "success"),
        ("cycle_id", {}, "outcome", "success"),
        ("retry_key", 7, "outcome", "pushed_late"),
        ("ts", [], "outcome", "success"),
        ("ts", {}, "outcome", "success"),
        ("ts", 7, "outcome", "success"),
        ("ts", True, "outcome", "success"),
        ("timestamp", [], "outcome", "success"),
        ("timestamp", {}, "outcome", "success"),
        ("timestamp", 7, "outcome", "success"),
        ("timestamp", True, "outcome", "success"),
        ("retry_key", "", "started", None),
        ("cycle_id", "", "outcome", "skipped-duplicate"),
    ],
)
def test_task_warning_malformed_ledger_fields_fail_closed(tmp_path, field, value, phase, outcome):
    """ADR-039: malformed JSON field types cannot crash or forge H5 status."""
    now = datetime.now(timezone.utc)
    state = tmp_path / "malformed"
    _append(state, "outcome", "anchor", "private-alpha", "success", _iso_at(now - timedelta(hours=1)))
    for index in range(12):
        _append(state, "started", f"attempt-{index}", "private-alpha", ts=_iso_at(now - timedelta(minutes=50 - index)))
    row = {
        "phase": phase if phase != "unused" else "started",
        "cycle_id": "bad-row",
        "retry_key": "private-alpha",
        "outcome": outcome,
        "ts": _iso_at(now - timedelta(minutes=5)),
    }
    if field == "timestamp":
        row.pop("ts")
    row[field] = value
    if field == "phase" and value == "unused":
        row.pop(field)
    if field == "outcome" and outcome == "unused":
        row[field] = value
    with (state / "ledger" / "cycles.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None
    assert "private-alpha" not in json.dumps(result)


def test_task_warning_malformed_skip_cycle_id_cli_is_unknown(tmp_path, monkeypatch):
    """Malformed cycle identity must fail closed through the public CLI."""
    now = datetime.now(timezone.utc)
    state = tmp_path / "malformed-skip"
    _append(state, "outcome", "anchor", "private-alpha", "success", _iso_at(now - timedelta(hours=1)))
    for index in range(12):
        _append(state, "started", f"attempt-{index}", "private-alpha", ts=_iso_at(now - timedelta(minutes=50 - index)))
    row = {"phase": "outcome", "cycle_id": [], "outcome": "skipped-duplicate", "ts": _iso_at(now - timedelta(minutes=1))}
    with (state / "ledger" / "cycles.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    monkeypatch.setattr("nanobot.runtime.health.load_runtime_state_from_root", lambda *a, **k: {})
    monkeypatch.setattr("nanobot.runtime.health._ledger_age_seconds", lambda *_a, **_k: 0.0)
    monkeypatch.setattr("nanobot.runtime.health.read_cycle_progress", lambda *_a, **_k: {"state": "healthy", "alert": False})
    monkeypatch.setattr("nanobot.runtime.health.read_service_status", lambda *_a, **_k: {})
    monkeypatch.setattr("nanobot.runtime.health.read_failed_units_count", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health._promotion_readiness", lambda *_a, **_k: {"state": "ready"})
    monkeypatch.setattr("nanobot.runtime.health.read_autonomous_commits_24h", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_checkpoint_commits_24h", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_subagent_queue_depth", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_derived_priorities_queue", lambda *_a, **_k: {"depth": 0, "limit": 10})
    monkeypatch.setattr("nanobot.runtime.health._default_runner", lambda command: subprocess.CompletedProcess(command, 0, "", ""))
    result = CliRunner().invoke(app, ["cycle-health", "--runtime-state-root", str(state), "--json"])
    payload = json.loads(result.stdout)
    assert result.exit_code == payload["exit_code"]
    assert payload["task_attempt_warning"]["status"] == "unknown"
    assert payload["task_attempt_warning"]["warning"] is None
    assert "private-alpha" not in result.stdout


def test_task_warning_malformed_outcome_does_not_crash(tmp_path, monkeypatch):
    """ADR-039: malformed outcome values fail closed without crashing health."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr("nanobot.runtime.health.cycle_ledger_retention_days", lambda: 90)
    state = tmp_path / "malformed-outcome"
    _append(state, "outcome", "anchor", "private-alpha", "success", "2026-11-01T10:00:00Z")
    _append(state, "started", "attempt", "private-alpha", ts="2026-11-01T11:00:00Z")
    path = state / "ledger" / "cycles.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"phase": "outcome", "cycle_id": "bad", "retry_key": "private-alpha", "outcome": [], "ts": _iso_at(now - timedelta(minutes=5))}) + "\n")
    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None


def test_task_warning_uses_cycle_ledger_retention_not_other_reader_env(tmp_path, monkeypatch):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "state"
    _anchored_attempts(state, "private-alpha", 12)
    monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "7")
    monkeypatch.setenv("SELFEVO_LEDGER_RETENTION_DAYS", "1")
    result = read_task_attempt_warning(state, now=now)
    assert result["coverage_days"] == 7
    assert result["status"] == "warning"
    assert result["warning_tasks"] is None


def test_task_warning_unknown_when_retained_window_expires_or_is_malformed(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "1")
    state = tmp_path / "state"
    _append(state, "started", "old", "private-alpha", ts=_iso_at(now - timedelta(days=12)))
    assert read_task_attempt_warning(state, now=now)["status"] == "unknown"

    malformed = tmp_path / "malformed"
    _append(malformed, "outcome", "reset", "private-alpha", "success", "2026-11-01T11:00:00Z")
    with (malformed / "ledger" / "cycles.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    assert read_task_attempt_warning(malformed, now=now)["status"] == "unknown"


def test_cycle_health_summary_includes_warning_without_raw_key(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    state = tmp_path / "state"
    _append(state, "outcome", "reset", "very-private-key", "success", _iso_at(now - timedelta(hours=1)))
    for index in range(12):
        record_cycle_started(
            state, f"cycle-{index}", f"request-{index}", None,
            retry_key="very-private-key",
        )
    monkeypatch.setattr("nanobot.runtime.health.load_runtime_state_from_root", lambda *a, **k: {})
    monkeypatch.setattr("nanobot.runtime.health._ledger_age_seconds", lambda *_a, **_k: 0.0)
    monkeypatch.setattr("nanobot.runtime.health.read_cycle_progress", lambda *_a, **_k: {"state": "healthy", "alert": False})
    monkeypatch.setattr("nanobot.runtime.health.read_service_status", lambda *_a, **_k: {})
    monkeypatch.setattr("nanobot.runtime.health.read_failed_units_count", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health._promotion_readiness", lambda *_a, **_k: {"state": "ready"})
    monkeypatch.setattr("nanobot.runtime.health.read_autonomous_commits_24h", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_checkpoint_commits_24h", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_subagent_queue_depth", lambda *_a, **_k: 0)
    monkeypatch.setattr("nanobot.runtime.health.read_derived_priorities_queue", lambda *_a, **_k: {"depth": 0, "limit": 10})

    summary = build_cycle_health_summary(state)
    serialized = json.dumps(summary)
    assert summary["task_attempt_warning"]["status"] == "warning"
    assert "very-private-key" not in serialized
    assert "Task-attempt warning: warning:" in "\n".join(format_cycle_health_summary(summary))


def test_task_warning_unknown_reset_does_not_emit_under_threshold_count(tmp_path):
    """ADR-039: UNKNOWN reset cannot be reported as 11 known starts."""
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "state"
    _append(state, "outcome", "late", None, "pushed_late", "2026-11-01T10:00:00Z")
    for index in range(11):
        _append(state, "started", f"alpha-{index}", "private-alpha", ts=f"2026-11-01T11:{index:02d}:00Z")
    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "unknown"
    assert result["warning"] is None
    assert result["warning_tasks"] is None


def test_task_warning_proves_12_after_late_push_unknown_reset(tmp_path):
    """ADR-039: twelve starts after unknown late success prove warning only."""
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "state"
    _append(state, "outcome", "late", None, "pushed_late", "2026-11-01T10:00:00Z")
    for index in range(12):
        _append(state, "started", f"alpha-{index}", "private-alpha", ts=f"2026-11-01T11:{index:02d}:00Z")
    result = read_task_attempt_warning(state, now=now)
    assert result["status"] == "warning"
    assert result["warning"] is True
    assert result["warning_tasks"] is None
