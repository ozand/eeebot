from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.experiment_ledger import (
    COLUMNS,
    append_experiment_result,
    ledger_path,
    read_experiment_ledger,
)


def test_missing_ledger_is_explicit_and_empty(tmp_path: Path):
    result = read_experiment_ledger(tmp_path / "state")
    assert result == {"status": "missing", "columns": list(COLUMNS), "rows": []}


def test_valid_empty_ledger_is_distinct(tmp_path: Path):
    state = tmp_path / "state"
    path = ledger_path(state)
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    assert read_experiment_ledger(state)["status"] == "empty"
    assert read_experiment_ledger(state)["rows"] == []


def test_corrupt_ledger_is_unavailable(tmp_path: Path):
    state = tmp_path / "state"
    path = ledger_path(state)
    path.parent.mkdir(parents=True)
    path.write_text("not json\n", encoding="utf-8")
    result = read_experiment_ledger(state)
    assert result["status"] == "unavailable"
    assert result["rows"] == []


def test_writer_reader_round_trip_preserves_exact_five_columns(tmp_path: Path):
    state = tmp_path / "state"
    assert append_experiment_result(
        state,
        matrix_cell="model=un/qwen;prompt=v1",
        arm="control",
        what_changed="baseline prompt",
        measured_delta="+0.12",
        verdict="keep",
    )
    result = read_experiment_ledger(state)
    assert result["status"] == "present"
    assert result["columns"] == list(COLUMNS)
    assert result["rows"] == [{
        "matrix_cell": "model=un/qwen;prompt=v1",
        "arm": "control",
        "what_changed": "baseline prompt",
        "measured_delta": "+0.12",
        "verdict": "keep",
    }]
    assert set(result["rows"][0]) == set(COLUMNS)


def test_failed_write_is_fail_open(tmp_path: Path):
    state = tmp_path / "state"
    path = ledger_path(state)
    path.parent.mkdir(parents=True)
    path.mkdir()
    assert append_experiment_result(
        state, matrix_cell="x", arm="a", what_changed="c", measured_delta="0", verdict="discard"
    ) is False


def test_experiment_ledger_is_separate_from_cycle_ledger_and_survives_reset(tmp_path: Path):
    state = tmp_path / "state"
    cycle = state / "ledger" / "cycles.jsonl"
    cycle.parent.mkdir(parents=True)
    cycle.write_text(json.dumps({"phase": "outcome", "outcome": "success"}) + "\n", encoding="utf-8")
    assert append_experiment_result(
        state, matrix_cell="cell", arm="variant", what_changed="change", measured_delta="-1", verdict="discard"
    )
    # Simulate the cycle reset boundary: clear only the cycle ledger.
    cycle.write_text("", encoding="utf-8")
    assert cycle.read_text(encoding="utf-8") == ""
    result = read_experiment_ledger(state)
    assert result["status"] == "present"
    assert result["rows"][0]["verdict"] == "discard"


def test_no_runtime_or_llm_dependency():
    source = Path("nanobot/runtime/experiment_ledger.py").read_text(encoding="utf-8")
    assert "llm" not in source.lower()
    assert "cycle_ledger" not in source
