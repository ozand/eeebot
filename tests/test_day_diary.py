"""ADR-028 rule 1 (#1810): the day diary's path, format, and append discipline.

Foundation only -- no instruction text (#1812), no bridge integration
(#1811/#1813), no fold job. This file proves: the path is
``diary/YYYY-MM-DD.md``; a fresh day file has a header and exactly one
marker; appending through the marker preserves every prior entry, in
order, across many sequential writes; a doubled marker makes ``edit_file``
refuse rather than silently double-write; ``write_file`` against any
``diary/`` path is refused outright; ``diary/`` is a recognised mutation
surface; and no diary content reaches the assembled system prompt
(ADR-028 rule 4).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nanobot.agent.tools.filesystem import EditFileTool, WriteFileTool
from nanobot.runtime import day_diary
from nanobot.runtime.mutation_policy import MUTATION_POLICY


# ---------------------------------------------------------------------------
# path and format
# ---------------------------------------------------------------------------


def test_diary_relpath_is_date_keyed_under_diary_dir():
    assert day_diary.diary_relpath(date(2026, 9, 20)) == "diary/2026-09-20.md"


def test_diary_relpath_defaults_to_todays_utc_date():
    path = day_diary.diary_relpath()
    assert path.startswith("diary/")
    assert path.endswith(".md")
    assert len(path) == len("diary/YYYY-MM-DD.md")


def test_the_day_boundary_is_decided_in_exactly_one_place():
    """ADR-029 (#1831): the clock choice must be visible and singular so
    the migration is a one-line edit -- pinned at the source level: the
    ONLY datetime.now()/date.today() call in this module lives inside
    _today(); diary_relpath() and new_day_file() must never gain their own
    (that would make #1831's migration a hunt instead of a one-line edit)."""
    import re

    src = Path(day_diary.__file__).read_text(encoding="utf-8")
    clock_calls = list(re.finditer(r"datetime\.now\([^)]*\)|date\.today\(\)", src))
    assert clock_calls, "expected at least one clock call (inside _today())"

    today_start = src.index("def _today(")
    today_end = src.index("def diary_relpath(")
    outside_today = [m.group(0) for m in clock_calls if not (today_start <= m.start() < today_end)]
    assert not outside_today, f"a clock call bypasses _today(): {outside_today}"


def test_diary_is_a_recognised_versioned_commit_surface():
    """AC 1: diary/ appears in the mutation policy's allowed targets."""
    assert "diary/" in MUTATION_POLICY.commit_path_prefixes
    assert "diary/" in MUTATION_POLICY.commit_surfaces
    # Not a release-owned or forbidden path -- it is a normal, loop-writable
    # surface (append-only in practice, enforced by the tool, not by being
    # unwritable).
    assert "diary/" not in MUTATION_POLICY.forbidden_dirs


def test_new_day_file_has_a_header_and_exactly_one_marker():
    """AC 2: a day file is created with a header and exactly one marker
    line, by a harness-owned helper -- not by the loop free-handing the
    format."""
    content = day_diary.new_day_file(date(2026, 9, 20))
    assert content.startswith("# Diary")
    assert "2026-09-20" in content
    assert content.count(day_diary.DIARY_MARKER) == 1


def test_new_day_file_has_exactly_one_plan_block_with_a_placeholder():
    """#1852: a fresh day file carries an empty, fixed-position plan block
    the first planning session of the day can find and replace."""
    content = day_diary.new_day_file(date(2026, 9, 20))
    assert content.count(day_diary.PLAN_BEGIN) == 1
    assert content.count(day_diary.PLAN_END) == 1
    assert content.index(day_diary.PLAN_BEGIN) < content.index(day_diary.PLAN_END)
    # positioned before the entries marker, not mixed into the entries list
    assert content.index(day_diary.PLAN_END) < content.index(day_diary.DIARY_MARKER)


def test_format_doc_states_intent_not_report():
    """AC (format doc): an entry records intent, not a report; the ledger
    holds what happened. Stated in the file itself, the one place every
    reader (loop or operator) actually sees the rule."""
    content = day_diary.new_day_file(date(2026, 9, 20))
    lowered = content.lower()
    assert "intent" in lowered
    assert "not a report" in lowered
    assert "ledger" in lowered


# ---------------------------------------------------------------------------
# the safe append -- ninety cycles, one file, one marker
# ---------------------------------------------------------------------------


def test_append_entry_preserves_prior_entries_and_keeps_one_marker():
    content = day_diary.new_day_file(date(2026, 9, 20))
    content = day_diary.append_entry(content, "09:00 first entry")
    assert content.count(day_diary.DIARY_MARKER) == 1
    assert "09:00 first entry" in content


def test_ninety_sequential_appends_keep_every_entry_in_order_no_loss():
    """AC 4: ninety entries in sequence through the marker-replacement
    route, all present, in order, nothing lost."""
    content = day_diary.new_day_file(date(2026, 9, 20))
    entries = [f"cycle {i:03d}: attempting task {i}" for i in range(90)]
    for entry in entries:
        content = day_diary.append_entry(content, entry)

    assert content.count(day_diary.DIARY_MARKER) == 1
    positions = [content.index(entry) for entry in entries]
    assert positions == sorted(positions), "entries out of order"
    for entry in entries:
        assert content.count(entry) == 1


def test_append_entry_refuses_a_malformed_file_with_no_marker():
    with pytest.raises(ValueError):
        day_diary.append_entry("# Diary\n\nno marker here.\n", "an entry")


def test_append_entry_refuses_a_file_with_the_marker_twice():
    """The pure function mirrors edit_file's own ambiguity rule: a doubled
    marker cannot be safely appended to (which entry is "the" marker?)."""
    doubled = f"# Diary\n\n{day_diary.DIARY_MARKER}\n\nstray text\n\n{day_diary.DIARY_MARKER}\n"
    with pytest.raises(ValueError):
        day_diary.append_entry(doubled, "an entry")


# ---------------------------------------------------------------------------
# #1852: the plan block -- replaced, not appended, at a fixed position
# ---------------------------------------------------------------------------


def test_set_plan_block_replaces_the_placeholder():
    content = day_diary.new_day_file(date(2026, 9, 20))
    content = day_diary.set_plan_block(content, "read the record, do X next")
    assert content.count(day_diary.PLAN_BEGIN) == 1
    assert content.count(day_diary.PLAN_END) == 1
    assert "read the record, do X next" in content
    assert "(no plan recorded yet)" not in content


def test_set_plan_block_replaces_a_prior_plan_rather_than_appending():
    """Only the latest plan survives -- REPLACE, not append (#1852: 'the
    diary carries the current plan', not a growing history of plans)."""
    content = day_diary.new_day_file(date(2026, 9, 20))
    content = day_diary.set_plan_block(content, "first plan")
    content = day_diary.set_plan_block(content, "second plan")
    assert content.count("second plan") == 1
    assert "first plan" not in content
    assert content.count(day_diary.PLAN_BEGIN) == 1
    assert content.count(day_diary.PLAN_END) == 1


def test_set_plan_block_preserves_entries_appended_around_it():
    content = day_diary.new_day_file(date(2026, 9, 20))
    content = day_diary.append_entry(content, "Implement and commit: task one")
    content = day_diary.set_plan_block(content, "do task two next")
    content = day_diary.append_entry(content, "Implement and commit: task two")
    assert "task one" in content
    assert "do task two next" in content
    assert content.count("Implement and commit: task two") == 1
    assert content.count(day_diary.DIARY_MARKER) == 1


def test_set_plan_block_refuses_a_file_with_no_markers():
    with pytest.raises(ValueError):
        day_diary.set_plan_block("# Diary\n\nno plan markers here.\n", "a plan")


def test_set_plan_block_refuses_a_file_with_the_begin_marker_twice():
    doubled = (
        f"# Diary\n\n{day_diary.PLAN_BEGIN}\nold\n{day_diary.PLAN_END}\n\n"
        f"{day_diary.PLAN_BEGIN}\nstray\n{day_diary.PLAN_END}\n"
    )
    with pytest.raises(ValueError):
        day_diary.set_plan_block(doubled, "a plan")


# ---------------------------------------------------------------------------
# edit_file against the marker: the real tool route, and its refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_file_appends_through_the_marker_route(tmp_path: Path):
    """The loop's real append route: edit_file(old_text=MARKER,
    new_text=entry+MARKER) against a freshly created day file."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    diary_file = workspace / day_diary.diary_relpath(date(2026, 9, 20))
    diary_file.parent.mkdir(parents=True)
    diary_file.write_text(day_diary.new_day_file(date(2026, 9, 20)), encoding="utf-8")

    tool = EditFileTool(workspace=workspace)
    entry = "09:15 attempting to connect the orphaned validator to its runner"
    result = await tool.execute(
        path=str(diary_file),
        old_text=day_diary.DIARY_MARKER,
        new_text=f"{entry}\n{day_diary.DIARY_MARKER}",
    )
    assert "Successfully" in result
    written = diary_file.read_text(encoding="utf-8")
    assert entry in written
    assert written.count(day_diary.DIARY_MARKER) == 1


@pytest.mark.asyncio
async def test_edit_file_refuses_rather_than_double_writes_a_doubled_marker(tmp_path: Path):
    """AC 5: if a diary file somehow contains the marker twice, edit_file
    returns its ambiguity warning rather than writing -- the failure mode
    is a refusal, never a silent double write."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    diary_file = workspace / day_diary.diary_relpath(date(2026, 9, 20))
    diary_file.parent.mkdir(parents=True)
    doubled = (
        f"# Diary — 2026-09-20\n\n{day_diary.DIARY_MARKER}\n\n"
        f"stray duplicate below\n\n{day_diary.DIARY_MARKER}\n"
    )
    diary_file.write_text(doubled, encoding="utf-8")
    before = diary_file.read_text(encoding="utf-8")

    tool = EditFileTool(workspace=workspace)
    result = await tool.execute(
        path=str(diary_file),
        old_text=day_diary.DIARY_MARKER,
        new_text=f"new entry\n{day_diary.DIARY_MARKER}",
    )
    assert "appears 2 times" in result or "Warning" in result
    after = diary_file.read_text(encoding="utf-8")
    assert after == before, "a refused edit must not have written anything"


# ---------------------------------------------------------------------------
# write_file: refused unconditionally under diary/
# ---------------------------------------------------------------------------


def test_is_diary_path_matches_the_directory_and_nothing_else(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert day_diary.is_diary_path(workspace / "diary" / "2026-09-20.md", workspace)
    assert day_diary.is_diary_path(workspace / "diary" / "nested" / "x.md", workspace)
    assert not day_diary.is_diary_path(workspace / "memory" / "diary-note.md", workspace)
    assert not day_diary.is_diary_path(workspace / "AGENTS.md", workspace)


@pytest.mark.asyncio
async def test_write_file_refuses_a_new_diary_path(tmp_path: Path):
    """AC 6: write_file against a diary/ path is refused, error names the
    marker route, and nothing is written."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / day_diary.diary_relpath(date(2026, 9, 20))

    tool = WriteFileTool(workspace=workspace)
    result = await tool.execute(path=str(target), content="freehand content")
    assert "Error" in result
    assert "diary/" in result
    assert "edit_file" in result
    assert day_diary.DIARY_MARKER in result
    assert not target.exists(), "write_file must not have created the file"


@pytest.mark.asyncio
async def test_write_file_refuses_an_existing_diary_path_too(tmp_path: Path):
    """The refusal is unconditional -- it also protects a file that
    already exists, not only path creation."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / day_diary.diary_relpath(date(2026, 9, 20))
    target.parent.mkdir(parents=True)
    original = day_diary.new_day_file(date(2026, 9, 20))
    target.write_text(original, encoding="utf-8")

    tool = WriteFileTool(workspace=workspace)
    result = await tool.execute(path=str(target), content="freehand overwrite")
    assert "Error" in result
    assert target.read_text(encoding="utf-8") == original, "write_file must not have touched the file"


@pytest.mark.asyncio
async def test_write_file_still_works_outside_diary(tmp_path: Path):
    """The refusal is scoped to diary/ -- every other path is unaffected."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "scripts" / "tool.py"

    tool = WriteFileTool(workspace=workspace)
    result = await tool.execute(path=str(target), content="print('hi')\n")
    assert "Successfully" in result
    assert target.read_text(encoding="utf-8") == "print('hi')\n"


# ---------------------------------------------------------------------------
# ADR-028 rule 4: never in the prompt
# ---------------------------------------------------------------------------


def test_day_diary_module_never_mentioned_by_context_or_subagent_source():
    """day_diary.py has no reader anywhere in prompt assembly -- checked at
    the source level, so a later edit that imports it there fails loudly."""
    from nanobot.agent import context as context_module
    from nanobot.agent import subagent as subagent_module

    for module in (context_module, subagent_module):
        src = Path(str(module.__file__)).read_text(encoding="utf-8")
        assert "day_diary" not in src

    # And the reverse never happened either: day_diary.py imports neither
    # assembly module (its own docstring names them descriptively, which
    # is fine -- what matters is no `import`/`from` statement pulling them
    # in).
    diary_src = Path(str(day_diary.__file__)).read_text(encoding="utf-8")
    import_lines = [ln for ln in diary_src.splitlines() if ln.startswith(("import ", "from "))]
    assert not any("nanobot.agent" in ln for ln in import_lines), import_lines


def test_no_diary_content_reaches_the_assembled_system_prompt(tmp_path: Path):
    """AC 8: even with a diary file sitting in the workspace, the assembled
    loop-profile system prompt never carries its content -- diary/ is not
    one of the files context.py loads (IDENTITY/SOUL/goals/USER/OPERATING/
    AGENTS.md/memory/skills/runtime/scorecard/position), and this proves
    that by construction rather than by absence of a test."""
    from nanobot.agent.context import ContextBuilder

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# Instance AGENTS.md\n\nRepo layout.", encoding="utf-8")
    diary_dir = workspace / "diary"
    diary_dir.mkdir()
    marker_text = "UNMISTAKABLE_DIARY_ENTRY_7c2a: attempting to refactor the gateway today"
    diary_file = diary_dir / day_diary.diary_relpath(date(2026, 9, 20)).split("/", 1)[1]
    diary_file.write_text(day_diary.append_entry(day_diary.new_day_file(date(2026, 9, 20)), marker_text), encoding="utf-8")

    prompt = ContextBuilder(workspace).build_system_prompt(loop_profile=True)
    assert marker_text not in prompt
    assert day_diary.DIARY_MARKER not in prompt
