"""#1300: the system-prompt cap must never choose surviving instructions by
position, and its choices must never be silent.

Pre-fix, ``_fit_system_prompt`` trimmed the bootstrap (AGENTS.md) from the end
at line boundaries. On the host that removed 11,688 of 22,972 chars — eleven
``##`` sections including ``## Working knowledge`` — from every executor
prompt from 2026-09-01 on, with a journal WARNING as the only trace. Here the
loop profile is strict: only sections the operator marked droppable may go,
whole and largest first; anything else that does not fit raises.
"""
from __future__ import annotations

import pytest

from nanobot.agent import context as context_module
from nanobot.agent.context import ContextBuilder, SystemPromptOverflowError

MARK = ContextBuilder.DROPPABLE_MARKER


def _section(title: str, lines: int, *, droppable: bool = False) -> str:
    body = "".join(f"{title} line {i} is standing guidance for the executor.\n" for i in range(lines))
    return f"## {title}\n\n" + (f"{MARK}\n" if droppable else "") + body + "\n"


def _builder(tmp_path, bootstrap_body: str, *, catalogue_lines: int = 40, memory_lines: int = 20) -> ContextBuilder:
    """A builder whose only variable is the bootstrap text; the other sections
    are fixed-size stand-ins so the cap arithmetic is legible."""
    builder = ContextBuilder(tmp_path)
    builder._get_identity = lambda loop_profile=False: "# identity\n\nYou are the loop executor."
    builder._load_bootstrap_files = lambda: "## AGENTS.md\n\n# Instance AGENTS.md\n\nintro paragraph.\n\n" + bootstrap_body
    builder.skills.get_always_skills = lambda: []
    builder.skills.load_skills_for_context = lambda names: ""
    builder.skills.build_skills_summary = lambda excluded_names=None: "<skills>\n" + "  <skill><name>s</name></skill>\n" * catalogue_lines + "</skills>"
    builder.memory.get_memory_context = lambda loop=False: "## Long-term Memory\n" + "remembered fact\n" * memory_lines
    return builder


def test_marker_literal_is_the_one_the_instance_repo_carries():
    """ozand/eeebot-self-evolving#186 wrote this exact string into AGENTS.md;
    changing it here silently makes every declared section critical again."""
    assert ContextBuilder.DROPPABLE_MARKER == "<!-- prompt-fit: droppable -->"


def test_split_keeps_every_character_and_names_sections():
    text = "## AGENTS.md\n\n# Title\n\nintro\n\n" + _section("Alpha", 2) + _section("Beta", 3, droppable=True)
    units = ContextBuilder._split_bootstrap_sections(text)
    assert "".join(t for _, t in units) == text
    assert [h for h, _ in units] == ["## AGENTS.md", "## Alpha", "## Beta"]


def test_strict_drops_only_declared_sections_largest_first_and_records_them(tmp_path, monkeypatch):
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 6_000)
    bootstrap = (
        _section("Working knowledge", 6)                    # critical, small, sits BEFORE the droppables
        + _section("Big optional appendix", 60, droppable=True)
        + _section("Small optional note", 5, droppable=True)
        + _section("Standard test runner", 8)               # critical, LAST in the file — pre-fix casualty
    )
    builder = _builder(tmp_path, bootstrap)
    prompt = builder.build_system_prompt(loop_profile=True)

    assert len(prompt) <= 6_000
    assert "## Working knowledge" in prompt and "## Standard test runner" in prompt, "critical sections survive regardless of position"
    assert "## Big optional appendix" not in prompt, "the largest declared-droppable section goes first"
    assert "## Small optional note" in prompt, "a droppable section is not dropped when the prompt already fits"
    assert "# Memory" in prompt and "<skills>" in prompt
    fit = builder.last_fit
    assert fit["strict"] is True and fit["cap"] == 6_000 and fit["chars"] == len(prompt)
    assert fit["dropped"] == [{"section": "## Big optional appendix", "chars": pytest.approx(len(_section("Big optional appendix", 60, droppable=True))), "how": "declared-droppable"}]


def test_strict_refuses_when_critical_sections_do_not_fit(tmp_path, monkeypatch):
    """The decision recorded for #1300: the cap never drops a critical section."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 4_000)
    bootstrap = _section("Working knowledge", 40) + _section("Optional", 4, droppable=True) + _section("Standard test runner", 40)
    builder = _builder(tmp_path, bootstrap)
    with pytest.raises(SystemPromptOverflowError) as info:
        builder.build_system_prompt(loop_profile=True)
    exc = info.value
    assert exc.cap == 4_000 and exc.over_by > 0
    assert set(exc.sections) == {"identity", "bootstrap", "skills_catalogue", "memory"}
    assert [d["section"] for d in exc.dropped] == ["## Optional"], "the droppable one was removed before giving up"
    assert ContextBuilder.DROPPABLE_MARKER in str(exc) and ContextBuilder.SYSTEM_PROMPT_CAP_ENV in str(exc)
    assert builder.last_fit["dropped"] == exc.dropped, "the record is left for the caller even on failure"


def test_strict_never_trims_lines_inside_a_section(tmp_path, monkeypatch):
    """Position-based line trimming is the defect; strict mode must not fall back to it."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 3_000)
    builder = _builder(tmp_path, _section("Working knowledge", 80))
    with pytest.raises(SystemPromptOverflowError):
        builder.build_system_prompt(loop_profile=True)


