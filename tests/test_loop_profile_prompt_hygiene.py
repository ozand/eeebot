from __future__ import annotations

from pathlib import Path

from nanobot.agent.context import ContextBuilder


def test_loop_profile_skips_stale_memory_always_skill_and_the_catalogue_too(tmp_path: Path):
    """#1857: the resident catalogue is gone from the loop profile entirely
    -- 'memory' (a builtin, not excluded from the old catalogue, only from
    get_always_skills) no longer appears via that route either."""
    prompt = ContextBuilder(tmp_path).build_system_prompt(loop_profile=True)

    assert "Always loaded into your context" not in prompt
    assert "You don't need to manage this" not in prompt
    assert "- memory: " not in prompt
    assert "(nanobot/skills/memory/SKILL.md)" not in prompt
    assert "<name>memory</name>" not in prompt


def test_loop_profile_uses_index_identity_and_neutral_role(tmp_path: Path):
    """#1725: the loop profile's runtime block names the memory index path
    (a workspace fact) but no longer carries any role sentence at all — the
    old loop role text moved out of code entirely (ADR-022 rule 2); it is
    the operator's to state, in IDENTITY.md/SOUL.md, not code's to author."""
    prompt = ContextBuilder(tmp_path).build_system_prompt(loop_profile=True)

    assert "memory/index.md (catalog; read facts on demand)" in prompt
    assert "MEMORY.md (write important facts here)" not in prompt
    assert "You are the autonomous improvement agent operating within a bounded engineering loop." not in prompt
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
