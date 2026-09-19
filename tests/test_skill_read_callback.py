from pathlib import Path

import pytest

from nanobot.agent.tools.filesystem import ReadFileTool


@pytest.mark.asyncio
async def test_callback_receives_resolved_path_only_after_success(tmp_path: Path):
    path = tmp_path / "skills" / "review" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("hello", encoding="utf-8")
    seen = []
    tool = ReadFileTool(workspace=tmp_path, on_skill_read=seen.append)

    result = await tool.execute(path="skills/review/SKILL.md")
    assert "hello" in result
    assert seen == [path.resolve()]

    result = await tool.execute(path="skills/missing/SKILL.md")
    assert result.startswith("Error:")
    assert seen == [path.resolve()]


@pytest.mark.asyncio
async def test_unrelated_skill_path_can_be_rejected_by_harness(tmp_path: Path):
    path = tmp_path / "docs" / "review" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("lookalike", encoding="utf-8")
    seen = []
    tool = ReadFileTool(workspace=tmp_path, on_skill_read=seen.append)
    await tool.execute(path="docs/review/SKILL.md")
    assert seen == [path.resolve()]
    workspace_skills = (tmp_path / "skills").resolve()
    with pytest.raises(ValueError):
        seen[0].relative_to(workspace_skills)


# ---------------------------------------------------------------------------
# #1767 -- the failure mirror
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failure_callback_fires_on_a_missing_skill(tmp_path: Path):
    """The read that returns "File not found" is the one #1759 could not
    see: before this callback it left no trace anywhere, so a cycle that
    asked for a skill and was refused looked exactly like a cycle that
    never asked."""
    missed = []
    tool = ReadFileTool(workspace=tmp_path, on_skill_read_failed=missed.append)

    result = await tool.execute(path="skills/skip-when-done/SKILL.md")
    assert result.startswith("Error:")
    assert missed == ["skills/skip-when-done/SKILL.md"]


@pytest.mark.asyncio
async def test_failure_callback_receives_the_requested_path_not_a_resolved_one(tmp_path: Path):
    """A failed lookup has no resolved path to report; the spelling the
    caller used is the whole evidence, so it is passed through verbatim."""
    missed = []
    tool = ReadFileTool(workspace=tmp_path, on_skill_read_failed=missed.append)
    await tool.execute(path="./skills/Not-A-Skill/SKILL.md")
    assert missed == ["./skills/Not-A-Skill/SKILL.md"]


@pytest.mark.asyncio
async def test_failure_callback_ignores_non_skill_reads(tmp_path: Path):
    missed = []
    tool = ReadFileTool(workspace=tmp_path, on_skill_read_failed=missed.append)
    result = await tool.execute(path="docs/nope.md")
    assert result.startswith("Error:")
    assert missed == []


@pytest.mark.asyncio
async def test_failure_callback_fires_when_the_path_is_a_directory(tmp_path: Path):
    (tmp_path / "skills" / "SKILL.md").mkdir(parents=True)
    missed = []
    tool = ReadFileTool(workspace=tmp_path, on_skill_read_failed=missed.append)
    result = await tool.execute(path="skills/SKILL.md")
    assert result.startswith("Error: Not a file")
    assert missed == ["skills/SKILL.md"]


@pytest.mark.asyncio
async def test_success_and_failure_callbacks_are_mutually_exclusive(tmp_path: Path):
    """One read produces at most one of the two rows, never both -- the
    pair is only readable as a total if they partition the attempts."""
    path = tmp_path / "skills" / "review" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("hello", encoding="utf-8")
    seen, missed = [], []
    tool = ReadFileTool(
        workspace=tmp_path, on_skill_read=seen.append, on_skill_read_failed=missed.append,
    )

    await tool.execute(path="skills/review/SKILL.md")
    assert (len(seen), len(missed)) == (1, 0)

    await tool.execute(path="skills/absent/SKILL.md")
    assert (len(seen), len(missed)) == (1, 1)


@pytest.mark.asyncio
async def test_failure_callback_exception_never_breaks_the_read(tmp_path: Path):
    def boom(_requested: str) -> None:
        raise RuntimeError("instrumentation bug")

    tool = ReadFileTool(workspace=tmp_path, on_skill_read_failed=boom)
    result = await tool.execute(path="skills/absent/SKILL.md")
    assert result.startswith("Error: File not found")


@pytest.mark.asyncio
async def test_no_failure_callback_configured_is_a_no_op(tmp_path: Path):
    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute(path="skills/absent/SKILL.md")
    assert result.startswith("Error: File not found")
