"""#1732: the loop-profile skills catalogue renders one line per skill
instead of an XML ``<skill>`` block, removing the ~200-char-per-skill
wrapper (66% of a 10,109-char catalogue measured on 33 skills, 2026-09-18).

Interactive sessions are unaffected: ``compact`` defaults to ``False`` and
their XML output is unchanged, byte-for-byte, for the same fixture.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.skills import SkillsLoader

_MISSING_BIN = "definitely-not-a-real-binary-1732"


def _skill(
    root: Path, name: str, description: str = "test", *, requires_bin: str | None = None,
) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = ""
    if requires_bin:
        meta = f'\nmetadata: {json.dumps({"nanobot": {"requires": {"bins": [requires_bin]}}})}'
    path.write_text(
        f"---\nname: {name}\ndescription: {description}{meta}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Acceptance criterion 1: three skills, one line each, one header, no <skill
# ---------------------------------------------------------------------------


def test_compact_catalogue_three_skills_one_line_each_with_header(tmp_path: Path):
    workspace = tmp_path / "skills"
    for name, desc in (
        ("alpha", "First skill for the fixture."),
        ("beta", "Second skill for the fixture."),
        ("gamma", "Third skill for the fixture."),
    ):
        _skill(workspace, name, desc)

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins")
    summary = loader.build_skills_summary(compact=True)

    assert "<skill" not in summary
    lines = summary.splitlines()
    assert lines[0] == SkillsLoader._COMPACT_HEADER
    assert lines[1] == ""
    entry_lines = [ln for ln in lines[2:] if ln]
    assert entry_lines == [
        "- alpha: First skill for the fixture.",
        "- beta: Second skill for the fixture.",
        "- gamma: Third skill for the fixture.",
    ]


# ---------------------------------------------------------------------------
# Acceptance criterion 2: unavailable skill's requires note stays inline
# ---------------------------------------------------------------------------


def test_compact_unavailable_skill_shows_requires_note_inline(tmp_path: Path):
    workspace = tmp_path / "skills"
    _skill(workspace, "needs-tool", "A skill needing a missing CLI.", requires_bin=_MISSING_BIN)

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins")
    summary = loader.build_skills_summary(compact=True)

    assert f"- needs-tool: A skill needing a missing CLI. (requires CLI: {_MISSING_BIN})" in summary


# ---------------------------------------------------------------------------
# Builtins surviving excluded_names get their real path (not under the
# workspace layout rule the header states once).
# ---------------------------------------------------------------------------


def test_compact_builtin_surviving_exclusion_shows_real_path(tmp_path: Path):
    builtins = tmp_path / "builtins"
    _skill(builtins, "kept-builtin", "Survives exclusion.")
    _skill(builtins, "excluded-builtin", "Should not appear.")

    loader = SkillsLoader(tmp_path, builtin_skills_dir=builtins)
    summary = loader.build_skills_summary(excluded_names=["excluded-builtin"], compact=True)

    assert "- kept-builtin: Survives exclusion. (nanobot/skills/kept-builtin/SKILL.md)" in summary
    assert "excluded-builtin" not in summary


def test_compact_workspace_skill_has_no_path_suffix(tmp_path: Path):
    """Workspace skills are under the header's stated rule -- no per-line
    path repeats it (that repetition is exactly the wrapper #1732 removes)."""
    _skill(tmp_path / "skills", "instance-thing", "An instance skill.")

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins")
    summary = loader.build_skills_summary(excluded_names=[], compact=True)

    assert "- instance-thing: An instance skill." in summary
    assert "(skills/instance-thing/SKILL.md)" not in summary
    assert "(nanobot/skills/instance-thing/SKILL.md)" not in summary


# ---------------------------------------------------------------------------
# Acceptance criterion 3: interactive (non-compact) output is byte-identical
# to the pre-#1732 XML shape for the same fixture.
# ---------------------------------------------------------------------------


def test_interactive_xml_output_unchanged_for_a_two_skill_fixture(tmp_path: Path):
    workspace = tmp_path / "skills"
    _skill(workspace, "alpha", "First skill.")
    _skill(workspace, "beta", "Second skill.")

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins")

    default_call = loader.build_skills_summary()
    explicit_noncompact = loader.build_skills_summary(compact=False)
    assert default_call == explicit_noncompact

    expected = (
        "<skills>\n"
        '  <skill available="true" source="workspace">\n'
        "    <name>alpha</name>\n"
        "    <description>First skill.</description>\n"
        "    <location>skills/alpha/SKILL.md</location>\n"
        "  </skill>\n"
        '  <skill available="true" source="workspace">\n'
        "    <name>beta</name>\n"
        "    <description>Second skill.</description>\n"
        "    <location>skills/beta/SKILL.md</location>\n"
        "  </skill>\n"
        "</skills>"
    )
    assert default_call == expected


def test_loop_profile_flag_alone_does_not_change_interactive_call_bytes(tmp_path: Path):
    """ContextBuilder.build_system_prompt() with no loop_profile keeps
    compact=False -- the format switch is driven only by loop_profile."""
    workspace = tmp_path / "skills"
    _skill(workspace, "alpha", "First skill.")
    builder = ContextBuilder(tmp_path)
    builder.skills = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins")

    interactive = builder.build_system_prompt()
    assert "<name>alpha</name>" in interactive
    assert "- alpha:" not in interactive


# ---------------------------------------------------------------------------
# Measurement: 33 realistic skills, lines vs xml catalogue chars.
# ---------------------------------------------------------------------------


def test_measure_33_skills_lines_vs_xml_chars(tmp_path: Path):
    workspace = tmp_path / "skills"
    for i in range(33):
        # 30-char name, 70-char description -- the issue's stated fixture shape.
        name = f"skill-{i:03d}-" + ("n" * 30)
        name = name[:30]
        desc = f"Do the bounded thing for case {i:03d}, padded to seventy chars xx"
        desc = (desc + ("y" * 70))[:70]
        _skill(workspace, name, desc)

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins")
    xml_summary = loader.build_skills_summary(excluded_names=[])
    lines_summary = loader.build_skills_summary(excluded_names=[], compact=True)

    xml_chars = len(xml_summary)
    lines_chars = len(lines_summary)
    # Reported in the PR body verbatim; bounds here just guard against a
    # regression in either direction, not pin the exact byte count.
    assert xml_chars > 8_000, xml_chars
    assert lines_chars < 6_000, lines_chars
    assert lines_chars < xml_chars * 0.6, (lines_chars, xml_chars)
