import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.runtime.health import build_cycle_health_summary, format_cycle_health_summary


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


def test_build_cycle_health_summary_reads_state_and_systemd(tmp_path: Path):
    state = tmp_path / "state"
    _write_json(
        state / "reports" / "evolution-001.json",
        {"cycle_id": "cycle-001", "result_status": "PASS"},
    )
    _write_json(
        state / "subagents" / "telemetry-001.json",
        {"subagent_id": "sub-001", "status": "ok"},
    )
    _write_json(
        state / "promotions" / "latest.json",
        {"promotion_candidate_id": "promo-001", "review_status": "ready_for_policy_review", "decision": "ready_for_policy_review"},
    )
    _write_json(state / "outbox" / "latest.json", {"approval_gate": {"state": "fresh"}})

    summary = build_cycle_health_summary(state, runner=_fake_runner)

    assert summary["schema_version"] == "cycle-health-summary-v1"
    assert summary["latest_cycle_id"] == "cycle-001"
    assert summary["latest_subagent_telemetry_id"] == "sub-001"
    assert summary["service_status"]["active_state"] == "active"
    assert summary["failed_units_count"] == 0
    assert summary["promotion_readiness"]["state"] == "ready_for_policy_review"
    assert summary["next_recommended_action"] == "review_promotion_candidate"

    lines = format_cycle_health_summary(summary)
    assert any("Latest cycle id: cycle-001" in line for line in lines)
    assert any("Next recommended action: review_promotion_candidate" in line for line in lines)


def test_cycle_health_cli_json(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    _write_json(state / "reports" / "evolution-001.json", {"cycle_id": "cycle-cli"})
    monkeypatch.setattr("nanobot.runtime.health._default_runner", _fake_runner)

    result = CliRunner().invoke(
        app,
        ["cycle-health", "--runtime-state-root", str(state), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["latest_cycle_id"] == "cycle-cli"
    assert payload["failed_units_count"] == 0
