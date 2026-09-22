"""#1857 AC 2: the planning session's fixed prompt (roles/planner.md)
instructs consulting skills/index.md and the memory entry point
unconditionally, before choosing an increment."""
from __future__ import annotations

from pathlib import Path

from nanobot.runtime.role_prompt import load_role_text

REPO_ROOT = Path(__file__).resolve().parents[1]
PLANNER_MD = REPO_ROOT / "roles" / "planner.md"


def _body() -> str:
    return PLANNER_MD.read_text(encoding="utf-8")


def test_planner_names_the_derivable_skills_path():
    """Not 'the skills catalogue' -- a path the session can read_file
    directly, same shape as the diary's derivable diary/<date>.md."""
    text = _body()
    assert "skills/index.md" in text
    assert "the skills catalogue" not in text.lower()


def test_planner_names_the_derivable_memory_path():
    """A named memory entry point, not a pointer to the search_memory tool."""
    text = _body()
    assert "memory/index.md" in text


def test_planner_discovery_clause_is_unconditional():
    """AC 2/scope: unconditional, stated as such in the text -- not left to
    the session's own judgement about whether this particular plan needs
    it (the #1805/#1857 failure mode this record exists to remove)."""
    text = _body()
    lowered = text.lower()
    assert "unconditional" in lowered
    # the discovery sentence itself must not carry a relevance qualifier
    discovery_line = next(
        line for line in text.splitlines() if "skills/index.md" in line
    )
    for qualifier in ("if ", "when it seems", "only when", "if it seems"):
        assert qualifier not in discovery_line.lower(), discovery_line


def test_planner_discovery_precedes_the_final_response_contract():
    """AC 2: consulted 'before choosing an increment' -- the discovery
    instruction is positioned before the Final response section, not after."""
    text = _body()
    discovery_pos = text.index("skills/index.md")
    final_response_pos = text.index("## Final response")
    assert discovery_pos < final_response_pos


def test_planner_task_writing_instruction_is_unconditional_and_precedes_choice():
    """#1865: planning always reads the release-owned task contract before
    emitting an increment; its full text stays outside the resident role."""
    text = _body()
    line = next(line for line in text.splitlines() if "task_writing_skill_path" in line)
    assert "unconditional" in text.lower()
    for qualifier in ("if ", "when it seems", "only when", "if it seems"):
        assert qualifier not in line.lower(), line
    assert text.index("task_writing_skill_path") < text.index("## Final response")
    assert "Structural Falsifiability" not in text


def test_planner_resolves_task_writing_to_its_explicit_release_root(tmp_path: Path):
    root = tmp_path / "release"
    (root / "roles").mkdir(parents=True)
    for name in ("IDENTITY.md", "goals.md"):
        (root / name).write_text("x\n", encoding="utf-8")
    (root / "roles" / "planner.md").write_text(_body(), encoding="utf-8")

    text, _meta = load_role_text("planner", release_root=root)

    expected = str(root / "nanobot" / "skills" / "task-writing" / "SKILL.md")
    assert expected in text
    assert "{task_writing_skill_path}" not in text
