from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

import pytest

from nanobot.runtime import state_access

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_live_shape"
ARTIFACT_FIXTURE = Path(__file__).parent / "subagents"


def _row(phase: str, ts: str, cycle: str = "c") -> dict:
    return {"phase": phase, "ts": ts, "cycle_id": cycle}


def _write_ledger(state: Path, rows: list[dict]) -> None:
    d = state / "ledger"
    d.mkdir(parents=True)
    (d / "cycles.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_ledger_window_reads_archive_and_active_in_order(tmp_path):
    state = tmp_path / "state"
    d = state / "ledger"
    d.mkdir(parents=True)
    with gzip.open(d / "cycles-2026-09-01.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_row("started", "2026-09-01T10:00:00Z", "old")) + "\n")
    (d / "cycles.jsonl").write_text(json.dumps(_row("outcome", "2026-09-02T10:00:00Z", "new")) + "\n", encoding="utf-8")
    result = state_access.ledger_window(state, since_ts="2026-09-01T00:00:00Z")
    assert [r["cycle_id"] for r in result.rows] == ["old", "new"]
    assert result.status == "complete"
    assert result.files_read == 2
    assert result.covered_from <= "2026-09-01T00:00:00Z"
    assert result.covered_to == "2026-09-02T10:00:00Z"


def test_ledger_window_corrupt_archive_is_partial(tmp_path):
    state = tmp_path / "state"
    d = state / "ledger"
    d.mkdir(parents=True)
    (d / "cycles-2026-09-01.jsonl.gz").write_bytes(b"bad gzip")
    (d / "cycles.jsonl").write_text(json.dumps(_row("started", "2026-09-02T00:00:00Z")) + "\n", encoding="utf-8")
    result = state_access.ledger_window(state, since_ts="2026-09-01T00:00:00Z")
    assert result.rows and result.files_skipped == 1
    assert any("cycles-2026-09-01" in note for note in result.notes)
    assert result.status == "partial"


def test_ledger_window_cap_is_partial(tmp_path):
    state = tmp_path / "state"
    _write_ledger(state, [_row("outcome", f"2026-09-02T00:00:{i:02d}Z", str(i)) for i in range(10)])
    result = state_access.ledger_window(state, since_ts="2026-09-01T00:00:00Z", max_bytes=80)
    assert result.status == "partial"
    assert "cap_bytes" in result.notes
    assert len(result.rows) < 10
    assert result.covered_to == result.rows[-1]["ts"]


def test_ledger_window_missing_dir_is_unavailable(tmp_path):
    result = state_access.ledger_window(tmp_path / "missing", since_ts="2026-09-01T00:00:00Z")
    assert result.status == "unavailable" and result.rows == ()
    assert "dir_missing" in result.notes


def test_invalid_archive_name_is_not_read(tmp_path):
    state = tmp_path / "state"
    d = state / "ledger"
    d.mkdir(parents=True)
    (d / "cycles-old.jsonl.gz").write_bytes(b"not used")
    (d / "cycles.jsonl").write_text("", encoding="utf-8")
    result = state_access.ledger_window(state, since_ts="2026-09-01T00:00:00Z")
    assert any(note == "invalid_archive:cycles-old.jsonl.gz" for note in result.notes)


def test_ledger_file_reports_non_permission_io_error(tmp_path):
    result = state_access._read_ledger_file(
        tmp_path / "missing.jsonl",
        since=state_access._parse_ts("2026-09-01T00:00:00Z"),
        phases=None,
        remaining=100,
    )
    assert result[-1] == "io_error"


def test_artifacts_filters_before_newest_bound_and_supports_directory_selection(tmp_path):
    root = tmp_path / "state" / "subagents"
    for name in ("results", "requests", "archive"):
        (root / name).mkdir(parents=True)
    for i in range(60):
        path = root / "requests" / f"request-{i:03d}.json"
        path.write_text(json.dumps({"status": "pending"}), encoding="utf-8")
        os.utime(path, (time.time() + i, time.time() + i))
    result = root / "archive" / "old-result.json"
    result.write_text(json.dumps({"files_changed": ["scripts/foo.py"]}), encoding="utf-8")
    os.utime(result, (time.time() - 1, time.time() - 1))
    window = state_access.artifacts(
        tmp_path / "state", newest=1, directories=("results", "archive"), required_key="files_changed"
    )
    assert [row["files_changed"] for row in window.rows] == [["scripts/foo.py"]]
    assert window.paths == (result,)


def test_artifacts_unifies_dirs_and_tie_breaks(tmp_path):
    root = tmp_path / "state" / "subagents"
    for name in ("results", "requests", "archive"):
        (root / name).mkdir(parents=True)
    paths = []
    for directory, filename, status in (("results", "a.json", "failed"), ("archive", "b.json", "blocked"), ("requests", "c.json", "error"), ("archive", "d.json", "ok")):
        p = root / directory / filename
        p.write_text(json.dumps({"status": status, "id": filename}), encoding="utf-8")
        paths.append(p)
    stamp = time.time() - 10
    for p in paths:
        os.utime(p, (stamp, stamp))
    result = state_access.artifacts(tmp_path / "state", newest=3, statuses=frozenset({"failed", "blocked", "error"}))
    assert [r["id"] for r in result.rows] == ["c.json", "b.json", "a.json"]


def test_artifacts_unavailable_when_selected_sources_cannot_be_read(tmp_path, monkeypatch):
    root = tmp_path / "state" / "subagents" / "results"
    root.mkdir(parents=True)
    original_is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: False if self == root else original_is_dir(self))
    result = state_access.artifacts(tmp_path / "state", newest=1, directories=("results",))
    assert result.status == "unavailable"
    assert "permission" in result.notes


def test_latest_file_tie_break_and_stale(tmp_path):
    d = tmp_path / "reports"
    d.mkdir()
    for name in ("evolution-a.json", "evolution-b.json"):
        (d / name).write_text("{}", encoding="utf-8")
    stamp = time.time() - 10
    for p in d.iterdir():
        os.utime(p, (stamp, stamp))
    result = state_access.latest_file(d, "evolution-*.json", max_age_s=1)
    assert result.path.name == "evolution-b.json"
    assert result.stale is True
    assert state_access.latest_file(tmp_path / "missing", "*.json", max_age_s=1).status == "dir_missing"


@pytest.mark.parametrize("content,status", [(None, "absent"), ("not json", "corrupt")])
def test_sidecar_statuses(tmp_path, content, status):
    path = tmp_path / "sidecar.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    result = state_access.sidecar(path, default={}, max_bytes=100)
    assert result.status == status


def test_sidecar_oversize(tmp_path):
    path = tmp_path / "sidecar.json"
    path.write_text("x" * 101, encoding="utf-8")
    assert state_access.sidecar(path, default={}, max_bytes=100).status == "oversize"


def test_prefilter_avoids_json_loads_for_nonmatching_rows(tmp_path, monkeypatch):
    state = tmp_path / "state"
    rows = [_row("started", "2026-09-02T00:00:00Z", str(i)) for i in range(10000)]
    rows.extend(_row("outcome", "2026-09-02T01:00:00Z", str(i)) for i in range(3))
    _write_ledger(state, rows)
    original = json.loads
    calls = 0

    def counting(value, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(value, *args, **kwargs)

    monkeypatch.setattr(state_access.json, "loads", counting)
    result = state_access.ledger_window(state, since_ts="2026-09-01T00:00:00Z", phases=frozenset({"outcome"}))
    assert len(result.rows) == 3
    assert calls <= 4


def test_deploy_phase_contract_fixture():
    fixture = (FIXTURE / "cycles.jsonl").read_text(encoding="utf-8")
    assert '"phase": "outcome"' in fixture
