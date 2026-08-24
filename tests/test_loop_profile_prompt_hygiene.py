from __future__ import annotations

from pathlib import Path

from nanobot.agent.context import ContextBuilder


def test_loop_profile_skips_stale_memory_always_skill_but_keeps_catalogue(tmp_path: Path):
    prompt = ContextBuilder(tmp_path).build_system_prompt(loop_profile=True)

    assert "Always loaded into your context" not in prompt
    assert "You don't need to manage this" not in prompt
    assert "<name>memory</name>" in prompt


def test_loop_profile_uses_index_identity_and_neutral_role(tmp_path: Path):
    prompt = ContextBuilder(tmp_path).build_system_prompt(loop_profile=True)

    assert "memory/index.md (catalog; read facts on demand)" in prompt
    assert "MEMORY.md (write important facts here)" not in prompt
    assert "You are the autonomous improvement agent operating within a bounded engineering loop." in prompt
    assert "You are nanobot, a helpful AI assistant." not in prompt


def test_interactive_prompt_remains_legacy_identity_and_memory(tmp_path: Path):
    prompt = ContextBuilder(tmp_path).build_system_prompt()

    assert "You are nanobot, a helpful AI assistant." in prompt
    assert "MEMORY.md (write important facts here)" in prompt
    assert "<name>memory</name>" in prompt


def test_loop_profile_flag_does_not_change_interactive_prompt_bytes(tmp_path: Path):
    builder = ContextBuilder(tmp_path)
    first = builder.build_system_prompt()
    second = builder.build_system_prompt()

    assert first == second
