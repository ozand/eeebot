"""Issue #1481: explicit FTS5 memory retrieval states and executor tool."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from nanobot.runtime import existence_index as ei


def _write(repo: Path, rel: str, text: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_unavailable_is_not_empty_and_tool_requires_blocked_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Written first: an index failure must never look like a real zero."""
    repo, state = tmp_path / "repo", tmp_path / "state"
    _write(repo, "memory/facts/one.md", "# One\nrare durable evidence\n")
    monkeypatch.setattr(ei, "_open_db", lambda *_: (_ for _ in ()).throw(OSError("broken")))

    result = ei.search_memory(state, repo, "durable evidence", limit=3)
    assert result["status"] == "unavailable"
    assert result["results"] == []
    assert result["reason"] == "index_unavailable"
    assert result["decision"] == "blocked"

    from nanobot.agent.tools.memory_search import MemorySearchTool
    payload = json.loads(asyncio.run(MemorySearchTool(repo, state).execute(query="durable evidence")))
    assert payload["status"] == "unavailable"
    assert payload["decision"] == "blocked"
    assert "do not treat this as zero matches" in payload["instruction"].lower()


def test_complete_zero_is_distinct_from_unavailable(tmp_path: Path):
    repo, state = tmp_path / "repo", tmp_path / "state"
    _write(repo, "memory/facts/one.md", "# One\nknown durable evidence\n")
    result = ei.search_memory(state, repo, "volcanic banana", limit=3)
    assert result["status"] == "complete"
    assert result["reason"] == "no_matches"
    assert result["results"] == []
    assert result["decision"] == "continue"


def test_partial_is_a_real_hard_limit_with_uneven_fixture(tmp_path: Path):
    repo, state = tmp_path / "repo", tmp_path / "state"
    for i in range(7):
        _write(repo, f"memory/facts/fact_{i}.md", f"# Fact {i}\nneedle topic detail unique-{i}\n")
    result = ei.search_memory(state, repo, "needle topic detail", limit=3)
    assert result["status"] == "partial"
    assert result["reason"] == "result_limit"
    assert result["returned"] == 3
    assert result["available"] >= 4
    assert len(result["results"]) == 3


def test_memory_index_is_kind_aware_incremental_and_retires_removed_file(tmp_path: Path):
    repo, state = tmp_path / "repo", tmp_path / "state"
    keep = _write(repo, "memory/facts/keep.md", "# Keep\nretained orchid phrase\n")
    gone = _write(repo, "memory/archive/gone.md", "# Gone\nretired zeppelin phrase\n")
    _write(repo, "memory/index.md", "generated catalogue must not be indexed\n")

    first = ei.reindex(state, repo)
    assert first["memory_indexed"] == 2
    assert first["memory_unchanged"] == 0
    second = ei.reindex(state, repo)
    assert second["memory_indexed"] == 0
    assert second["memory_unchanged"] == 2
    gone.unlink()
    third = ei.reindex(state, repo)
    assert third["memory_deactivated"] == 1

    con = sqlite3.connect(str(state / "existence_index" / "index.sqlite"))
    rows = con.execute("SELECT path, active FROM documents WHERE kind='memory' ORDER BY path").fetchall()
    con.close()
    assert rows == [("memory/archive/gone.md", 0), ("memory/facts/keep.md", 1)]
    assert ei.search_memory(state, repo, "zeppelin", limit=8)["results"] == []
    assert ei.search_memory(state, repo, "orchid", limit=8)["results"][0]["path"] == keep.relative_to(repo).as_posix()


def test_companion_evidence_mismatch_is_unavailable_not_a_hit(tmp_path: Path):
    repo, state = tmp_path / "repo", tmp_path / "state"
    _write(repo, "memory/facts/one.md", "# One\ncompanion evidence phrase\n")
    assert ei.search_memory(state, repo, "companion evidence", limit=3)["status"] == "complete"
    db = state / "existence_index" / "index.sqlite"
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT hash FROM documents WHERE kind='memory'").fetchone()
    con.execute("DELETE FROM content WHERE hash=?", row)
    con.commit(); con.close()

    result = ei.search_memory(state, repo, "companion evidence", limit=3, reindex_first=False)
    assert result["status"] == "unavailable"
    assert result["reason"] == "invalid_companion_evidence"
    assert result["results"] == []
    assert result["decision"] == "blocked"


def test_unsafe_or_unreadable_memory_source_makes_corpus_unavailable_without_retirement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, state = tmp_path / "repo", tmp_path / "state"
    good = _write(repo, "memory/facts/good.md", "# Good\nsafe phrase\n")
    assert ei.search_memory(state, repo, "safe phrase", limit=3)["status"] == "complete"
    original = Path.read_text

    def broken_read(path: Path, *args, **kwargs):
        if path == good:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", broken_read)
    result = ei.search_memory(state, repo, "safe phrase", limit=3)
    assert result["status"] == "unavailable"
    assert result["reason"] == "memory_corpus_unavailable"
    assert "memory" in result["notes"]


def test_tool_bounds_limit_query_and_snippets_and_returns_safe_paths(tmp_path: Path):
    repo, state = tmp_path / "repo", tmp_path / "state"
    _write(repo, "memory/facts/long.md", "# Long\n" + ("bounded needle content " * 100))
    from nanobot.agent.tools.memory_search import MemorySearchTool
    tool = MemorySearchTool(repo, state)
    schema = tool.parameters
    assert schema["properties"]["limit"]["maximum"] == 8
    payload = json.loads(asyncio.run(tool.execute(query="bounded needle content", limit=99)))
    assert payload["status"] in {"complete", "partial"}
    assert payload["limit"] == 8
    assert all(row["path"].startswith("memory/") and ".." not in row["path"] for row in payload["results"])
    assert all(len(row["snippet"]) <= 320 for row in payload["results"])
