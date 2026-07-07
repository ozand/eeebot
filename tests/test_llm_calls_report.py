"""Tests for scripts/llm_calls_report.py (issue #675)."""

import importlib.util
import json
from pathlib import Path

import pytest


def _load_module():
    """Import scripts/llm_calls_report.py as a module without running __main__."""
    script_path = Path(__file__).parent.parent / "scripts" / "llm_calls_report.py"
    spec = importlib.util.spec_from_file_location("llm_calls_report", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def report_module():
    return _load_module()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_load_records_reads_all_jsonl_files(tmp_path, report_module):
    _write_jsonl(tmp_path / "2026-07-01.jsonl", [{"model": "m1"}])
    _write_jsonl(tmp_path / "2026-07-02.jsonl", [{"model": "m2"}, {"model": "m2"}])

    records = report_module.load_records(tmp_path)
    assert len(records) == 3


def test_load_records_since_filters_by_day(tmp_path, report_module):
    _write_jsonl(tmp_path / "2026-07-01.jsonl", [{"model": "m1"}])
    _write_jsonl(tmp_path / "2026-07-05.jsonl", [{"model": "m2"}])

    records = report_module.load_records(tmp_path, since="2026-07-03")
    assert len(records) == 1
    assert records[0]["model"] == "m2"


def test_load_records_skips_malformed_lines(tmp_path, report_module):
    path = tmp_path / "2026-07-01.jsonl"
    path.write_text('{"model": "m1"}\nnot-json\n{"model": "m2"}\n', encoding="utf-8")

    records = report_module.load_records(tmp_path)
    assert len(records) == 2


def test_load_records_missing_dir_returns_empty(tmp_path, report_module):
    records = report_module.load_records(tmp_path / "does-not-exist")
    assert records == []


def test_aggregate_per_model_and_per_cycle(report_module):
    records = [
        {"model": "m1", "duration_ms": 100.0, "total_tokens": 10, "cycle_id": "c1"},
        {"model": "m1", "duration_ms": 200.0, "total_tokens": 20, "cycle_id": "c1"},
        {"model": "m2", "duration_ms": 50.0, "total_tokens": 5, "cycle_id": "c2"},
    ]

    summary = report_module.aggregate(records)

    assert summary["totals"]["calls"] == 3
    assert summary["totals"]["duration_ms"] == pytest.approx(350.0)
    assert summary["totals"]["tokens"] == 35

    assert summary["per_model"]["m1"]["count"] == 2
    assert summary["per_model"]["m1"]["total_tokens"] == 30
    assert summary["per_model"]["m1"]["avg_duration_ms"] == pytest.approx(150.0)

    assert summary["per_model"]["m2"]["count"] == 1
    assert summary["per_model"]["m2"]["total_tokens"] == 5

    assert summary["per_cycle_duration_ms"]["c1"] == pytest.approx(300.0)
    assert summary["per_cycle_duration_ms"]["c2"] == pytest.approx(50.0)


def test_aggregate_handles_missing_cycle_id(report_module):
    records = [{"model": "m1", "duration_ms": 10.0, "total_tokens": 1}]
    summary = report_module.aggregate(records)
    assert summary["per_cycle_duration_ms"] == {}


def test_aggregate_empty_records(report_module):
    summary = report_module.aggregate([])
    assert summary["totals"]["calls"] == 0
    assert summary["per_model"] == {}
    assert summary["per_cycle_duration_ms"] == {}


def test_main_json_output(tmp_path, report_module, capsys):
    _write_jsonl(tmp_path / "2026-07-01.jsonl", [
        {"model": "m1", "duration_ms": 10.0, "total_tokens": 1, "cycle_id": "c1"},
    ])

    rc = report_module.main(["--dir", str(tmp_path), "--json"])
    assert rc == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["totals"]["calls"] == 1


def test_main_table_output(tmp_path, report_module, capsys):
    _write_jsonl(tmp_path / "2026-07-01.jsonl", [
        {"model": "m1", "duration_ms": 10.0, "total_tokens": 1, "cycle_id": "c1"},
    ])

    rc = report_module.main(["--dir", str(tmp_path)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Total calls: 1" in out
    assert "m1" in out
