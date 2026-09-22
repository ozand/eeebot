"""ADR-033 / #1863: release-owned operator skills cannot be shadowed or mutated."""
from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.runtime import gate, skills_index

_SELECTED = ("eeebot-agent-work-review", "memory-lookup", "run-tests")


def _skill(root: Path, name: str, description: str, *, author: bool = False) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    field = 'author: "eeebot"\n' if author else ""
    path.write_text(f"---\nname: {name}\ndescription: {description}\n{field}---\n\n# {name}\n", encoding="utf-8")


def test_loader_lists_both_roots_and_release_wins_name_collision(tmp_path: Path):
    builtins = tmp_path / "release"
    _skill(builtins, "run-tests", "release test procedure")
    _skill(tmp_path / "skills", "run-tests", "instance shadow")
    _skill(tmp_path / "skills", "loop-only", "loop procedure")

    loader = SkillsLoader(tmp_path, builtin_skills_dir=builtins)
    skills = loader.list_skills(filter_unavailable=False)

    assert [(s["name"], s["source"]) for s in skills] == [
        ("run-tests", "release"), ("loop-only", "workspace"),
    ]
    assert loader.load_skill("run-tests") == (builtins / "run-tests" / "SKILL.md").read_text(encoding="utf-8")


def test_gate_rejects_release_owned_skill_mutation():
    """ADR-033: release-tree skills are outside loop mutation authority."""
    path = "nanobot/skills/run-tests/SKILL.md"
    assert gate._validate_mutation_surfaces([path]) == [
        f"file outside allowed paths {gate._ALLOWED_PATH_PREFIXES}: {path}"
    ]
    blocked, violations, _tier = gate._classify_mutation_surface([path])
    assert blocked == []
    assert violations == [f"file outside allowed paths {gate._ALLOWED_PATH_PREFIXES} and not in runtime slice: {path}"]


def test_skills_index_labels_release_and_workspace_sources(tmp_path: Path, monkeypatch):
    builtins = tmp_path / "release"
    _skill(builtins, "memory-lookup", "release lookup")
    _skill(tmp_path / "skills", "loop-only", "loop procedure")
    monkeypatch.setattr("nanobot.agent.skills.BUILTIN_SKILLS_DIR", builtins)

    content = skills_index.render_skills_index(tmp_path)

    assert "- memory-lookup: release lookup (release: nanobot/skills/memory-lookup/SKILL.md)" in content
    assert "- loop-only: loop procedure" in content


def test_release_skill_is_not_hidden_by_instance_retirement_sidecar(tmp_path: Path, monkeypatch):
    release = tmp_path / "release"
    _skill(release, "memory-lookup", "release lookup")
    state = tmp_path / "state" / "demand"
    state.mkdir(parents=True)
    (state / "skill_retirement_cooldown.json").write_text(
        '{"paths":{"skills/memory-lookup/SKILL.md":{"status":"verified_absent"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(tmp_path / "state"))

    summary = SkillsLoader(tmp_path, builtin_skills_dir=release).build_skills_summary()

    assert '<name>memory-lookup</name>' in summary


def test_selected_operator_skills_are_release_owned_and_readable():
    release = Path(__file__).parents[1] / "nanobot" / "skills"
    loader = SkillsLoader(Path("/nonexistent-workspace"), builtin_skills_dir=release)

    for name in _SELECTED:
        text = (release / name / "SKILL.md").read_text(encoding="utf-8")
        assert "author:" not in text
        assert loader.load_skill(name) == text


@pytest.mark.asyncio
async def test_release_owned_skill_is_readable_inside_workspace_restriction(tmp_path: Path):
    release = tmp_path / "release"
    _skill(release, "memory-lookup", "release lookup")
    tool = ReadFileTool(
        workspace=tmp_path,
        allowed_dir=tmp_path,
        extra_allowed_dirs=[release],
    )

    result = await tool.execute(path=str(release / "memory-lookup" / "SKILL.md"))

    assert "release lookup" in result


def test_compact_index_cost_is_reported_before_and_after_migration(tmp_path: Path):
    builtins = tmp_path / "release"
    workspace = tmp_path / "skills"
    for name in _SELECTED:
        _skill(workspace, name, f"{name} procedure", author=True)
        _skill(builtins, name, f"{name} procedure")

    before = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "empty").build_skills_summary(compact=True)
    after = SkillsLoader(tmp_path, builtin_skills_dir=builtins).build_skills_summary(compact=True)

    assert len(before) > 0
    assert len(after) > len(before)
    assert all("(release: nanobot/skills/" in after for _ in _SELECTED)
