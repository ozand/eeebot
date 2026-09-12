import json
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


def _retirement_sidecar(state: Path, paths: dict[str, object]) -> None:
    target = state / "demand" / "skill_retirement_cooldown.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"schema_version": "skill-retirement-cooldown-v1", "paths": paths}), encoding="utf-8")


def test_verified_absent_skill_is_not_in_catalogue(tmp_path: Path, monkeypatch):
    skills = tmp_path / "skills"
    _skill(skills, "retired")
    _skill(skills, "active")
    state = tmp_path / "state"
    _retirement_sidecar(state, {"skills/retired/SKILL.md": {"status": "verified_absent"}})
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(state))

    summary = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins").build_skills_summary()

    assert "<name>retired</name>" not in summary
    assert "<name>active</name>" in summary


def test_unverified_present_skill_remains_in_catalogue(tmp_path: Path, monkeypatch):
    skills = tmp_path / "skills"
    _skill(skills, "pending")
    state = tmp_path / "state"
    _retirement_sidecar(state, {"skills/pending/SKILL.md": {"status": "unverified"}})
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(state))

    summary = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins").build_skills_summary()

    assert "<name>pending</name>" in summary


def test_unreadable_retirement_sidecar_filters_nothing(tmp_path: Path, monkeypatch):
    skills = tmp_path / "skills"
    _skill(skills, "retired")
    _skill(skills, "active")
    sidecar = tmp_path / "state" / "demand" / "skill_retirement_cooldown.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(tmp_path / "state"))

    summary = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtins").build_skills_summary()

    assert "<name>retired</name>" in summary
    assert "<name>active</name>" in summary


def test_retirement_filter_is_non_vacuous_against_isolated_unfiltered_copy(tmp_path: Path, monkeypatch):
    skills = tmp_path / "skills"
    _skill(skills, "retired")
    state = tmp_path / "state"
    _retirement_sidecar(state, {"skills/retired/SKILL.md": {"status": "verified_absent"}})
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(state))

    source = Path(__file__).parents[1] / "nanobot" / "agent" / "skills.py"
    text = source.read_text(encoding="utf-8")
    assert "verified_absent" in text
    broken = text.replace("if self._is_retired_skill(s, retired_paths):\n                continue\n", "if False:\n                continue\n", 1)
    assert broken != text
    isolated = tmp_path / "skills_loader_isolated.py"
    isolated.write_text(broken, encoding="utf-8")
    namespace: dict[str, object] = {"__file__": str(isolated), "__name__": "skills_loader_isolated"}
    exec(compile(broken, str(isolated), "exec"), namespace)
    loader = namespace["SkillsLoader"](tmp_path, builtin_skills_dir=tmp_path / "builtins")

    assert "<name>retired</name>" in loader.build_skills_summary()


def test_skill_order_is_stable_regardless_of_directory_order(tmp_path: Path, monkeypatch):
    """#1421: the enumeration order reaches the skills catalogue verbatim.

    Nothing downstream re-sorts, so a filesystem that hands back a different
    directory order changes the prompt prefix byte-for-byte for reasons that
    have nothing to do with any skill's content. Reversing iterdir is the
    only way to exercise this — on NTFS and ext4 the raw order is usually
    already sorted, so a test that merely creates directories out of order
    passes with or without the fix.
    """
    workspace_skills = tmp_path / "skills"
    builtins = tmp_path / "builtins"
    for name in ("alpha", "mike", "zulu"):
        _skill(workspace_skills, name)
    for name in ("bravo", "november"):
        _skill(builtins, name)

    real_iterdir = Path.iterdir

    def reversed_iterdir(self):
        return reversed(sorted(real_iterdir(self)))

    monkeypatch.setattr(Path, "iterdir", reversed_iterdir)

    loader = SkillsLoader(tmp_path, builtin_skills_dir=builtins)
    names = [s["name"] for s in loader.list_skills(filter_unavailable=False)]

    # Workspace skills keep their priority over builtins; within each source
    # the order is alphabetical rather than whatever the directory returned.
    assert names == ["alpha", "mike", "zulu", "bravo", "november"], names
