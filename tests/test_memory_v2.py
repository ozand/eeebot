from __future__ import annotations

from pathlib import Path

from nanobot.agent.context import ContextBuilder


def test_loop_context_uses_index_only(tmp_path: Path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "index.md").write_text("* [Fact](facts/fact.md) - desc\n", encoding="utf-8")
    (tmp_path / "memory" / "MEMORY.md").write_text("FULL LEGACY BODY", encoding="utf-8")
    prompt = ContextBuilder(tmp_path).build_system_prompt(loop_profile=True)
    assert "facts/fact.md" in prompt
    assert "FULL LEGACY BODY" not in prompt


def test_interactive_context_keeps_legacy_memory(tmp_path: Path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "index.md").write_text("INDEX ONLY", encoding="utf-8")
    (tmp_path / "memory" / "MEMORY.md").write_text("FULL LEGACY BODY", encoding="utf-8")
    prompt = ContextBuilder(tmp_path).build_system_prompt()
    assert "FULL LEGACY BODY" in prompt


def test_loop_memory_context_reads_tail_and_shows_freshly_added_fact(tmp_path: Path):
    """Issue #1041 Part 1: when index.md exceeds max_chars, newest facts at the tail are visible."""
    from nanobot.agent.memory import MemoryStore

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    index_file = mem_dir / "index.md"

    # Fill index.md with older facts totaling > 4000 chars
    lines = [f"- [Old Fact {i}](facts/old_{i}.md) — Older context fact {i}" for i in range(100)]
    lines.append("- [Brand New Fact](facts/fresh.md) — Crucial latest discovered fact")
    content = "\n".join(lines) + "\n"
    index_file.write_text(content, encoding="utf-8")
    assert len(content) > 4000

    store = MemoryStore(tmp_path)
    ctx = store.get_memory_context(loop=True, max_chars=4000)

    # Tail should be included, so the fresh fact is visible
    assert "Crucial latest discovered fact" in ctx
    assert "Brand New Fact" in ctx
    # Old head facts should be truncated out
    assert "Old Fact 0" not in ctx
