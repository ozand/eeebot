from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.skills import SkillsLoader


def _skill(root: Path, name: str, *, always: bool = False, description: str = "test") -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    marker = "\nalways: true" if always else ""
    path.write_text(f"---\nname: {name}\ndescription: {description}{marker}\n---\n\n# {name}\n", encoding="utf-8")


def test_workspace_always_skill_is_not_auto_loaded(tmp_path: Path):
    _skill(tmp_path / "skills", "untrusted", always=True)
    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins")
    assert loader.get_always_skills() == []


def test_builtin_always_skill_is_loaded(tmp_path: Path):
    builtins = tmp_path / "builtins"
    _skill(builtins, "trusted", always=True)
    loader = SkillsLoader(tmp_path, builtin_skills_dir=builtins)
    assert loader.get_always_skills() == ["trusted"]


def test_excluded_skills_absent_but_workspace_skill_visible_before_memory(tmp_path: Path):
    _skill(tmp_path / "skills", "instance-review")
    for name in ("weather", "tmux", "clawhub", "keep"):
        _skill(tmp_path / "builtins", name)
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text("M" * 30000, encoding="utf-8")
    builder = ContextBuilder(tmp_path)
    builder.skills = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins")

    prompt = builder.build_system_prompt(excluded_skill_names=["weather", "tmux", "clawhub"])
    assert "instance-review" in prompt
    assert "<name>keep</name>" in prompt
    assert "<name>weather</name>" not in prompt
    assert "<name>tmux</name>" not in prompt
    assert "<name>clawhub</name>" not in prompt
    assert prompt.index("# Skills") < prompt.index("# Memory")


def test_workspace_skill_locations_are_relative_and_builtin_locations_absolute(tmp_path: Path):
    workspace = tmp_path / "instance"
    builtins = tmp_path / "builtins"
    _skill(workspace / "skills", "workspace-skill", description="trigger workspace")
    _skill(builtins, "builtin-skill", description="trigger builtin")
    summary = SkillsLoader(workspace, builtin_skills_dir=builtins).build_skills_summary()
    assert "<location>skills/workspace-skill/SKILL.md</location>" in summary
    assert str(builtins / "builtin-skill" / "SKILL.md") in summary
