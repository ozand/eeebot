"""#1865: the planner's task-writing contract is release-owned and non-resident."""
from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent import skills
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.runtime import gate
from nanobot.runtime.role_prompt import build_role_system_prompt

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "nanobot" / "skills" / "task-writing" / "SKILL.md"


def test_task_writing_is_a_release_skill_and_cannot_be_a_loop_mutation():
    assert SKILL.is_file()
    assert "task-writing" in skills._RELEASE_OWNED_SKILL_NAMES
    path = "nanobot/skills/task-writing/SKILL.md"
    assert gate._validate_mutation_surfaces([path]) == [
        f"file outside allowed paths {gate._ALLOWED_PATH_PREFIXES}: {path}"
    ]


def test_contract_is_not_resident_in_the_planner_prompt():
    skill = SKILL.read_text(encoding="utf-8")
    prompt, _meta = build_role_system_prompt("planner", release_root=REPO)

    assert "must not be able to satisfy its own criterion" in skill
    assert "must not be able to satisfy its own criterion" not in prompt
    assert str(SKILL) not in prompt
    assert "harness has already" in prompt.lower()


def test_task_writing_contract_names_external_dor_dod_requirements():
    text = SKILL.read_text(encoding="utf-8")

    for required in (
        "iterations_planned",
        "metric",
        "script_exit_zero",
        "test_count_increase",
        "file_exists",
        "outside the loop's mutable commit surfaces",
        "no allowlisted metric",
    ):
        assert required in text


@pytest.mark.asyncio
async def test_only_a_complete_skill_read_satisfies_the_contract_callback(tmp_path: Path):
    skill = tmp_path / "release" / "nanobot" / "skills" / "task-writing" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("line one\nline two\n", encoding="utf-8")
    complete: list[Path] = []
    tool = ReadFileTool(
        workspace=tmp_path,
        allowed_dir=tmp_path,
        on_complete_skill_read=complete.append,
    )

    partial = await tool.execute(path=str(skill), limit=1)
    assert "Showing lines" in partial
    assert complete == []

    tail = await tool.execute(path=str(skill), offset=2)
    assert "End of file" in tail
    assert complete == []

    full = await tool.execute(path=str(skill))
    assert "End of file" in full
    assert complete == [skill.resolve()]
