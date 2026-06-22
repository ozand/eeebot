"""Tests for #525: curriculum-style staged backlog.

Verifies that _curriculum_level() and _parse_backlog_task_from_memory()
enforce Darwin-Mode-style progression: only return task at the current
curriculum level; block higher-numbered priorities until lower ones are done.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nanobot.runtime.coordinator import (
    _curriculum_level,
    _parse_backlog_task_from_memory,
)


MEMORY_P9_DONE_P10_ACTIVE = textwrap.dedent("""\
    # Project Memory

    ## Active backlog

    ### Priority 9: Restructure MEMORY.md [Done]
    File: memory/MEMORY.md
    Move done blocks to Completed section.

    ### Priority 10: HISTORY.md newest-first — update cycle_logger.py to prepend
    File: scripts/cycle_logger.py (already exists).
    Change append_cycle_summary() to prepend new entries.

    ### Priority 11: Auto-seed backlog from research/feed.json
    File: scripts/eeepc_self_evolving_subagent_bridge.py
    After marking last priority Done, auto-seed.

    ## Completed
    """)

MEMORY_P9_ACTIVE = textwrap.dedent("""\
    # Project Memory

    ## Active backlog

    ### Priority 9: Restructure MEMORY.md — active backlog first
    File: memory/MEMORY.md
    Move done blocks to Completed section.

    ### Priority 10: HISTORY.md newest-first
    File: scripts/cycle_logger.py

    ## Completed
    """)

MEMORY_ALL_DONE = textwrap.dedent("""\
    # Project Memory

    ## Active backlog

    ### Priority 9: Restructure MEMORY.md [Done]
    File: memory/MEMORY.md

    ### Priority 10: HISTORY.md newest-first [Done]
    File: scripts/cycle_logger.py

    ## Completed
    """)


def _make_selfevo(tmp_path: Path, content: str) -> Path:
    """Create a fake eeebot-self-evolving with a MEMORY.md."""
    repo = tmp_path / "eeebot-self-evolving"
    (repo / "memory").mkdir(parents=True)
    (repo / "memory" / "MEMORY.md").write_text(content)
    return repo


# ─── _curriculum_level tests ──────────────────────────────────────────────────

def test_curriculum_level_p9_done_returns_10(tmp_path):
    """P9 has [Done] marker → curriculum level = 10."""
    repo = _make_selfevo(tmp_path, MEMORY_P9_DONE_P10_ACTIVE)
    with patch("subprocess.check_output", return_value=b""):
        level = _curriculum_level(repo)
    assert level == 10


def test_curriculum_level_p9_active_returns_9(tmp_path):
    """P9 has no [Done] marker → curriculum level = 9."""
    repo = _make_selfevo(tmp_path, MEMORY_P9_ACTIVE)
    with patch("subprocess.check_output", return_value=b""):
        level = _curriculum_level(repo)
    assert level == 9


def test_curriculum_level_all_done_returns_9999(tmp_path):
    """All priorities Done → level = 9999 (backlog exhausted)."""
    repo = _make_selfevo(tmp_path, MEMORY_ALL_DONE)
    with patch("subprocess.check_output", return_value=b""):
        level = _curriculum_level(repo)
    assert level == 9999


def test_curriculum_level_git_detection(tmp_path):
    """P9 found via git log keywords → treated as done, level advances to 10."""
    repo = _make_selfevo(tmp_path, MEMORY_P9_ACTIVE)
    # Simulate git log showing P9-related commit
    fake_log = b"abc1234 restructure MEMORY.md backlog first\n"
    with patch("subprocess.check_output", return_value=fake_log):
        level = _curriculum_level(repo)
    assert level == 10


def test_curriculum_level_missing_memory(tmp_path):
    """Missing MEMORY.md → returns default level 9."""
    repo = tmp_path / "eeebot-self-evolving"
    repo.mkdir()
    level = _curriculum_level(repo)
    assert level == 9


# ─── _parse_backlog_task_from_memory with curriculum gate ─────────────────────

def test_parse_returns_p10_when_p9_done(tmp_path):
    """When P9 is [Done], parse returns P10."""
    repo = _make_selfevo(tmp_path, MEMORY_P9_DONE_P10_ACTIVE)
    with patch("subprocess.check_output", return_value=b""):
        result = _parse_backlog_task_from_memory(repo)
    assert result is not None
    assert result["priority"] == 10
    assert "curriculum_level" in result
    assert result["curriculum_level"] == 10


def test_parse_returns_p9_when_active(tmp_path):
    """When P9 is active, parse returns P9 (not P10)."""
    repo = _make_selfevo(tmp_path, MEMORY_P9_ACTIVE)
    with patch("subprocess.check_output", return_value=b""):
        result = _parse_backlog_task_from_memory(repo)
    assert result is not None
    assert result["priority"] == 9


def test_parse_blocks_p11_when_p10_active(tmp_path):
    """P10 active but P11 is in backlog — parse must NOT return P11."""
    repo = _make_selfevo(tmp_path, MEMORY_P9_DONE_P10_ACTIVE)
    with patch("subprocess.check_output", return_value=b""):
        result = _parse_backlog_task_from_memory(repo)
    # Should return P10, never P11
    assert result is not None
    assert result["priority"] == 10


def test_parse_returns_none_when_all_done(tmp_path):
    """All priorities Done → parse returns None (triggers research feed fallback)."""
    repo = _make_selfevo(tmp_path, MEMORY_ALL_DONE)
    with patch("subprocess.check_output", return_value=b""):
        result = _parse_backlog_task_from_memory(repo)
    assert result is None


def test_parse_includes_curriculum_level_in_result(tmp_path):
    """Result dict must include curriculum_level field for build_task() to use."""
    repo = _make_selfevo(tmp_path, MEMORY_P9_DONE_P10_ACTIVE)
    with patch("subprocess.check_output", return_value=b""):
        result = _parse_backlog_task_from_memory(repo)
    assert result is not None
    assert "curriculum_level" in result
    assert isinstance(result["curriculum_level"], int)
