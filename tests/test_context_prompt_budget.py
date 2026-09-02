from __future__ import annotations

from nanobot.agent import context as context_module
from nanobot.agent.context import ContextBuilder


def _oversized_builder(tmp_path):
    builder = ContextBuilder(tmp_path)
    builder._get_identity = lambda loop_profile=False: "# identity"
    builder._load_bootstrap_files = lambda: "## AGENTS.md\n\n" + "bootstrap line\n" * 5000
    builder.skills.get_always_skills = lambda: ["memory"]
    builder.skills.load_skills_for_context = lambda names: "always skill content"
    builder.skills.build_skills_summary = lambda excluded_names=None: "<skills>\n  <skill><name>catalogue</name></skill>\n</skills>"
    builder.memory.get_memory_context = lambda loop=False: "## Long-term Memory\nremembered fact"
    return builder


def test_oversized_bootstrap_preserves_memory_and_skills_and_reports_drop(tmp_path, monkeypatch):
    messages: list[str] = []
    monkeypatch.setattr(
        context_module.logger,
        "warning",
        lambda message, *args: messages.append(message.format(*args)),
    )

    prompt = _oversized_builder(tmp_path).build_system_prompt()

    assert len(prompt) <= ContextBuilder.MAX_SYSTEM_PROMPT_CHARS
    assert "# Memory" in prompt
    assert "# Skills" in prompt
    assert "# Active Skills" in prompt
    assert prompt.endswith("\n") or prompt.splitlines()[-1] in {"</skills>", "remembered fact"}
    assert any("bootstrap" in message and "dropped" in message for message in messages)
    assert any("chars" in message for message in messages)


def test_bootstrap_configuration_contains_only_tracked_file():
    assert ContextBuilder.BOOTSTRAP_FILES == ["AGENTS.md"]
