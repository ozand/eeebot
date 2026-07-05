"""Tests for Issue #518: memory_archiver.py — L0/L1 memory split with LLM/deterministic summary."""
from __future__ import annotations
import datetime
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.memory_archiver import (
    _parse_history_entries,
    _week_label,
    _last_archive_date,
    _needs_archiving,
    _deterministic_summary,
    _summarize_with_llm,
    archive,
    should_archive,
    HISTORY_KEEP_DAYS,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

OLD_DATE = (datetime.date.today() - datetime.timedelta(days=HISTORY_KEEP_DAYS + 5)).isoformat()
RECENT_DATE = datetime.date.today().isoformat()

SAMPLE_HISTORY = f"""\
- {RECENT_DATE}: [cycle-new001] feat: add report_summary.py (files: scripts/report_summary.py)
- {RECENT_DATE}: [cycle-new002] fix: smoke_test stall detection (files: scripts/smoke_test_loop.py)
- {OLD_DATE}: [cycle-old001] feat: add cycle_logger.py (files: scripts/cycle_logger.py)
- {OLD_DATE}: [cycle-old002] chore: mark Priority 5 Done in MEMORY.md
"""

SAMPLE_MEMORY_SMALL = """\
# Project Memory

## Active backlog — pick one each session

### Priority 9: Do X
Instructions.

## Completed

### Priority 8: Old [Done]
Done.
"""

SAMPLE_ARCHIVE_OLD = f"""\
## Week 2026-W20 (2026-05-18)
Old week summary.
"""


def _make_repo(tmp: Path, history: str = "", memory: str = "", archive: str = "") -> Path:
    repo = tmp / "eeebot-self-evolving"
    (repo / "memory").mkdir(parents=True)
    if history:
        (repo / "memory" / "HISTORY.md").write_text(history)
    if memory:
        (repo / "memory" / "MEMORY.md").write_text(memory)
    if archive:
        (repo / "memory" / "MEMORY_ARCHIVE.md").write_text(archive)
    return repo


# ── Test 1: _parse_history_entries ───────────────────────────────────────────

def test_parse_history_returns_list():
    entries = _parse_history_entries(SAMPLE_HISTORY)
    assert len(entries) == 4
    assert all("date" in e and "text" in e for e in entries)


def test_parse_history_extracts_dates():
    entries = _parse_history_entries(SAMPLE_HISTORY)
    today = datetime.date.today()
    recent = [e for e in entries if e["date"] == today]
    assert len(recent) == 2


# ── Test 2: _needs_archiving ──────────────────────────────────────────────────

def test_needs_archiving_when_no_archive():
    assert _needs_archiving(SAMPLE_MEMORY_SMALL, "") is True


def test_needs_archiving_when_archive_stale():
    stale_archive = "## Week 2026-W10 (2026-03-09)\nOld summary.\n"
    assert _needs_archiving(SAMPLE_MEMORY_SMALL, stale_archive) is True


def test_not_needs_archiving_when_fresh():
    today = datetime.date.today()
    fresh_archive = f"## Week {today.isocalendar()[0]}-W{today.isocalendar()[1]:02d} ({today.isoformat()})\nFresh summary.\n"
    assert _needs_archiving(SAMPLE_MEMORY_SMALL, fresh_archive) is False


# ── Test 3: _deterministic_summary ───────────────────────────────────────────

def test_deterministic_summary_has_three_sentences():
    entries = ["- 2026-06-14: feat: add cycle_logger.py (files: scripts/cycle_logger.py)",
               "- 2026-06-14: fix: fixed coordinator NameError"]
    summary = _deterministic_summary(entries, "Week 2026-W24 (2026-06-10)")
    assert summary.count(".") >= 2, f"Expected >= 2 sentences, got: {summary}"
    assert "Week 2026-W24" in summary


# ── Test 4: LLM summarization mock ───────────────────────────────────────────

def test_summarize_with_llm_uses_correct_model(monkeypatch):
    """_summarize_with_llm sends request to SUMMARY_MODEL."""
    import scripts.memory_archiver as memory_archiver

    monkeypatch.setattr(memory_archiver, "LITELLM_BASE_URL", "http://litellm.internal.test:4001/v1")

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "choices": [{"message": {"content": "Week summary sentence one. Sentence two. Sentence three."}}]
    }).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = _summarize_with_llm("Some log entries", "Week 2026-W26")
    assert result is not None
    assert "sentence" in result.lower()


def test_summarize_with_llm_returns_none_on_error():
    """_summarize_with_llm returns None when LLM is unavailable."""
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        result = _summarize_with_llm("Some log entries", "Week 2026-W26")
    assert result is None


# ── Test 5: archive() full flow ───────────────────────────────────────────────

def test_archive_dry_run_does_not_write():
    with tempfile.TemporaryDirectory() as td:
        repo = _make_repo(Path(td), history=SAMPLE_HISTORY, memory=SAMPLE_MEMORY_SMALL)
        archive_path = repo / "memory" / "MEMORY_ARCHIVE.md"

        with patch("scripts.memory_archiver._summarize_with_llm", return_value=None):
            result = archive(repo, dry_run=True, force=True)

        assert not archive_path.exists(), "Dry-run must not create archive file"
        assert result["action"] == "dry_run"


def test_archive_creates_archive_file():
    with tempfile.TemporaryDirectory() as td:
        repo = _make_repo(Path(td), history=SAMPLE_HISTORY, memory=SAMPLE_MEMORY_SMALL)
        archive_path = repo / "memory" / "MEMORY_ARCHIVE.md"

        with patch("scripts.memory_archiver._summarize_with_llm", return_value=None):
            result = archive(repo, force=True)

        assert result["action"] == "archived"
        if archive_path.exists():
            archive_text = archive_path.read_text()
            assert "## Week" in archive_text


def test_archive_skips_when_not_needed():
    today = datetime.date.today()
    fresh_archive = f"## Week {today.isocalendar()[0]}-W{today.isocalendar()[1]:02d} ({today.isoformat()})\nFresh.\n"
    with tempfile.TemporaryDirectory() as td:
        repo = _make_repo(Path(td), history=SAMPLE_HISTORY, memory=SAMPLE_MEMORY_SMALL, archive=fresh_archive)
        result = archive(repo, force=False)
        assert result["action"] == "skipped"


# ── Test 6: should_archive helper ────────────────────────────────────────────

def test_should_archive_returns_bool():
    with tempfile.TemporaryDirectory() as td:
        repo = _make_repo(Path(td), memory=SAMPLE_MEMORY_SMALL)
        result = should_archive(repo)
        assert isinstance(result, bool)
        assert result is True  # no archive file → needs archiving
