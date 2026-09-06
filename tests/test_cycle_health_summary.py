import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.runtime.health import (
    build_cycle_health_summary,
    format_cycle_health_summary,
    read_autonomous_commits_24h,
    read_cycle_progress,
    read_subagent_queue_depth,
)


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fake_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    if command[:3] == ["systemctl", "is-active", "eeepc-self-evolving-subagent-bridge.service"]:
        return subprocess.CompletedProcess(command, 0, "active\n", "")
    if command[:3] == ["systemctl", "show", "eeepc-self-evolving-subagent-bridge.service"]:
        return subprocess.CompletedProcess(command, 0, "Result=success\nActiveState=active\nSubState=exited\n", "")
    if command[:2] == ["systemctl", "--failed"]:
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.CompletedProcess(command, 1, "", "unexpected")


def _write_ledger(state: Path, cycle_id: str) -> None:
    """#1222: the cycle ledger is the loop's heartbeat — latest cycle id and
    staleness come from it, not from the coordinator's reports/evolution-*.json."""
    path = state / "ledger" / "cycles.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"phase": "proposed", "cycle_id": cycle_id, "task_title": "t"}) + "\n"
        + json.dumps({"phase": "outcome", "cycle_id": cycle_id, "outcome": "success", "ts": "2026-09-03T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )


def test_build_cycle_health_summary_reads_state_and_systemd(tmp_path: Path):
    state = tmp_path / "state"
    _write_ledger(state, "cycle-001")
    _write_json(
        state / "subagents" / "telemetry-001.json",
        {"subagent_id": "sub-001", "status": "ok"},
    )
    _write_json(
        state / "promotions" / "latest.json",
        {"promotion_candidate_id": "promo-001", "review_status": "ready_for_policy_review", "decision": "ready_for_policy_review"},
    )
    # A frozen coordinator report is not a source (#1222).
    _write_json(state / "reports" / "evolution-001.json", {"cycle_id": "cycle-frozen", "result_status": "PASS"})

    summary = build_cycle_health_summary(state, runner=_fake_runner)

    assert summary["schema_version"] == "cycle-health-summary-v2"
    assert summary["latest_cycle_id"] == "cycle-001"
    assert isinstance(summary["ledger_age_seconds"], float)
    assert "latest_report_path" not in summary
    assert summary["latest_subagent_telemetry_id"] == "sub-001"
    assert summary["service_status"]["active_state"] == "active"
    assert summary["failed_units_count"] == 0
    assert summary["promotion_readiness"]["state"] == "ready_for_policy_review"
    assert summary["severity"] == "ok"
    assert summary["exit_code"] == 0
    assert summary["next_recommended_action"] == "review_promotion_candidate"

    lines = format_cycle_health_summary(summary)
    assert any("Latest cycle id: cycle-001" in line for line in lines)
    assert any("Next recommended action: review_promotion_candidate" in line for line in lines)


def test_cycle_health_cli_json(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    _write_ledger(state, "cycle-cli")
    _write_json(
        state / "promotions" / "latest.json",
        {"promotion_candidate_id": "promo-cli", "review_status": "ready_for_policy_review", "decision": "ready_for_policy_review"},
    )
    monkeypatch.setattr("nanobot.runtime.health._default_runner", _fake_runner)

    result = CliRunner().invoke(
        app,
        ["cycle-health", "--runtime-state-root", str(state), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["latest_cycle_id"] == "cycle-cli"
    assert payload["failed_units_count"] == 0
    assert "success_signals" in payload
    assert "autonomous_commits_24h" in payload["success_signals"]
    assert "subagent_queue_depth" in payload["success_signals"]


def _init_git_repo_with_commit(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    env_cmds = [
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ]
    for cmd in env_cmds:
        subprocess.run(cmd, cwd=repo_dir, check=True, capture_output=True, text=True)
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def test_read_autonomous_commits_24h_counts_recent_commit(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    selfevo_repo = tmp_path / "eeebot-self-evolving"
    _init_git_repo_with_commit(selfevo_repo)

    assert read_autonomous_commits_24h(state) == 1


def test_read_autonomous_commits_24h_missing_repo_returns_none(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()

    assert read_autonomous_commits_24h(state) is None


def test_read_subagent_queue_depth_counts_json_files(tmp_path: Path):
    state = tmp_path / "state"
    requests_dir = state / "subagents" / "requests"
    requests_dir.mkdir(parents=True)
    for i in range(3):
        (requests_dir / f"req-{i}.json").write_text("{}", encoding="utf-8")
    (requests_dir / "not-json.txt").write_text("ignore me", encoding="utf-8")

    assert read_subagent_queue_depth(state) == 3


def test_read_subagent_queue_depth_missing_dir_returns_zero(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()

    assert read_subagent_queue_depth(state) == 0


def _write_outcomes(state: Path, rows: list[dict]) -> None:
    path = state / "ledger" / "cycles.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_cycle_progress_26_failures_trips_and_reports_dominant_reason(tmp_path: Path):
    state = tmp_path / "state"
    now = 1_000_000.0
    rows = [
        {"phase": "outcome", "cycle_id": f"c-{i}", "outcome": "failed", "reason": "dirty_tree", "ts": f"2026-09-06T{6 + (i // 15):02d}:{(i % 15) * 4:02d}:00Z"}
        for i in range(26)
    ]
    _write_outcomes(state, rows)
    progress = read_cycle_progress(state, now=now)
    assert progress["state"] == "stalled"
    assert progress["alert"] is True
    assert progress["consecutive_non_integrating_cycles"] == 26
    assert progress["dominant_reason"] == "dirty_tree"
    assert progress["threshold_cycles"] == 15
    assert progress["threshold_hours"] == 1.0


def test_cycle_progress_empty_missing_and_malformed_are_unavailable(tmp_path: Path):
    for variant in ("missing", "empty", "malformed"):
        state = tmp_path / variant
        ledger = state / "ledger" / "cycles.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        if variant == "empty":
            ledger.write_text("", encoding="utf-8")
        elif variant == "malformed":
            ledger.write_text("not json\n", encoding="utf-8")
        progress = read_cycle_progress(state, now=1_000_000.0)
        assert progress["state"] == "unavailable", (variant, progress)
        assert progress["alert"] is None, (variant, progress)
        assert progress["hours_since_last_success"] is None, (variant, progress)
        assert progress["consecutive_non_integrating_cycles"] is None, (variant, progress)


def test_cycle_progress_no_success_is_distinct_and_interleaved_success_is_healthy(tmp_path: Path):
    no_success = tmp_path / "no-success"
    _write_outcomes(no_success, [
        {"phase": "outcome", "cycle_id": "c1", "outcome": "failed", "reason": "x", "ts": "2026-09-06T10:00:00Z"},
    ])
    fresh = read_cycle_progress(no_success, now=1788690000.0)
    assert fresh["state"] == "no_success_yet"
    assert fresh["alert"] is False
    assert fresh["hours_since_last_success"] is None
    assert fresh["consecutive_non_integrating_cycles"] == 1

    interleaved = tmp_path / "interleaved"
    _write_outcomes(interleaved, [
        {"phase": "outcome", "cycle_id": "c1", "outcome": "failed", "reason": "old", "ts": "2026-09-06T10:00:00Z"},
        {"phase": "outcome", "cycle_id": "c2", "outcome": "success", "ts": "2026-09-06T10:04:00Z"},
        {"phase": "outcome", "cycle_id": "c3", "outcome": "failed", "reason": "new", "ts": "2026-09-06T10:08:00Z"},
    ])
    progress = read_cycle_progress(interleaved, now=1788690000.0)
    assert progress["state"] == "healthy"
    assert progress["alert"] is False
    assert progress["consecutive_non_integrating_cycles"] == 1
    assert progress["dominant_reason"] == "new"


def test_build_cycle_health_summary_includes_success_signals(tmp_path: Path):
    state = tmp_path / "state"
    _write_ledger(state, "cycle-001")
    requests_dir = state / "subagents" / "requests"
    requests_dir.mkdir(parents=True)
    (requests_dir / "req-0.json").write_text("{}", encoding="utf-8")

    summary = build_cycle_health_summary(state, runner=_fake_runner)

    assert summary["success_signals"]["autonomous_commits_24h"] is None
    assert summary["success_signals"]["subagent_queue_depth"] == 1

    lines = format_cycle_health_summary(summary)
    assert any("Success signals:" in line for line in lines)
