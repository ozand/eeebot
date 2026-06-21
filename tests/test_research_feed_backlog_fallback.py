"""Tests for Issue #517: auto-seed backlog from research/feed.json when empty,
and _pick_candidate_from_research_feed coordinator fallback."""
from __future__ import annotations
import ast
import json
import re
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Bridge function extraction (avoid full loguru import) ─────────────────────

def _load_bridge_functions(*names: str):
    """Extract named functions from bridge script via AST without importing module."""
    bridge_path = Path(__file__).parent.parent / "scripts" / "eeepc_self_evolving_subagent_bridge.py"
    source = bridge_path.read_text()
    ns: dict = {"re": re, "Path": Path, "json": json}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            func_src = ast.get_source_segment(source, node)
            exec(func_src, ns)  # noqa: S102
    return ns


# ── Test 1: _active_backlog_is_empty ─────────────────────────────────────────

def test_active_backlog_is_empty_when_all_done():
    ns = _load_bridge_functions("_active_backlog_is_empty")
    text = """\
## Active backlog — pick one each session

### Priority 9: Task A [Done]
Done.

### Priority 10: Task B [Done]
Done too.

## Completed
"""
    assert ns["_active_backlog_is_empty"](text) is True


def test_active_backlog_not_empty_when_undone():
    ns = _load_bridge_functions("_active_backlog_is_empty")
    text = """\
## Active backlog — pick one each session

### Priority 9: Task A
Not done yet.

### Priority 10: Task B [Done]
Done.
"""
    assert ns["_active_backlog_is_empty"](text) is False


# ── Test 2: _auto_seed_backlog_from_research ──────────────────────────────────

def _make_env(tmp: Path, memory_text: str, feed_entries: list | None = None) -> tuple:
    """Create test repo structure with optional research feed."""
    repo = tmp / "eeebot-self-evolving"
    state = tmp / "state"
    (repo / "memory").mkdir(parents=True)
    (state / "research").mkdir(parents=True)
    memory_path = repo / "memory" / "MEMORY.md"
    memory_path.write_text(memory_text)

    if feed_entries is not None:
        feed = {
            "schema_version": "research-feed-v1",
            "cycle_id": "cycle-test",
            "goal_id": "goal-bootstrap",
            "entry_count": len(feed_entries),
            "entries": feed_entries,
        }
        (state / "research" / "feed.json").write_text(json.dumps(feed))

    return repo, state, memory_path


ALL_DONE_MEMORY = """\
# Project Memory

## Active backlog — pick one each session

<!-- BACKLOG_START -->
<!-- BACKLOG_END -->

---

## Completed

### Priority 8: Old task [Done]
Done.
"""

FEED_ENTRIES = [
    {
        "id": "exploit-dashboard",
        "title": "Exploit and expand dashboard improvements",
        "hypothesis": "Dashboard improvements drive operator value",
        "acceptance": "Extend scripts/eeebot_dashboard.py with new metrics",
        "action": "Add 3 new metrics to dashboard CLI output",
    },
    {
        "id": "inspect-pass-streak",
        "title": "Inspect PASS streak for new bounded improvement",
        "acceptance": "Identify a concrete bounded improvement from the PASS streak",
        "action": "Analyze recent reward trend and propose next experiment",
    },
]


def test_auto_seed_adds_priorities_when_empty():
    """Auto-seed inserts 2 new Priority blocks when backlog is empty."""
    ns = _load_bridge_functions(
        "_active_backlog_is_empty",
        "_auto_seed_backlog_from_research",
    )
    with tempfile.TemporaryDirectory() as td:
        repo, state, memory_path = _make_env(Path(td), ALL_DONE_MEMORY, FEED_ENTRIES)
        result = ns["_auto_seed_backlog_from_research"](repo, ALL_DONE_MEMORY, memory_path)
        assert result is True, "Should return True when seeding"
        new_text = memory_path.read_text()
        # Two new Priority blocks added
        priorities = re.findall(r"### Priority (\d+):", new_text)
        active_section = new_text.split("## Completed")[0]
        active_priorities = re.findall(r"### Priority (\d+):", active_section)
        assert len(active_priorities) >= 2, f"Expected 2 new priorities, got: {active_priorities}"
        assert "Exploit and expand dashboard" in new_text
        assert "Inspect PASS streak" in new_text


def test_auto_seed_skips_when_backlog_has_undone():
    """Auto-seed does nothing when Active backlog has undone priorities."""
    ns = _load_bridge_functions(
        "_active_backlog_is_empty",
        "_auto_seed_backlog_from_research",
    )
    memory_with_active = ALL_DONE_MEMORY.replace(
        "<!-- BACKLOG_END -->",
        "### Priority 9: Active task\nNot done yet.\n<!-- BACKLOG_END -->",
    )
    with tempfile.TemporaryDirectory() as td:
        repo, state, memory_path = _make_env(Path(td), memory_with_active, FEED_ENTRIES)
        result = ns["_auto_seed_backlog_from_research"](repo, memory_with_active, memory_path)
        assert result is False, "Should skip when backlog has active priorities"
        assert memory_path.read_text() == memory_with_active


def test_auto_seed_graceful_when_no_feed():
    """Auto-seed returns False gracefully when feed.json missing."""
    ns = _load_bridge_functions(
        "_active_backlog_is_empty",
        "_auto_seed_backlog_from_research",
    )
    with tempfile.TemporaryDirectory() as td:
        repo, state, memory_path = _make_env(Path(td), ALL_DONE_MEMORY, feed_entries=None)
        result = ns["_auto_seed_backlog_from_research"](repo, ALL_DONE_MEMORY, memory_path)
        assert result is False


# ── Test 3: coordinator _pick_candidate_from_research_feed ───────────────────

def test_pick_candidate_from_feed_returns_top_entry():
    """_pick_candidate_from_research_feed returns top entry as backlog_task."""
    from nanobot.runtime.coordinator import _pick_candidate_from_research_feed

    with tempfile.TemporaryDirectory() as td:
        state_root = Path(td)
        (state_root / "research").mkdir()
        feed = {
            "schema_version": "research-feed-v1",
            "entry_count": 2,
            "entries": FEED_ENTRIES,
        }
        (state_root / "research" / "feed.json").write_text(json.dumps(feed))

        result = _pick_candidate_from_research_feed(state_root)
        assert result is not None
        assert "Exploit" in result["title"]
        assert result["priority"] == 99
        assert result["source"] == "research_feed"


def test_pick_candidate_returns_none_when_no_feed():
    """_pick_candidate_from_research_feed returns None when feed missing."""
    from nanobot.runtime.coordinator import _pick_candidate_from_research_feed

    with tempfile.TemporaryDirectory() as td:
        result = _pick_candidate_from_research_feed(Path(td))
        assert result is None


def test_pick_candidate_returns_none_when_feed_empty():
    """_pick_candidate_from_research_feed returns None when feed has no entries."""
    from nanobot.runtime.coordinator import _pick_candidate_from_research_feed

    with tempfile.TemporaryDirectory() as td:
        state_root = Path(td)
        (state_root / "research").mkdir()
        feed = {"schema_version": "research-feed-v1", "entry_count": 0, "entries": []}
        (state_root / "research" / "feed.json").write_text(json.dumps(feed))

        result = _pick_candidate_from_research_feed(state_root)
        assert result is None
