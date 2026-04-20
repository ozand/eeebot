from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.main import main as service_main


def _write_fresh_approval_gate(state_root: Path) -> None:
    approvals_dir = state_root / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    expires_at_utc = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at_utc}, indent=2),
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_service_entrypoint_routes_through_writer_lane(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "authority"
    workspace.mkdir(parents=True)
    _write_fresh_approval_gate(state_root)

    monkeypatch.setenv("NANOBOT_WORKSPACE", str(workspace))
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(state_root))
    monkeypatch.delenv("NANOBOT_RUNTIME_STATE_SOURCE", raising=False)
    monkeypatch.setenv("NANOBOT_SELF_EVOLVING_TASKS", "Verify the writer lane path.")

    exit_code = service_main()

    assert exit_code == 0

    current_path = state_root / "goals" / "current.json"
    active_path = state_root / "goals" / "active.json"
    history_dir = state_root / "goals" / "history"
    history_files = sorted(history_dir.glob("cycle-*.json"))

    assert current_path.exists()
    assert active_path.exists()
    assert history_files

    current = _read_json(current_path)
    active = _read_json(active_path)
    history = _read_json(history_files[-1])

    assert current["schema_version"] == "task-plan-v1"
    assert current["current_task_id"] == "record-reward"
    assert current["reward_signal"]["value"] == 1.0
    assert current["history_path"] == str(history_files[-1])
    assert active["active_goal"] == "goal-bootstrap"
    assert history["schema_version"] == "task-history-v1"
    assert history["current_task_id"] == "record-reward"
    assert history["reward_signal"]["value"] == 1.0
