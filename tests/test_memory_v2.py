from nanobot.agent.context import ContextBuilder
from pathlib import Path


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
