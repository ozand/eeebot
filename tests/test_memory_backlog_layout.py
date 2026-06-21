"""Tests for Issue #516: MEMORY.md restructure — active/completed sections,
and cycle_logger.py newest-first prepend behavior."""
from __future__ import annotations
import re
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

ACTIVE_BACKLOG_SAMPLE = """\
# Project Memory

## Identity
- Host: eeepc

## Active backlog — pick one each session

<!-- BACKLOG_START -->

### Priority 9: Do something new
File: scripts/new.py — implement it.

### Priority 10: Another task [Done]
Already done.

<!-- BACKLOG_END -->

---

## Completed
<!-- Completed priorities are moved here -->

### Priority 8: Old task [Done]
Completed earlier.
"""

ALL_DONE_SAMPLE = """\
# Project Memory

## Identity
- Host: eeepc

## Active backlog — pick one each session

<!-- BACKLOG_START -->

<!-- BACKLOG_END -->

---

## Completed

### Priority 7: Something [Done]
Completed.
"""


def _make_repo(tmp: Path, memory_text: str, history_text: str = "") -> Path:
    (tmp / "memory").mkdir(parents=True, exist_ok=True)
    (tmp / "memory" / "MEMORY.md").write_text(memory_text)
    if history_text:
        (tmp / "memory" / "HISTORY.md").write_text(history_text)
    return tmp


# ── Test 1: _parse_backlog_task_from_memory finds first active priority ──────

def test_parse_backlog_finds_first_active():
    """_parse_backlog_task_from_memory returns Priority 9 (not Done)."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from nanobot.runtime.coordinator import _parse_backlog_task_from_memory

    with tempfile.TemporaryDirectory() as td:
        repo = _make_repo(Path(td), ACTIVE_BACKLOG_SAMPLE)
        result = _parse_backlog_task_from_memory(repo)
        assert result is not None, "Should find active Priority 9"
        assert result["priority"] == 9
        assert "Do something new" in result["title"]


def test_parse_backlog_skips_done_in_active():
    """_parse_backlog_task_from_memory skips Priority 10 [Done] in active section."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from nanobot.runtime.coordinator import _parse_backlog_task_from_memory

    # Only [Done] priorities in Active
    only_done = ACTIVE_BACKLOG_SAMPLE.replace(
        "### Priority 9: Do something new\nFile: scripts/new.py — implement it.",
        "### Priority 9: Do something new [Done]\nDone already.",
    )
    with tempfile.TemporaryDirectory() as td:
        repo = _make_repo(Path(td), only_done)
        result = _parse_backlog_task_from_memory(repo)
        assert result is None, "All active Done → should return None"


def test_parse_backlog_ignores_completed_section():
    """_parse_backlog_task_from_memory does NOT pick up items in ## Completed."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from nanobot.runtime.coordinator import _parse_backlog_task_from_memory

    with tempfile.TemporaryDirectory() as td:
        repo = _make_repo(Path(td), ALL_DONE_SAMPLE)
        result = _parse_backlog_task_from_memory(repo)
        assert result is None, "## Completed items must not be returned as active"


# ── Test 2: _move_priority_to_completed helper ───────────────────────────────

def test_move_priority_to_completed_removes_from_active():
    """_move_priority_to_completed moves block out of Active backlog."""
    import re
    # Import _move_priority_to_completed directly without full bridge module load
    import importlib.util
    bridge_path = Path(__file__).parent.parent / "scripts" / "eeepc_self_evolving_subagent_bridge.py"
    source = bridge_path.read_text()
    # Extract just the function body using exec in isolated namespace
    ns: dict = {"re": re, "Path": Path}
    # Parse out the two functions we need
    import ast, types
    tree = ast.parse(source)
    func_src_parts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
            "_move_priority_to_completed",
        ):
            func_src_parts.append(ast.get_source_segment(source, node))
    assert func_src_parts, "_move_priority_to_completed not found in bridge"
    exec("\n\n".join(func_src_parts), ns)  # noqa: S102
    _move_priority_to_completed = ns["_move_priority_to_completed"]

    text = ACTIVE_BACKLOG_SAMPLE
    title_escaped = re.escape("Do something new")
    updated = _move_priority_to_completed(
        text=text,
        title_escaped=title_escaped,
        backlog_title="Do something new",
        what_was_done="Implemented scripts/new.py",
    )
    # Block removed from Active section
    assert "### Priority 9: Do something new\nFile:" not in updated, \
        "Block must be removed from Active section"
    # Block appears in Completed section
    assert "## Completed" in updated
    completed_section = updated.split("## Completed")[1]
    assert "Do something new" in completed_section, \
        f"Title must appear in Completed. Got: {completed_section[:300]}"


# ── Test 3: cycle_logger prepend (newest-first) ──────────────────────────────

def test_cycle_logger_prepend_order():
    """cycle_logger.append_cycle_summary prepends newest entry first."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from scripts.cycle_logger import append_cycle_summary

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "memory").mkdir()

        append_cycle_summary(repo, "cycle-old001", "feat: old task", ["a.py"])
        append_cycle_summary(repo, "cycle-new002", "feat: new task", ["b.py"])

        hist = (repo / "memory" / "HISTORY.md").read_text()
        lines = [l for l in hist.splitlines() if l.strip()]
        assert "cycle-new002" in lines[0], f"Newest must be first, got: {lines[0]}"
        assert "cycle-old001" in lines[1], f"Older must be second, got: {lines[1]}"


def test_cycle_logger_no_duplicate():
    """cycle_logger skips duplicate cycle_id and file is unchanged."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from scripts.cycle_logger import append_cycle_summary

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "memory").mkdir()

        append_cycle_summary(repo, "cycle-dup", "feat: original")
        original = (repo / "memory" / "HISTORY.md").read_text()

        result = append_cycle_summary(repo, "cycle-dup", "feat: duplicate attempt")
        assert result is False
        assert (repo / "memory" / "HISTORY.md").read_text() == original


def test_memory_layout_has_both_sections():
    """Canonical MEMORY.md has both '## Active backlog' and '## Completed' sections."""
    memory_path = Path(__file__).parent.parent / "memory" / "MEMORY.md"
    text = memory_path.read_text()
    assert "## Active backlog" in text, "MEMORY.md must have ## Active backlog section"
    assert "## Completed" in text, "MEMORY.md must have ## Completed section"
    # Active section should NOT contain [Done] items for priorities 1-8
    active_section = text.split("## Completed")[0]
    assert "Priority 1:" not in active_section or "[Done]" not in active_section.split("Priority 1:")[1].split("###")[0]