def test_non_strict_keeps_the_interactive_behaviour_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 3_000)
    messages: list[str] = []
    monkeypatch.setattr(context_module.logger, "warning", lambda message, *args: messages.append(message.format(*args)))
    builder = _builder(tmp_path, _section("Working knowledge", 80))
    prompt = builder.build_system_prompt(loop_profile=False)
    assert len(prompt) <= 3_000 and "# Memory" in prompt
    assert builder.last_fit["strict"] is False
    assert builder.last_fit["dropped"][0]["section"] == "bootstrap" and builder.last_fit["dropped"][0]["how"] == "line-trim"
    assert any("bootstrap" in m and "dropped" in m for m in messages)


def test_under_cap_nothing_is_dropped_in_either_mode(tmp_path):
    builder = _builder(tmp_path, _section("Working knowledge", 3) + _section("Optional", 2, droppable=True))
    for strict in (True, False):
        prompt = builder.build_system_prompt(loop_profile=strict)
        assert "## Optional" in prompt and builder.last_fit["dropped"] == []


def test_cap_env_override_is_the_operator_lever(tmp_path, monkeypatch):
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 3_000)
    builder = _builder(tmp_path, _section("Working knowledge", 80))
    with pytest.raises(SystemPromptOverflowError):
        builder.build_system_prompt(loop_profile=True)
    monkeypatch.setenv(ContextBuilder.SYSTEM_PROMPT_CAP_ENV, "40000")
    prompt = builder.build_system_prompt(loop_profile=True)
    assert "## Working knowledge" in prompt and builder.last_fit["cap"] == 40000 and builder.last_fit["dropped"] == []
    monkeypatch.setenv(ContextBuilder.SYSTEM_PROMPT_CAP_ENV, "not-a-number")
    assert builder._cap() == 3_000, "a malformed override falls back to the default, never to unbounded"


def test_droppable_reserve_chars_is_full_total_when_nothing_dropped(tmp_path):
    """#1313: under the cap, the reserve is every declared-droppable section
    still standing — the fuse length an operator can read without waiting
    for the cap to bite."""
    optional = _section("Optional", 2, droppable=True)
    builder = _builder(tmp_path, _section("Working knowledge", 3) + optional)
    for strict in (True, False):
        builder.build_system_prompt(loop_profile=strict)
        assert builder.last_fit["droppable_reserve_chars"] == len(optional)


def test_droppable_reserve_chars_shrinks_by_exact_drop(tmp_path, monkeypatch):
    """#1313: one of two declared-droppable sections goes; the reserve left is
    the other one's exact size, not a re-derived estimate."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 6_000)
    small_optional = _section("Small optional note", 5, droppable=True)
    bootstrap = (
        _section("Working knowledge", 6)
        + _section("Big optional appendix", 60, droppable=True)
        + small_optional
        + _section("Standard test runner", 8)
    )
    builder = _builder(tmp_path, bootstrap)
    builder.build_system_prompt(loop_profile=True)
    fit = builder.last_fit
    assert fit["dropped"] == [{"section": "## Big optional appendix", "chars": pytest.approx(len(_section("Big optional appendix", 60, droppable=True))), "how": "declared-droppable"}]
    assert fit["droppable_reserve_chars"] == len(small_optional), "reserve is what is LEFT to drop, not what already went"


def test_droppable_reserve_chars_is_zero_after_exhaustion(tmp_path, monkeypatch):
    """#1313: at the moment the cap gives up, every declared-droppable section
    has already been removed — the reserve that motivated this issue is gone,
    and the ledger must say so as 0, not omit the key."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 4_000)
    bootstrap = _section("Working knowledge", 40) + _section("Optional", 4, droppable=True) + _section("Standard test runner", 40)
    builder = _builder(tmp_path, bootstrap)
    with pytest.raises(SystemPromptOverflowError) as info:
        builder.build_system_prompt(loop_profile=True)
    assert info.value.droppable_reserve_chars == 0
    assert builder.last_fit["droppable_reserve_chars"] == 0, "the record left for the caller matches the exception, even on failure"


def test_subagent_prompt_is_strict_and_exposes_the_fit(tmp_path, monkeypatch):
    from nanobot.agent import subagent as subagent_module

    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 3_000)
    (tmp_path / "AGENTS.md").write_text("# Instance\n\n" + _section("Working knowledge", 80), encoding="utf-8")
    mgr = subagent_module.SubagentManager.__new__(subagent_module.SubagentManager)
    mgr.workspace = tmp_path
    mgr._excluded_skill_names = []
    mgr.system_context = "# Immutable operator charter\n\ncharter"
    with pytest.raises(SystemPromptOverflowError):
        mgr._build_subagent_prompt()
    assert isinstance(mgr.last_prompt_fit, dict) and mgr.last_prompt_fit["strict"] is True

    monkeypatch.setenv(ContextBuilder.SYSTEM_PROMPT_CAP_ENV, "60000")
    prompt = mgr._build_subagent_prompt()
    assert prompt.endswith("# Immutable operator charter\n\ncharter") and "## Working knowledge" in prompt
    assert mgr.last_prompt_fit["dropped"] == []
