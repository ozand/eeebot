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


def test_loop_memory_context_keeps_resident_rules_and_drops_whole_entries(tmp_path: Path):
    """#1443: small caps cannot remove the rules block or split an entry."""
    from nanobot.agent.memory import MemoryStore

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    index_file = mem_dir / "index.md"

    lines = [
        "# Memory index", "", "## Facts (memory/facts/)", "",
        "* [Identity](facts/identity.md)",
        "* [Write target: workspace](facts/write-target.md)",
        "* [DO NOT touch](facts/do-not-touch.md)",
        "* [Rules](facts/rules.md)",
        "* [Key paths on host](facts/key-paths.md)",
    ]
    lines.extend(f"- [Old Fact {i}](facts/old_{i}.md) — Older context fact {i}" for i in range(100))
    lines.append("- [Brand New Fact](facts/fresh.md) — Crucial latest discovered fact")
    content = "\n".join(lines) + "\n"
    index_file.write_text(content, encoding="utf-8")
    assert len(content) > 4000

    store = MemoryStore(tmp_path)
    ctx = store.get_memory_context(loop=True, max_chars=4000)

    assert "[Identity]" in ctx
    assert "[Write target: workspace]" in ctx
    assert "[DO NOT touch]" in ctx
    assert "[Rules]" in ctx
    assert "[Key paths on host]" in ctx
    assert "Crucial latest discovered fact" in ctx
    assert "Brand New Fact" in ctx
    assert "Old Fact 0" not in ctx
    assert "[trimmed" not in ctx
    assert store.last_index_fit["dropped_entries"] > 0
    assert store.last_index_fit["resident_chars"] <= len(ctx)


def test_context_fit_records_memory_index_drop_details(tmp_path: Path):
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    index = [
        "# Memory index", "", "## Facts (memory/facts/)", "",
        "* [Identity](facts/identity.md)", "* [Write target: workspace](facts/write-target.md)",
        "* [DO NOT touch](facts/do-not-touch.md)", "* [Rules](facts/rules.md)",
        "* [Key paths on host](facts/key-paths.md)",
    ] + [f"- [Filler {i}](facts/filler_{i}.md) — {'x' * 40}" for i in range(100)]
    (mem_dir / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    builder = ContextBuilder(tmp_path)
    builder.skills.get_always_skills = lambda: []
    builder.skills.load_skills_for_context = lambda names: ""
    builder.skills.build_skills_summary = lambda excluded_names=None: ""
    prompt = builder.build_system_prompt(loop_profile=True)
    fit = builder.last_fit
    assert "[Identity]" in prompt and "[Rules]" in prompt
    assert fit["memory_index"]["dropped_entries"] > 0
    assert fit["memory_index"]["resident_chars"] > 0
