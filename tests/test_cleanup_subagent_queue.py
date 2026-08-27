"""
Tests for scripts/cleanup_subagent_queue.py — harvested enhancements (issue #672):
- --max-queue N: count-based archival once age-based cleanup still leaves >N files.
- --json: machine-readable output mode.
- _METADATA_FILES exclusion: metadata files are never counted/archived as queue entries.
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest


def _load_module():
    """Import scripts/cleanup_subagent_queue.py as a module without running __main__."""
    script_path = Path(__file__).parent.parent / "scripts" / "cleanup_subagent_queue.py"
    spec = importlib.util.spec_from_file_location("cleanup_subagent_queue", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cleanup_mod():
    return _load_module()


def _run_main(cleanup_mod, argv):
    old_argv = sys.argv
    sys.argv = ["cleanup_subagent_queue.py"] + argv
    try:
        return cleanup_mod.main()
    finally:
        sys.argv = old_argv


def _make_fresh_file(path: Path, name: str) -> Path:
    f = path / name
    f.write_text(json.dumps({"id": name}))
    return f


def _age_file(path: Path, hours: float) -> None:
    old_time = time.time() - (hours * 3600)
    os.utime(path, (old_time, old_time))


def test_max_queue_count_based_archival(cleanup_mod, tmp_path):
    """After age-based cleanup, if more than --max-queue files remain, oldest are archived."""
    requests_dir = tmp_path / "subagents" / "requests"
    requests_dir.mkdir(parents=True)

    # 5 fresh files — age-based cleanup won't touch any of them.
    files = []
    for i in range(5):
        f = _make_fresh_file(requests_dir, f"req_{i}.json")
        files.append(f)
        # Stagger mtimes so ordering (oldest-first) is deterministic.
        os.utime(f, (time.time() - (5 - i), time.time() - (5 - i)))

    rc = _run_main(
        cleanup_mod,
        ["--hours", "24", "--max-queue", "2", "--state-root", str(tmp_path)],
    )

    assert rc == 0
    remaining = list(requests_dir.glob("*.json"))
    assert len(remaining) == 2

    archive_dir = tmp_path / "subagents" / "archive"
    archived = list(archive_dir.glob("*.json"))
    assert len(archived) == 3
    # The oldest three (req_0, req_1, req_2) should have been archived.
    archived_names = {p.name for p in archived}
    assert archived_names == {"req_0.json", "req_1.json", "req_2.json"}


def test_max_queue_no_op_when_under_limit(cleanup_mod, tmp_path):
    requests_dir = tmp_path / "subagents" / "requests"
    requests_dir.mkdir(parents=True)
    _make_fresh_file(requests_dir, "req_only.json")

    rc = _run_main(
        cleanup_mod,
        ["--hours", "24", "--max-queue", "9", "--state-root", str(tmp_path)],
    )

    assert rc == 0
    assert len(list(requests_dir.glob("*.json"))) == 1


def test_metadata_files_excluded_from_queue_and_archival(cleanup_mod, tmp_path):
    """archive_latest.json, queue_index.json, index.json must never be archived or counted."""
    subagents_root = tmp_path / "subagents"
    subagents_root.mkdir(parents=True)

    metadata_names = ["archive_latest.json", "queue_index.json", "index.json"]
    for name in metadata_names:
        f = _make_fresh_file(subagents_root, name)
        _age_file(f, 48)  # old enough to be archived if it were a queue entry

    rc = _run_main(
        cleanup_mod,
        ["--hours", "24", "--max-queue", "0", "--state-root", str(tmp_path)],
    )

    assert rc == 0
    # Metadata files must remain in place, untouched.
    for name in metadata_names:
        assert (subagents_root / name).exists()

    archive_dir = subagents_root / "archive"
    if archive_dir.exists():
        archived_names = {p.name for p in archive_dir.glob("*.json")}
        assert archived_names.isdisjoint(set(metadata_names))

    health = json.loads((tmp_path / "current_health.json").read_text())
    assert health["subagent_queue_count"] == 0


def test_json_output_mode(cleanup_mod, tmp_path, capsys):
    requests_dir = tmp_path / "subagents" / "requests"
    requests_dir.mkdir(parents=True)
    f = _make_fresh_file(requests_dir, "stale.json")
    _age_file(f, 48)

    rc = _run_main(
        cleanup_mod,
        ["--hours", "24", "--json", "--state-root", str(tmp_path)],
    )

    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["archived"] == 1
    assert payload["kept"] == 0
    assert payload["errors"] == 0
    assert payload["dry_run"] is False
    assert "subagent_queue_count" in payload
    assert "subagent_archive_count" in payload
    assert "timestamp" in payload


def test_json_output_mode_dry_run(cleanup_mod, tmp_path, capsys):
    requests_dir = tmp_path / "subagents" / "requests"
    requests_dir.mkdir(parents=True)
    f = _make_fresh_file(requests_dir, "stale.json")
    _age_file(f, 48)

    rc = _run_main(
        cleanup_mod,
        ["--hours", "24", "--json", "--dry-run", "--state-root", str(tmp_path)],
    )

    assert rc == 0
    captured = capsys.readouterr()
    # Dry-run mode still prints the "WOULD ARCHIVE" line before the JSON blob;
    # the JSON blob itself must be the last, valid, parseable JSON payload.
    json_start = captured.out.rindex("{")
    payload = json.loads(captured.out[json_start:])
    assert payload["dry_run"] is True
    assert f.exists()  # nothing actually moved


def test_archive_pruning_30_days(cleanup_mod, tmp_path):
    """Issue #1039: purge files from subagents/archive/ older than archive_retention_days (default 30d)."""
    archive_dir = tmp_path / "subagents" / "archive"
    archive_dir.mkdir(parents=True)

    old_file = _make_fresh_file(archive_dir, "req-old.json")
    _age_file(old_file, 35 * 24)  # 35 days old

    recent_file = _make_fresh_file(archive_dir, "req-recent.json")
    _age_file(recent_file, 10 * 24)  # 10 days old

    # Also make an archive_latest.json file which must NOT be deleted
    meta_file = _make_fresh_file(archive_dir, "archive_latest.json")
    _age_file(meta_file, 40 * 24)

    rc = _run_main(
        cleanup_mod,
        ["--archive-retention-days", "30", "--state-root", str(tmp_path)],
    )

    assert rc == 0
    assert not old_file.exists()
    assert recent_file.exists()
    assert meta_file.exists()
