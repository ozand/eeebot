from __future__ import annotations

import json
import subprocess
from pathlib import Path
import pytest

from nanobot.runtime import doctor


def _fixture(tmp_path: Path) -> dict[str, Path]:
    state = tmp_path / "state"
    (state / "ledger").mkdir(parents=True)
    (state / "reflector").mkdir()
    (state / "skill_evals").mkdir()
    (state / "knowledge_lift").mkdir()
    (state / "completed").mkdir()
    (state / "demand").mkdir()
    (state / "ledger" / "cycles.jsonl").write_text(
        json.dumps({"phase": "outcome", "ts": "2026-08-31T12:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    for rel in (
        "reflector/watermark.json",
        "skill_evals/watermark.json",
        "knowledge_lift/watermark.json",
    ):
        (state / rel).write_text(
            json.dumps({"last_run_utc": "2026-08-31T11:59:00Z"}), encoding="utf-8"
        )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    release = tmp_path / "releases" / "20260831T120000Z-test"
    release.mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(release, target_is_directory=True)
    return {"state": state, "repo": repo, "current": current}


def _runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    joined = " ".join(command)
    if "systemctl show" in joined and "Environment" in joined:
        return subprocess.CompletedProcess(command, 0, "Environment=SUBAGENT_BRIDGE_MODEL=x SUBAGENT_BRIDGE_MAX_REVISIONS=x SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN=x\n", "")
    if "systemctl show" in joined and "Result" in joined:
        return subprocess.CompletedProcess(command, 0, "Result=success\nExecMainStatus=0\nExecMainExitTimestamp=now\n", "")
    if "is-enabled" in joined or "is-active" in joined:
        return subprocess.CompletedProcess(command, 0, "active\n", "")
    if "branch --show-current" in joined:
        return subprocess.CompletedProcess(command, 0, "main\n", "")
    if "status --porcelain" in joined or "remote get-url" in joined:
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.CompletedProcess(command, 0, "", "")


def test_healthy_fixture_returns_zero_and_covers_all_areas(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    import nanobot.runtime.doctor as doctor_module
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(doctor_module, "file_owner", lambda path: "eeepc-agent")
    result = doctor.run_doctor(
        state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"],
        command_runner=_runner,
        now=doctor.datetime.fromisoformat("2026-08-31T12:00:00+00:00"),
        environment={"SUBAGENT_BRIDGE_MODEL": "hidden", "SUBAGENT_BRIDGE_MAX_REVISIONS": "hidden", "SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN": "hidden"},
    )
    assert result.exit_code == 0
    assert [check.name for check in result.checks] == [
        "timers", "release", "ownership", "watermarks", "integrity", "environment", "repository"
    ]
    assert all(check.status == "PASS" for check in result.checks)
    monkeypatch.undo()


def test_root_owned_state_file_is_fail(tmp_path: Path, monkeypatch) -> None:
    paths = _fixture(tmp_path)
    bad = paths["state"] / "root-owned.json"
    bad.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "file_owner", lambda _: "root" if _ == bad else "eeepc-agent")
    result = doctor.run_doctor(state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"], command_runner=_runner)
    ownership = next(check for check in result.checks if check.name == "ownership")
    assert result.exit_code == 2
    assert ownership.status == "FAIL"
    assert "root-owned.json" in ownership.reason


def test_stale_watermark_is_warn(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    import nanobot.runtime.doctor as doctor_module
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(doctor_module, "file_owner", lambda path: "eeepc-agent")
    (paths["state"] / "reflector" / "watermark.json").write_text(
        json.dumps({"last_run_utc": "2026-08-29T00:00:00Z"}), encoding="utf-8"
    )
    result = doctor.run_doctor(
        state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"],
        command_runner=_runner, now=doctor.datetime.fromisoformat("2026-08-31T12:00:00+00:00"),
        environment={"SUBAGENT_BRIDGE_MODEL": "x", "SUBAGENT_BRIDGE_MAX_REVISIONS": "x", "SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN": "x"},
    )
    assert next(check for check in result.checks if check.name == "watermarks").status == "WARN"
    assert result.exit_code == 1
    monkeypatch.undo()


def test_watermark_without_last_run_is_valid_when_present(tmp_path: Path, monkeypatch) -> None:
    paths = _fixture(tmp_path)
    import nanobot.runtime.doctor as doctor_module
    monkeypatch.setattr(doctor_module, "file_owner", lambda path: "eeepc-agent")
    (paths["state"] / "reflector" / "watermark.json").write_text(json.dumps({"watermark": "abc"}), encoding="utf-8")
    result = doctor.run_doctor(state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"], command_runner=_runner, environment={"SUBAGENT_BRIDGE_MODEL":"x","SUBAGENT_BRIDGE_MAX_REVISIONS":"x","SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN":"x"})
    assert "reflector/watermark.json missing or invalid" not in next(c for c in result.checks if c.name == "watermarks").reason


def test_malformed_json_rows_are_counted_per_file(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    (paths["state"] / "ledger" / "cycles.jsonl").write_text("not-json\n{}\n", encoding="utf-8")
    (paths["state"] / "completed" / "completed.json").write_text("{bad", encoding="utf-8")
    result = doctor.run_doctor(state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"], command_runner=_runner)
    integrity = next(check for check in result.checks if check.name == "integrity")
    assert integrity.status == "WARN"
    assert "cycles.jsonl: 1" in integrity.reason
    assert "completed.json: 1" in integrity.reason


def test_missing_release_symlink_is_fail(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths["current"].unlink()
    result = doctor.run_doctor(state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"], command_runner=_runner)
    release = next(check for check in result.checks if check.name == "release")
    assert release.status == "FAIL"
    assert result.exit_code == 2


def test_json_output_contains_same_contract_and_never_env_values(tmp_path: Path, capsys) -> None:
    paths = _fixture(tmp_path)
    code = doctor.main([
        "--state-dir", str(paths["state"]), "--repo-dir", str(paths["repo"]),
        "--release-link", str(paths["current"]), "--json",
    ], command_runner=_runner, environment={"SUBAGENT_BRIDGE_MODEL": "SECRET-VALUE"})
    output = capsys.readouterr().out
    assert code in (0, 1, 2)
    payload = json.loads(output)
    assert payload["exit_code"] == code
    assert "SECRET-VALUE" not in output
    assert len(payload["checks"]) == 7


def test_environment_file_names_are_checked_without_reading_values(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    env_file = tmp_path / "bridge.env"
    env_file.write_text("SUBAGENT_BRIDGE_MODEL=secret-value\n", encoding="utf-8")

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["systemctl", "show", "eeepc-self-evolving-subagent-bridge.service"]:
            if "-p" in command and "EnvironmentFiles" in command:
                return subprocess.CompletedProcess(command, 0, f"EnvironmentFiles={env_file}\n", "")
            if "-p" in command and "Environment" in command:
                return subprocess.CompletedProcess(command, 0, "Environment=\n", "")
        return _runner(command, **kwargs)

    result = doctor.run_doctor(
        state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"],
        command_runner=runner, environment={},
    )
    environment = next(check for check in result.checks if check.name == "environment")
    assert "SUBAGENT_BRIDGE_MODEL" not in environment.reason
    assert "secret-value" not in environment.reason


def test_unreadable_environment_file_is_reported_without_failing(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    missing = tmp_path / "missing.env"

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["systemctl", "show", "eeepc-self-evolving-subagent-bridge.service"]:
            if "-p" in command and "EnvironmentFiles" in command:
                return subprocess.CompletedProcess(command, 0, f"EnvironmentFiles=-{missing}\n", "")
            if "-p" in command and "Environment" in command:
                return subprocess.CompletedProcess(command, 0, "Environment=\n", "")
        return _runner(command, **kwargs)

    result = doctor.run_doctor(
        state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"],
        command_runner=runner, environment={},
    )
    environment = next(check for check in result.checks if check.name == "environment")
    assert "skipped (unreadable)" in environment.reason


def test_mid_cycle_branch_with_fresh_bridge_activity_passes(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    now = doctor.datetime.fromisoformat("2026-08-31T12:00:00+00:00")
    (paths["state"] / "ledger" / "cycles.jsonl").write_text(
        json.dumps({"phase": "started", "cycle_id": "cycle-abc", "ts": "2026-08-31T11:59:00Z"}) + "\n",
        encoding="utf-8",
    )

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "branch --show-current" in " ".join(command):
            return subprocess.CompletedProcess(command, 0, "selfevo/cycle-cycle-abc\n", "")
        return _runner(command, **kwargs)

    result = doctor.run_doctor(
        state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"],
        command_runner=runner, now=now,
        environment={"SUBAGENT_BRIDGE_MODEL": "x"},
    )
    repository = next(check for check in result.checks if check.name == "repository")
    assert repository.status == "PASS"
    assert repository.reason == "mid-cycle branch selfevo/cycle-cycle-abc"


def test_stale_mid_cycle_branch_fails(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    now = doctor.datetime.fromisoformat("2026-08-31T12:00:00+00:00")
    (paths["state"] / "ledger" / "cycles.jsonl").write_text(
        json.dumps({"phase": "started", "cycle_id": "cycle-old", "ts": "2026-08-30T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "branch --show-current" in " ".join(command):
            return subprocess.CompletedProcess(command, 0, "selfevo/cycle-cycle-old\n", "")
        return _runner(command, **kwargs)

    result = doctor.run_doctor(
        state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"],
        command_runner=runner, now=now,
        environment={"SUBAGENT_BRIDGE_MODEL": "x"},
    )
    repository = next(check for check in result.checks if check.name == "repository")
    assert repository.status == "FAIL"
    assert "selfevo/cycle-cycle-old" in repository.reason


def test_litellm_env_is_not_opened(tmp_path: Path, monkeypatch) -> None:
    paths = _fixture(tmp_path)
    doctor.run_doctor(state_dir=paths["state"], repo_dir=paths["repo"], release_link=paths["current"], command_runner=_runner)
    assert not (paths["state"] / "litellm.env").exists()
