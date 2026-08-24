"""Tests for _LOOP_EXCLUDED_SKILLS in bridge.py (#958 Part B).

Verifies:
- cron, summarize, github are in the exclusion list alongside existing entries
- The list is wired into both primary and repair SubagentManager calls
- Interactive (non-loop) sessions are unaffected (excluded_skill_names is bridge-local)
"""
from __future__ import annotations

import ast
from pathlib import Path

BRIDGE_PATH = Path(__file__).parent.parent / "nanobot" / "runtime" / "bridge.py"


def _bridge_src() -> str:
    return BRIDGE_PATH.read_text(encoding="utf-8")


def _find_loop_excluded_skills_literal() -> list[str]:
    """Extract the _LOOP_EXCLUDED_SKILLS list literal from bridge.py."""
    tree = ast.parse(_bridge_src(), filename=str(BRIDGE_PATH))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_LOOP_EXCLUDED_SKILLS"
            and isinstance(node.value, ast.List)
        ):
            return [elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)]
    raise AssertionError("_LOOP_EXCLUDED_SKILLS not found in bridge.py")


def _find_excluded_skill_names_call_sites() -> list[str]:
    """Find all keyword values for 'excluded_skill_names' in bridge.py AST."""
    tree = ast.parse(_bridge_src(), filename=str(BRIDGE_PATH))
    sites: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "excluded_skill_names":
                    if isinstance(kw.value, ast.Name):
                        sites.append(kw.value.id)
    return sites


# ─── Part B: catalogue cleanup ───────────────────────────────────────────────


def test_cron_in_excluded_skills():
    excluded = _find_loop_excluded_skills_literal()
    assert "cron" in excluded, f"'cron' must be in _LOOP_EXCLUDED_SKILLS; got {excluded}"


def test_summarize_in_excluded_skills():
    excluded = _find_loop_excluded_skills_literal()
    assert "summarize" in excluded, f"'summarize' must be in _LOOP_EXCLUDED_SKILLS; got {excluded}"


def test_github_in_excluded_skills():
    excluded = _find_loop_excluded_skills_literal()
    assert "github" in excluded, f"'github' must be in _LOOP_EXCLUDED_SKILLS; got {excluded}"


def test_existing_excluded_skills_preserved():
    """Ensure existing entries (weather, tmux, clawhub) are still present."""
    excluded = _find_loop_excluded_skills_literal()
    for name in ("weather", "tmux", "clawhub"):
        assert name in excluded, f"'{name}' should still be in _LOOP_EXCLUDED_SKILLS"


def test_excluded_skill_names_wired_to_loop_excluded_variable():
    """Both SubagentManager call sites pass excluded_skill_names=_LOOP_EXCLUDED_SKILLS."""
    sites = _find_excluded_skill_names_call_sites()
    assert sites.count("_LOOP_EXCLUDED_SKILLS") >= 2, (
        f"Expected >= 2 call sites passing _LOOP_EXCLUDED_SKILLS, "
        f"found: {sites}"
    )


# ─── Interactive sessions unaffected ─────────────────────────────────────────


def test_interactive_session_build_system_prompt_accepts_no_exclusions(tmp_path: Path):
    """ContextBuilder.build_system_prompt works with no excluded_skill_names (interactive path)."""
    from nanobot.agent.context import ContextBuilder
    from nanobot.agent.skills import SkillsLoader

    skills_dir = tmp_path / "skills"
    (skills_dir / "cron").mkdir(parents=True)
    (skills_dir / "cron" / "SKILL.md").write_text(
        "---\nname: cron\ndescription: cron skill\n---\n# cron\n", encoding="utf-8"
    )

    builder = ContextBuilder(tmp_path)
    builder.skills = SkillsLoader(tmp_path, builtin_skills_dir=skills_dir)

    # No excluded_skill_names = interactive mode; cron should appear
    prompt = builder.build_system_prompt()
    assert "<name>cron</name>" in prompt, (
        "Interactive build_system_prompt with no exclusions should include cron"
    )


def test_excluded_skill_names_removes_cron_in_loop_context(tmp_path: Path):
    """When bridge passes excluded_skill_names=['cron', ...], cron is absent."""
    from nanobot.agent.context import ContextBuilder
    from nanobot.agent.skills import SkillsLoader

    skills_dir = tmp_path / "skills"
    for name in ("cron", "keep-me"):
        (skills_dir / name).mkdir(parents=True)
        (skills_dir / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test\n---\n# {name}\n", encoding="utf-8"
        )

    builder = ContextBuilder(tmp_path)
    builder.skills = SkillsLoader(tmp_path, builtin_skills_dir=skills_dir)

    prompt = builder.build_system_prompt(excluded_skill_names=["cron", "summarize", "github"])
    assert "<name>cron</name>" not in prompt
    assert "<name>keep-me</name>" in prompt
