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
