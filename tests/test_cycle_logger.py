"""
Tests for scripts/cycle_logger.py — harvested enhancements (issue #672):
- --list [--count N] [--json]: list recent HISTORY.md entries.
- --dedup [--dry-run]: remove duplicate cycle entries.
- --compact [--dry-run]: collapse repeated verification entries.
- --stats [--json]: summary statistics.
"""
import importlib.util
import json
from pathlib import Path

import pytest


def _load_module():
    """Import scripts/cycle_logger.py as a module without running __main__."""
    script_path = Path(__file__).parent.parent / "scripts" / "cycle_logger.py"
    spec = importlib.util.spec_from_file_location("cycle_logger", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cycle_logger():
    return _load_module()


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "memory").mkdir()
    return tmp_path


def _write_history(repo: Path, text: str) -> Path:
    hist = repo / "memory" / "HISTORY.md"
    hist.write_text(text, encoding="utf-8")
    return hist


def test_list_recent_entries(cycle_logger, repo):
    cycle_logger.append_cycle_summary(repo, "cycle-1", "feat: added A")
    cycle_logger.append_cycle_summary(repo, "cycle-2", "feat: added B")
    cycle_logger.append_cycle_summary(repo, "cycle-3", "feat: added C")

    entries = cycle_logger.list_cycles(repo)
    assert len(entries) == 3
    # Newest first.
    assert entries[0]["cycle_id"] == "cycle-3"
    assert entries[2]["cycle_id"] == "cycle-1"


def test_list_with_count(cycle_logger, repo):
    cycle_logger.append_cycle_summary(repo, "cycle-1", "feat: added A")
    cycle_logger.append_cycle_summary(repo, "cycle-2", "feat: added B")

    entries = cycle_logger.list_cycles(repo, count=1)
    assert len(entries) == 1
    assert entries[0]["cycle_id"] == "cycle-2"


def test_list_empty_history(cycle_logger, repo):
    assert cycle_logger.list_cycles(repo) == []


def test_dedup_removes_duplicate(cycle_logger, repo):
    _write_history(
        repo,
        "- 2026-07-01: [cycle-a] first\n"
        "- 2026-07-02: [cycle-b] unique\n"
        "- 2026-07-03: [cycle-a] duplicate\n",
    )

    result = cycle_logger.dedup_cycles(repo, dry_run=False)

    assert result["duplicates_removed"] == 1
    assert result["kept"] == 2
    text = (repo / "memory" / "HISTORY.md").read_text()
    assert text.count("cycle-a") == 1
    assert "cycle-b" in text


def test_dedup_dry_run_does_not_write(cycle_logger, repo):
    original = (
        "- 2026-07-01: [cycle-a] first\n"
        "- 2026-07-03: [cycle-a] duplicate\n"
    )
    _write_history(repo, original)

    result = cycle_logger.dedup_cycles(repo, dry_run=True)

    assert result["duplicates_removed"] == 1
    assert result["dry_run"] is True
    assert (repo / "memory" / "HISTORY.md").read_text() == original


def test_compact_collapses_verification_entries(cycle_logger, repo):
    _write_history(
        repo,
        "- 2026-07-01: [cycle-v1] feat: added X (files: scripts/x.py)\n"
        "- 2026-07-02: [cycle-v2] chore: confirm Priority 5 verified for cycle-abc — 9/9 self-tests pass\n"
        "- 2026-07-03: [cycle-v3] chore: confirm Priority 5 verified for cycle-def — 9/9 self-tests pass\n"
        "- 2026-07-04: [cycle-v4] fix: corrected Y (files: scripts/y.py)\n",
    )

    result = cycle_logger.compact_history(repo, dry_run=False)

    assert result["collapsed"] == 1
    text = (repo / "memory" / "HISTORY.md").read_text()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 3
    assert "+1 more verifications" in text
    assert "feat: added X" in text
    assert "fix: corrected Y" in text


def test_compact_dry_run_does_not_write(cycle_logger, repo):
    original = (
        "- 2026-07-01: [cycle-v1] chore: confirm Priority 5 verified — pass\n"
        "- 2026-07-02: [cycle-v2] chore: confirm Priority 5 verified — pass\n"
    )
    _write_history(repo, original)

    result = cycle_logger.compact_history(repo, dry_run=True)

    assert result["collapsed"] == 1
    assert (repo / "memory" / "HISTORY.md").read_text() == original


def test_stats_summary(cycle_logger, repo):
    _write_history(
        repo,
        "- 2026-07-01: [cycle-s1] action A\n"
        "- 2026-07-02: [cycle-s2] action B\n"
        "- 2026-07-03: [cycle-s3] action C\n",
    )

    stats = cycle_logger.cycle_stats(repo)

    assert stats["cycle_entries"] == 3
    assert stats["unique_cycles"] == 3
    assert stats["duplicates"] == 0
    assert stats["date_range"] == {"earliest": "2026-07-01", "latest": "2026-07-03"}


def test_stats_json_via_cli(cycle_logger, repo, capsys):
    cycle_logger.append_cycle_summary(repo, "cycle-1", "feat: added A")

    import sys

    old_argv = sys.argv
    sys.argv = ["cycle_logger.py", "--repo-root", str(repo), "--stats", "--json"]
    try:
        with pytest.raises(SystemExit) as exc_info:
            cycle_logger.main()
    finally:
        sys.argv = old_argv

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["cycle_entries"] == 1


def test_list_json_via_cli(cycle_logger, repo, capsys):
    cycle_logger.append_cycle_summary(repo, "cycle-1", "feat: added A")
    cycle_logger.append_cycle_summary(repo, "cycle-2", "feat: added B")

    import sys

    old_argv = sys.argv
    sys.argv = ["cycle_logger.py", "--repo-root", str(repo), "--list", "--count", "1", "--json"]
    try:
        with pytest.raises(SystemExit) as exc_info:
            cycle_logger.main()
    finally:
        sys.argv = old_argv

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert len(payload) == 1
    assert payload[0]["cycle_id"] == "cycle-2"
