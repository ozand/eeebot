"""ADR-028 rule 5 (#1812): ``ReadFileTool``'s ``on_day_file_read`` callback."""
from pathlib import Path

import pytest

from nanobot.agent.tools.filesystem import ReadFileTool


@pytest.mark.asyncio
async def test_callback_fires_with_the_day_stem_after_a_successful_read(tmp_path: Path):
    path = tmp_path / "diary" / "2026-09-20.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Diary\n", encoding="utf-8")
    seen = []
    tool = ReadFileTool(workspace=tmp_path, on_day_file_read=seen.append)

    result = await tool.execute(path="diary/2026-09-20.md")
    assert "Diary" in result
    assert seen == ["2026-09-20"]


@pytest.mark.asyncio
async def test_callback_does_not_fire_on_a_failed_read(tmp_path: Path):
    seen = []
    tool = ReadFileTool(workspace=tmp_path, on_day_file_read=seen.append)
    result = await tool.execute(path="diary/2026-09-20.md")
    assert result.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_callback_ignores_a_lookalike_file_outside_diary(tmp_path: Path):
    """A file named like a day but sitting under ``memory/`` is not a diary
    read -- the path must actually resolve under ``diary/`` (per
    ``day_diary.is_diary_path``), the filename alone is not enough."""
    path = tmp_path / "memory" / "2026-09-20.md"
    path.parent.mkdir(parents=True)
    path.write_text("not a diary", encoding="utf-8")
    seen = []
    tool = ReadFileTool(workspace=tmp_path, on_day_file_read=seen.append)
    await tool.execute(path="memory/2026-09-20.md")
    assert seen == []


@pytest.mark.asyncio
async def test_success_skill_and_diary_callbacks_are_independent(tmp_path: Path):
    diary_path = tmp_path / "diary" / "2026-09-20.md"
    diary_path.parent.mkdir(parents=True)
    diary_path.write_text("# Diary\n", encoding="utf-8")
    skills_seen, diary_seen = [], []
    tool = ReadFileTool(workspace=tmp_path, on_skill_read=skills_seen.append, on_day_file_read=diary_seen.append)

    await tool.execute(path="diary/2026-09-20.md")
    assert skills_seen == []
    assert diary_seen == ["2026-09-20"]


@pytest.mark.asyncio
async def test_diary_callback_exception_never_breaks_the_read(tmp_path: Path):
    path = tmp_path / "diary" / "2026-09-20.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Diary\n", encoding="utf-8")

    def boom(day: str) -> None:
        raise RuntimeError(f"instrumentation bug for {day}")

    tool = ReadFileTool(workspace=tmp_path, on_day_file_read=boom)
    result = await tool.execute(path="diary/2026-09-20.md")
    assert "Diary" in result


@pytest.mark.asyncio
async def test_no_diary_callback_configured_is_a_no_op(tmp_path: Path):
    path = tmp_path / "diary" / "2026-09-20.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Diary\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute(path="diary/2026-09-20.md")
    assert "Diary" in result
