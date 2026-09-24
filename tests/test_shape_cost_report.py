"""Tests for scripts/shape_cost_report.py (#1795).

Verifies offline cost reporting by task shape across executor calls,
iterations, wall-clock duration, integration/rollback shares, and estimate pairs.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.shape_cost_report import (
    analyze_shape_costs,
    format_report,
)

# 3-5 real host row shapes (sanitized, zero private fields)
FIXTURE_PROPOSED = {
    "phase": "proposed",
    "cycle_id": "cycle-0c4e2292dce6",
    "task_title": "Add result ID lookup to scripts/search_subagent_archive.py",
    "target_path": "scripts/search_subagent_archive.py",
    "demand_id": "defect-ee26c0384b2a",
    "ts": "2026-09-23T23:27:50.627916Z",
}
FIXTURE_STARTED = {
    "phase": "started",
    "cycle_id": "cycle-0c4e2292dce6",
    "ts": "2026-09-23T23:31:23.463870Z",
}
FIXTURE_OUTCOME = {
    "phase": "outcome",
    "cycle_id": "cycle-0c4e2292dce6",
    "outcome": "success",
    "iterations_used": 6,
    "change_shape": "unclassified",
    "ts": "2026-09-24T00:05:17.201586Z",
}
FIXTURE_LLM_CALL = {
    "cycle_id": "cycle-0c4e2292dce6",
    "component": "executor",
    "ts": "2026-09-24T00:00:22.958127Z",
    "duration_ms": 238461.0,
    "prompt_tokens": 58729,
    "completion_tokens": 1791,
    "total_tokens": 60520,
}
FIXTURE_RESULT = {
    "cycle_id": "cycle-0c4e2292dce6",
    "status": "completed",
    "rollback": {
        "integrated": True,
        "main_sha_before": "b107af04bb41e1e344ccd4aa133c48426c6b72cd",
        "main_sha_after": "fcd02f193b7f93b6f2ae0d568d579001f13e5228",
    },
}


def _seed_state(state_dir: Path, count: int = 1) -> None:
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    llm_dir = state_dir / "llm_calls"
    llm_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = state_dir / "subagents" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    with (ledger_dir / "cycles.jsonl").open("w", encoding="utf-8") as f:
        for i in range(count):
            cid = f"cycle-{i:04d}" if count > 1 else FIXTURE_PROPOSED["cycle_id"]
            p = dict(FIXTURE_PROPOSED, cycle_id=cid)
            s = dict(FIXTURE_STARTED, cycle_id=cid)
            o = dict(FIXTURE_OUTCOME, cycle_id=cid)
            f.write(json.dumps(p) + "\n" + json.dumps(s) + "\n" + json.dumps(o) + "\n")

    with (llm_dir / "2026-09-24.jsonl").open("w", encoding="utf-8") as f:
        for i in range(count):
            cid = f"cycle-{i:04d}" if count > 1 else FIXTURE_LLM_CALL["cycle_id"]
            call = dict(FIXTURE_LLM_CALL, cycle_id=cid)
            f.write(json.dumps(call) + "\n")

    for i in range(count):
        cid = f"cycle-{i:04d}" if count > 1 else FIXTURE_RESULT["cycle_id"]
        res = dict(FIXTURE_RESULT, cycle_id=cid)
        (archive_dir / f"result-{cid}.json").write_text(json.dumps(res), encoding="utf-8")


def test_shape_cost_report_from_fixtures(tmp_path: Path):
    state_dir = tmp_path / "state"
    _seed_state(state_dir, count=1)

    report = analyze_shape_costs(state_dir, days=7)
    assert report["provenance"] == "harness_measured_state"

    shape_info = report["shapes"]["extend_existing_script"]
    assert shape_info["n"] == 1
    # n < 5 is marked as low history
    assert shape_info["status"] == "мало истории (n < 5)"
    assert shape_info["distributions"] is None


def test_shape_cost_report_sufficient_history(tmp_path: Path):
    state_dir = tmp_path / "state"
    _seed_state(state_dir, count=6)

    report = analyze_shape_costs(state_dir, days=7)
    shape_info = report["shapes"]["extend_existing_script"]
    assert shape_info["n"] == 6
    assert shape_info["status"] == "достаточно истории"

    dists = shape_info["distributions"]
    assert dists is not None
    assert dists["executor_calls"]["median"] == 1.0
    assert dists["iterations_used"]["median"] == 6.0
    assert dists["wall_clock_s"]["median"] > 0

    rates = shape_info["rates"]
    assert rates["integrated_share"] == 1.0
    assert rates["rollback_share"] == 0.0

    ests = shape_info["estimates"]
    assert ests["missing"] == 6
    assert ests["pairs"] == []


def test_inputs_are_measurements_no_model_calls(monkeypatch, tmp_path: Path):
    """AC: A test proves the audit's inputs are measurements and that no path
    re-scores a task description with a model.
    """
    def _forbidden_llm(*args, **kwargs):
        raise AssertionError("Model invocation is forbidden in offline measurement audit!")

    monkeypatch.setattr("subprocess.Popen", _forbidden_llm)
    monkeypatch.setattr("subprocess.run", _forbidden_llm)

    state_dir = tmp_path / "state"
    _seed_state(state_dir, count=5)

    # analyze_shape_costs must complete purely by reading local filesystem state
    report = analyze_shape_costs(state_dir, days=7)
    assert report["provenance"] == "harness_measured_state"

    # Verify no graph-derived figures (e.g. ArtifactGraph rungs) are mixed into the report
    for shape, info in report["shapes"].items():
        if info["distributions"]:
            assert "rung" not in info["distributions"]
            assert "value" not in info["distributions"]


def test_format_report_human_readable(tmp_path: Path):
    state_dir = tmp_path / "state"
    _seed_state(state_dir, count=5)

    report = analyze_shape_costs(state_dir, days=7)
    text = format_report(report, days=7)
    assert "Отчет о стоимости по формам задач" in text
    assert "extend_existing_script" in text
    assert "Executor-вызовы:" in text
    assert "Iterations used:" in text
    assert "Wall clock" in text
