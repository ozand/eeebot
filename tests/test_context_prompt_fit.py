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
    are fixed-size stand-ins so the cap arithmetic is legible.

    Interactive (``loop_profile=False``) only, unchanged since before #1725
    — the loop profile no longer reads ``_get_identity``/
    ``_load_bootstrap_files`` at all; see :func:`_loop_builder`.
    """
    builder = ContextBuilder(tmp_path)
    builder._get_identity = lambda loop_profile=False: "# identity\n\nYou are the loop executor."
    builder._load_bootstrap_files = lambda: "## AGENTS.md\n\n# Instance AGENTS.md\n\nintro paragraph.\n\n" + bootstrap_body
    builder.skills.get_always_skills = lambda: []
    builder.skills.load_skills_for_context = lambda names: ""
    builder.skills.build_skills_summary = lambda excluded_names=None, compact=False: "<skills>\n" + "  <skill><name>s</name></skill>\n" * catalogue_lines + "</skills>"
    builder.memory.get_memory_context = lambda *, loop=False, max_chars=4000: "## Long-term Memory\n" + "remembered fact\n" * memory_lines
    return builder


#: #1725: the loop profile's fixed (non-workspace) section keys, in build
#: order, excluding "agents" (the one variable, droppable-carrying block in
#: :func:`_loop_builder`) and "skills_catalogue" (computed from the others).
LOOP_FIXED_SECTION_NAMES = ("identity", "soul", "goals", "user", "operating", "memory", "runtime")
LOOP_SECTION_NAMES = ("identity", "soul", "goals", "user", "operating", "agents", "skills_catalogue", "memory", "runtime")


def _loop_builder(tmp_path, agents_md_body: str, *, catalogue_lines: int = 40, memory_lines: int = 20) -> ContextBuilder:
    """A loop-profile builder whose only variable is the AGENTS.md
    (workspace/"agents") text — the one block the loop's strict fit may
    mark ``## `` sub-sections droppable in (:data:`ContextBuilder.
    _LOOP_DROPPABLE_SECTION`). The five release blocks, skills catalogue,
    and memory are fixed-size stand-ins, same spirit as :func:`_builder`.

    ``_load_ontology_blocks`` is stubbed directly (bypassing
    ``ContextBuilder.load_block``'s own per-file cap) so these tests
    exercise the GENERIC fit/strict/droppable/uniform-trim ladder only —
    the per-block loader itself (missing markers, truncation notices) has
    its own dedicated tests in ``tests/test_ontology_loader.py``.
    """
    builder = ContextBuilder(tmp_path)

    def _stub_ontology_blocks():
        sections = [
            ("identity", "## IDENTITY.md\n\nstub identity."),
            ("soul", "## SOUL.md\n\nstub soul."),
            ("goals", "## goals.md\n\nstub goals."),
            ("user", "## USER.md\n\nstub user."),
            ("operating", "## OPERATING.md\n\nstub operating."),
            ("agents", "## AGENTS.md\n\n# Instance AGENTS.md\n\nintro paragraph.\n\n" + agents_md_body),
        ]
        return sections, [], []

    builder._load_ontology_blocks = _stub_ontology_blocks
    builder._get_identity = lambda loop_profile=False: "## Runtime\nstub runtime facts."
    builder.skills.get_always_skills = lambda: []
    builder.skills.load_skills_for_context = lambda names: ""
    builder.skills.build_skills_summary = lambda excluded_names=None, compact=False: "<skills>\n" + "  <skill><name>s</name></skill>\n" * catalogue_lines + "</skills>"
    builder.memory.get_memory_context = lambda *, loop=False, max_chars=4000: "## Long-term Memory\n" + "remembered fact\n" * memory_lines
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
    agents_md = (
        _section("Working knowledge", 6)                    # critical, small, sits BEFORE the droppables
        + _section("Big optional appendix", 60, droppable=True)
        + _section("Small optional note", 5, droppable=True)
        + _section("Standard test runner", 8)               # critical, LAST in the file — pre-fix casualty
    )
    builder = _loop_builder(tmp_path, agents_md)
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
    """Direct strict callers retain the old refusal contract."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 4_000)
    agents_md = _section("Working knowledge", 40) + _section("Optional", 4, droppable=True) + _section("Standard test runner", 40)
    builder = _loop_builder(tmp_path, agents_md)
    with pytest.raises(SystemPromptOverflowError) as info:
        builder.build_system_prompt(loop_profile=True)
    exc = info.value
    assert exc.cap == 4_000 and exc.over_by > 0
    assert set(exc.sections) == set(LOOP_SECTION_NAMES)
    assert [d["section"] for d in exc.dropped] == ["## Optional"], "the droppable one was removed before giving up"
    assert ContextBuilder.DROPPABLE_MARKER in str(exc) and ContextBuilder.SYSTEM_PROMPT_CAP_ENV in str(exc)
    assert builder.last_fit["dropped"] == exc.dropped, "the record is left for the caller even on failure"


def test_strict_never_trims_lines_inside_a_section(tmp_path, monkeypatch):
    """Position-based line trimming is the defect; direct strict mode must not fall back to it."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 3_000)
    builder = _loop_builder(tmp_path, _section("Working knowledge", 80))
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


def test_under_cap_nothing_is_dropped_in_loop_profile(tmp_path):
    builder = _loop_builder(tmp_path, _section("Working knowledge", 3) + _section("Optional", 2, droppable=True))
    prompt = builder.build_system_prompt(loop_profile=True)
    assert "## Optional" in prompt and builder.last_fit["dropped"] == []


def test_under_cap_nothing_is_dropped_interactive(tmp_path):
    builder = _builder(tmp_path, _section("Working knowledge", 3) + _section("Optional", 2, droppable=True))
    prompt = builder.build_system_prompt(loop_profile=False)
    assert "## Optional" in prompt and builder.last_fit["dropped"] == []


def test_cap_env_override_is_the_operator_lever(tmp_path, monkeypatch):
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 3_000)
    builder = _loop_builder(tmp_path, _section("Working knowledge", 80))
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
    body = _section("Working knowledge", 3) + optional

    loop_builder = _loop_builder(tmp_path / "loop", body)
    loop_builder.build_system_prompt(loop_profile=True)
    assert loop_builder.last_fit["droppable_reserve_chars"] == len(optional)

    interactive_builder = _builder(tmp_path / "interactive", body)
    interactive_builder.build_system_prompt(loop_profile=False)
    assert interactive_builder.last_fit["droppable_reserve_chars"] == len(optional)


def test_droppable_reserve_chars_shrinks_by_exact_drop(tmp_path, monkeypatch):
    """#1313: one of two declared-droppable sections goes; the reserve left is
    the other one's exact size, not a re-derived estimate."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 6_000)
    small_optional = _section("Small optional note", 5, droppable=True)
    agents_md = (
        _section("Working knowledge", 6)
        + _section("Big optional appendix", 60, droppable=True)
        + small_optional
        + _section("Standard test runner", 8)
    )
    builder = _loop_builder(tmp_path, agents_md)
    builder.build_system_prompt(loop_profile=True)
    fit = builder.last_fit
    assert fit["dropped"] == [{"section": "## Big optional appendix", "chars": pytest.approx(len(_section("Big optional appendix", 60, droppable=True))), "how": "declared-droppable"}]
    assert fit["droppable_reserve_chars"] == len(small_optional), "reserve is what is LEFT to drop, not what already went"


def test_droppable_reserve_chars_is_zero_after_exhaustion(tmp_path, monkeypatch):
    """#1313: at the moment the cap gives up, every declared-droppable section
    has already been removed — the reserve that motivated this issue is gone,
    and the ledger must say so as 0, not omit the key."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 4_000)
    agents_md = _section("Working knowledge", 40) + _section("Optional", 4, droppable=True) + _section("Standard test runner", 40)
    builder = _loop_builder(tmp_path, agents_md)
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
    mgr.release_root = None
    mgr._excluded_skill_names = []
    mgr.system_context = "# Immutable operator charter\n\ncharter"
    prompt = mgr._build_subagent_prompt()
    assert isinstance(mgr.last_prompt_fit, dict) and mgr.last_prompt_fit["strict"] is True
    assert mgr.last_prompt_fit["rung"] == "uniform_trim"
    # #1379: the operator charter is appended AFTER the fit and is deliberately
    # outside the cap, so the capped portion is what the ladder bounds — not
    # the returned string. Asserting the whole string would be asserting the
    # charter away.
    charter_tail = ContextBuilder.SECTION_SEPARATOR + mgr.system_context
    assert prompt.endswith(charter_tail)
    assert len(prompt) - len(charter_tail) <= 3_000
    assert mgr.last_prompt_fit["chars"] <= 3_000

    monkeypatch.setenv(ContextBuilder.SYSTEM_PROMPT_CAP_ENV, "60000")
    prompt = mgr._build_subagent_prompt()
    assert prompt.endswith("# Immutable operator charter\n\ncharter") and "## Working knowledge" in prompt
    assert mgr.last_prompt_fit["dropped"] == []


def test_catalogue_bound_keeps_complete_entries_and_records_named_omissions(tmp_path, monkeypatch):
    builder = _builder(tmp_path, "", catalogue_lines=4, memory_lines=2)
    section = "# Skills\n\n" + "\n".join(
        f'  <skill available="true" source="workspace">\n'
        f'    <name>skill-{i}</name>\n'
        f'    <description>description-{i}</description>\n'
        f'    <location>skills/skill-{i}/SKILL.md</location>\n'
        "  </skill>"
        for i in range(4)
    )
    matches = list(context_module.re.finditer(r"<skill\b[^>]*>.*?</skill>", section, context_module.re.DOTALL))
    marker = builder.SKILLS_CATALOGUE_TRUNCATION_MARKER.format(
        count=3, chars=sum(len(match.group(0)) for match in matches[1:]),
    )
    budget = len(section[:matches[0].start()]) + len(matches[0].group(0)) + len(marker)
    bounded, evidence = builder._bound_skills_catalogue(section, budget)
    assert evidence["status"] == "bounded"
    assert evidence["load"]["status"] == "complete"
    assert evidence["start"]["budget"] == budget
    assert evidence["total_count"] == 4
    assert evidence["retained_count"] == 1
    assert evidence["omitted_count"] == 3
    assert evidence["omitted_chars"] == sum(len(match.group(0)) for match in matches[1:])
    assert evidence["omitted_names"] == ["skill-1", "skill-2", "skill-3"]
    assert evidence["sweep"]["status"] == "bounded"
    assert "skills catalogue truncated" in bounded
    assert bounded.count("<skill ") == 1
    assert bounded.count("</skill>") == 1


def test_catalogue_budget_uses_live_fixed_floor(tmp_path, monkeypatch):
    builder = _loop_builder(tmp_path, "", catalogue_lines=1, memory_lines=1)
    builder.skills.build_skills_summary = lambda excluded_names=None, compact=False: (
        '<skill available="true"><name>catalogue</name></skill>'
    )
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 3_000)
    builder.build_system_prompt(loop_profile=True)
    first_fit = dict(builder.last_fit)
    first_budget = first_fit["skills_catalogue"]["budget"]
    # #1725: the floor is every fixed section EXCEPT skills_catalogue itself
    # — the six ontology blocks plus memory and runtime, all non-empty for
    # this builder's stub content.
    expected_floor = sum(first_fit["sections"][name] for name in LOOP_FIXED_SECTION_NAMES + ("agents",))
    expected_floor += len(LOOP_FIXED_SECTION_NAMES + ("agents",)) * len(builder.SECTION_SEPARATOR)
    assert first_budget == 3_000 - expected_floor
    builder.memory.get_memory_context = lambda *, loop=False, max_chars=4000: "M" * 2000
    builder.build_system_prompt(loop_profile=True)
    assert builder.last_fit["skills_catalogue"]["budget"] < first_budget


def test_fair_budgets_are_keyed_on_length_not_position():
    """Permuting the entries must permute the budgets identically.

    This is the property that separates the ladder from positional
    truncation: where an entry sits in the list decides nothing. A tail-slice
    implementation would pass a "does it fit" assertion but fail this one.
    """
    lengths = [10, 4_000, 40, 4_000]
    budgets = ContextBuilder._fair_budgets(lengths, 2_000)
    reversed_budgets = ContextBuilder._fair_budgets(lengths[::-1], 2_000)
    assert budgets == reversed_budgets[::-1]
    # The two short entries fit outright; the two long ones share what is left
    # and receive the SAME budget as each other, not a prefix-first split.
    assert budgets[0] == 10 and budgets[2] == 40
    assert budgets[1] == budgets[3]
    assert sum(budgets) <= 2_000


def test_uniform_trim_gives_over_budget_entries_the_same_allowance(tmp_path):
    """Two entries of very different length get the same budget when both overflow."""
    builder = _builder(tmp_path, "")
    sections = [("short", "s" * 5_000), ("long", "l" * 50_000)]
    trimmed, shortfall = builder._uniform_trim(sections, 6_000)
    kept = dict(trimmed)
    assert len(kept["short"]) == len(kept["long"]), (
        "a 10x length difference must not buy a larger allowance"
    )
    assert shortfall > 0
    assert builder.TRIM_NOTE.split("{")[0].strip() in kept["long"], (
        "the loss must be visible in the artifact the model reads"
    )


def test_trimmed_entry_reports_the_characters_it_lost(tmp_path):
    builder = _builder(tmp_path, "")
    sections = [("only", "x" * 10_000)]
    trimmed, _ = builder._uniform_trim(sections, 1_000)
    body = dict(trimmed)["only"]
    assert len(body) <= 1_000
    lost = 10_000 - (1_000 - len(builder.TRIM_NOTE.format(n=0)))
    assert "[trimmed" in body and "chars]" in body
    assert str(lost)[:2] in body, "the note names how much went, not just that something did"
