"""Deterministic model-free release activation and timeout recording (#1904)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nanobot.runtime import activation_check

REPO = Path(__file__).resolve().parents[1]


def _run_check(monkeypatch, tmp_path: Path, *, broken_import: bool = False, invalid_config: bool = False):
    state = tmp_path / "state"
    config = tmp_path / "config.json"
    config.write_text('{"agents":{"defaults":{"model":12}}}' if invalid_config else '{}', encoding="utf-8")
    monkeypatch.setenv("STATE_DIR", str(state))
    monkeypatch.setenv("NANOBOT_CONFIG_PATH", str(config))
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(state))
    monkeypatch.setenv("RELEASE_ROOT", str(REPO))
    monkeypatch.setenv("TARGET_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("SUBAGENT_BRIDGE_ENABLED", "1")
    if broken_import:
        monkeypatch.setattr(activation_check.importlib, "import_module", lambda _name: (_ for _ in ()).throw(ImportError("broken release import")))
    return state


def test_activation_self_check_passes_without_calling_any_model(monkeypatch, tmp_path):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("activation self-check must never call a model")

    monkeypatch.setattr("nanobot.providers.factory._make_provider", fail_if_called)
    monkeypatch.setattr("nanobot.agent.subagent.SubagentManager", fail_if_called)
    monkeypatch.setattr("nanobot.runtime.bridge._make_provider", fail_if_called)
    state = _run_check(monkeypatch, tmp_path)
    assert activation_check.run_activation_self_check() == 0
    rows = [json.loads(line) for line in (state / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["phase"] == "bridge_activation_self_check" and row["model_called"] is False for row in rows)
    marker_dir = state / "bridge" / "activation_checks"
    assert marker_dir.is_dir() and len(list(marker_dir.iterdir())) == 1


def test_broken_bridge_import_fails_self_check(monkeypatch, tmp_path):
    _run_check(monkeypatch, tmp_path, broken_import=True)
    with pytest.raises(ImportError, match="broken release import"):
        activation_check.run_activation_self_check()


def test_invalid_config_fails_self_check_instead_of_defaulting(monkeypatch, tmp_path):
    _run_check(monkeypatch, tmp_path, invalid_config=True)
    with pytest.raises(Exception):
        activation_check.run_activation_self_check()


def test_behavior_timeout_is_recorded_as_inconclusive(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("STATE_DIR", str(state))
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(state))
    monkeypatch.setenv("RELEASE_ROOT", str(REPO))
    monkeypatch.setenv("NANOBOT_CONFIG_PATH", str(tmp_path / "config.json"))
    activation_check.record_behavior_timeout(result="timeout", status="15", release="/release/new")
    rows = [json.loads(line) for line in (state / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["phase"] == "deploy_behavioral_check"
    assert rows[-1]["outcome"] == "inconclusive"
    assert rows[-1]["behavior_confirmed"] is False
    marker = next((state / "bridge" / "behavioral_checks").glob("timeout-*.json"))
    assert json.loads(marker.read_text(encoding="utf-8"))["outcome"] == "inconclusive"


def test_real_cycle_non_timeout_failure_classifies_failed():
    lib = REPO / "host/eeepc/scripts/lib_bridge_exit.sh"
    result = subprocess.run(["bash", "-c", f'. "{lib}"; classify_bridge_run 1 exit-code 1'], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "failed"


def test_timeout_classification_takes_precedence_over_failed_restart_rc():
    lib = REPO / "host/eeepc/scripts/lib_bridge_exit.sh"
    result = subprocess.run(["bash", "-c", f'. "{lib}"; classify_bridge_run 1 timeout 15'], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "timeout"


def test_real_cycle_timeout_has_distinct_nonrollback_classification():
    lib = REPO / "host/eeepc/scripts/lib_bridge_exit.sh"
    result = subprocess.run(["bash", "-c", f'. "{lib}"; classify_bridge_run 1 timeout 15'], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "timeout"
