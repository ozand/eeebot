from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from wsgiref.util import setup_testing_defaults

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops" / "dashboard" / "src"))

from nanobot_ops_dashboard.app import create_app  # noqa: E402
from nanobot_ops_dashboard.config import DashboardConfig  # noqa: E402
from nanobot_ops_dashboard.storage import init_db  # noqa: E402


def _call_json(app, path: str) -> dict:
    captured: dict[str, object] = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    environ: dict[str, object] = {}
    setup_testing_defaults(environ)
    environ["PATH_INFO"] = path
    body = b"".join(app(environ, start_response)).decode("utf-8")
    assert str(captured["status"]).startswith("200"), body
    return json.loads(body)


def test_ci_dashboard_subagent_gate_reconciles_blocked_materialized_result(tmp_path: Path) -> None:
    project_root = tmp_path / "dashboard"
    repo_root = tmp_path / "nanobot"
    db = tmp_path / "dashboard.sqlite3"
    init_db(db)
    state_root = repo_root / "workspace" / "state"
    request_dir = state_root / "subagents" / "requests"
    result_dir = state_root / "subagents" / "results"
    request_dir.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    request_path = request_dir / "request-cycle-ci.json"
    request_path.write_text(
        json.dumps({"schema_version": "subagent-request-v1", "request_status": "queued", "task_id": "same-task", "cycle_id": "cycle-ci"}),
        encoding="utf-8",
    )
    result_path = result_dir / "result-cycle-ci.json"
    result_path.write_text(
        json.dumps({"schema_version": "subagent-result-v1", "status": "blocked", "task_id": "same-task", "cycle_id": "cycle-ci", "request_path": str(request_path)}),
        encoding="utf-8",
    )
    old = time.time() - 3 * 3600
    os.utime(request_path, (old, old))
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host="eeepc", eeepc_ssh_key=tmp_path / "missing-key", eeepc_state_root="/state")

    payload = _call_json(create_app(cfg), "/api/subagents")

    assert payload["summary"]["state"] == "completed"
    assert payload["summary"]["stale_request_count"] == 0
    assert payload["summary"]["queued_request_count"] == 0
    assert payload["summary"]["blocked_result_count"] == 1
    assert payload["requests"][0]["status"] == "blocked"
    assert payload["requests"][0]["materialized_result_path"] == str(result_path)
