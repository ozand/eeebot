"""Per-task H5 warning evidence contract (ADR-039 / #2143)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.runtime.bridge import _retry_key_for
from nanobot.runtime.cycle_ledger import record_cycle_started
from nanobot.runtime.health import (
    build_cycle_health_summary,
    format_cycle_health_summary,
    read_task_attempt_warning,
)


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


def _anchored_attempts(state, key, count):
    _append(state, "outcome", f"reset-{key}", key, "success", "2026-11-01T11:00:00Z")
    for index in range(count):
        _append(state, "started", f"{key}-{index}", key, ts=f"2026-11-01T11:{index + 1:02d}:00Z")


def test_task_warning_threshold_exact_key_success_reset_and_privacy(tmp_path):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "state"
    _anchored_attempts(state, "private-alpha", 11)
    clear = read_task_attempt_warning(state, now=now)
    assert clear["status"] == "clear"
    assert clear["warning"] is False
    assert clear["warning_tasks"] == 0
    assert format_cycle_health_summary({"task_attempt_warning": clear})[7] == (
        "  Task-attempt warning: clear: 0 task warnings (coverage complete)"
    )
    _append(state, "started", "alpha-11", "private-alpha", ts="2026-11-01T11:12:00Z")
    at_twelve = read_task_attempt_warning(state, now=now)
    assert at_twelve["status"] == "warning"
    assert at_twelve["warning"] is True
    assert at_twelve["warning_tasks"] == 1
    _append(state, "started", "alpha-12", "private-alpha", ts="2026-11-01T11:13:00Z")
    at_thirteen = read_task_attempt_warning(state, now=now)
    assert at_thirteen["status"] == "warning"
    assert at_thirteen["warning_tasks"] == 1

    interleaved = tmp_path / "interleaved"
    _anchored_attempts(interleaved, "private-alpha", 11)
    _append(interleaved, "outcome", "beta-reset", "private-beta", "success", "2026-11-01T11:19:00Z")
    for index in range(11):
        _append(interleaved, "started", f"beta-{index}", "private-beta", ts=f"2026-11-01T11:{20 + index:02d}:00Z")
    before_beta_twelve = read_task_attempt_warning(interleaved, now=now)
    assert before_beta_twelve["status"] == "clear"
    _append(interleaved, "started", "beta-11", "private-beta", ts="2026-11-01T11:31:00Z")
    beta_twelve = read_task_attempt_warning(interleaved, now=now)
    assert beta_twelve["status"] == "warning"
    assert beta_twelve["warning_tasks"] == 1

    reset = tmp_path / "reset"
    _anchored_attempts(reset, "private-alpha", 12)
    _append(reset, "outcome", "alpha-success", "private-alpha", "success", "2026-11-01T11:22:00Z")
    after_success = read_task_attempt_warning(reset, now=now)
    assert after_success["status"] == "clear"
    assert after_success["warning_tasks"] == 0

    result = read_task_attempt_warning(state, now=now)
    assert result == {
        "threshold": 12,
        "coverage_days": 90,
        "status": "warning",
        "coverage": "complete",
        "warning": True,
        "warning_tasks": 1,
    }
    assert "private-alpha" not in json.dumps(result)
    assert "private-beta" not in json.dumps(result)


def test_self_title_edit_creates_distinct_warning_identity():
    first = _retry_key_for(None, "Review ADR design")
    edited = _retry_key_for(None, "Review ADR design revised")
    assert first != edited


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
    state = tmp_path / "state"
    _append(state, "outcome", "reset", "private-clear", "success", "2026-11-01T10:00:00Z")
    _append(state, "started", "one", "private-clear", ts="2026-11-01T11:00:00Z")
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
    assert "Task-attempt warning: clear: 0 task warnings (coverage complete)" in clear.stdout
    clear_json = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(state), "--json"])
    clear_payload = json.loads(clear_json.stdout)
    assert clear_json.exit_code == clear_payload["exit_code"]
    assert clear_payload["task_attempt_warning"]["status"] == "clear"
    assert clear_payload["task_attempt_warning"]["warning"] is False

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
    _append(warning, "outcome", "reset", "private-warning", "success", "2026-11-01T10:00:00Z")
    for index in range(12):
        _append(warning, "started", f"attempt-{index}", "private-warning", ts=f"2026-11-01T11:{index:02d}:00Z")
    warning_text = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(warning)])
    assert "Task-attempt warning: warning: at least one task has 12 evidenced starts" in warning_text.stdout
    assert "private-warning" not in warning_text.stdout
    result = runner.invoke(app, ["cycle-health", "--runtime-state-root", str(warning), "--json"])
    warning_payload = json.loads(result.stdout)
    assert result.exit_code == warning_payload["exit_code"]
    assert warning_payload["task_attempt_warning"]["status"] == "warning"
    assert warning_payload["task_attempt_warning"]["warning"] is True
    assert "private-warning" not in result.stdout


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


def test_task_warning_uses_cycle_ledger_retention_not_other_reader_env(tmp_path, monkeypatch):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    state = tmp_path / "state"
    _anchored_attempts(state, "private-alpha", 12)
    monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "7")
    monkeypatch.setenv("SELFEVO_LEDGER_RETENTION_DAYS", "1")
    result = read_task_attempt_warning(state, now=now)
    assert result["coverage_days"] == 7
    assert result["status"] == "warning"
    assert result["warning_tasks"] == 1


def test_task_warning_unknown_when_retained_window_expires_or_is_malformed(tmp_path, monkeypatch):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "1")
    state = tmp_path / "state"
    _append(state, "started", "old", "private-alpha", ts="2026-10-20T11:00:00Z")
    assert read_task_attempt_warning(state, now=now)["status"] == "unknown"

    malformed = tmp_path / "malformed"
    _append(malformed, "outcome", "reset", "private-alpha", "success", "2026-11-01T11:00:00Z")
    with (malformed / "ledger" / "cycles.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    assert read_task_attempt_warning(malformed, now=now)["status"] == "unknown"


def test_cycle_health_summary_includes_warning_without_raw_key(tmp_path, monkeypatch):
    state = tmp_path / "state"
    _append(state, "outcome", "reset", "very-private-key", "success", "2026-11-01T10:00:00Z")
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
