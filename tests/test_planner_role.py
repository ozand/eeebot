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
    """AC 2/scope: the three discovery sources are required every run."""
    text = _body()
    discovery_line = next(line for line in text.splitlines() if "skills/index.md" in line)
    assert "every run" in text.lower()
    for qualifier in ("if ", "when it seems", "only when", "if it seems"):
        assert qualifier not in discovery_line.lower(), discovery_line


def test_planner_discovery_precedes_the_final_response_contract():
    """AC 2: consulted 'before choosing an increment' -- the discovery
    instruction is positioned before the Final response section, not after."""
    text = _body()
    discovery_pos = text.index("skills/index.md")
    final_response_pos = text.index("## Final response")
    assert discovery_pos < final_response_pos


def test_planner_applies_harness_loaded_task_contract_before_discovery():
    """#1891: model compliance with a read pointer is not the trust boundary;
    the harness loads the contract before the first model turn."""
    text = _body()
    assert "harness has already" in text.lower()
    assert text.index("task-writing contract") < text.index("diary/<today>.md")
    assert "Structural Falsifiability" not in text


def test_planner_role_fits_its_declared_budget_without_truncation():
    text, meta = load_role_text("planner", release_root=REPO_ROOT)
    assert meta["truncated"] is False
    assert len(text) <= meta["budget"]


def test_planner_role_contains_no_task_writing_path_placeholder():
    text, _meta = load_role_text("planner", release_root=REPO_ROOT)
    assert "{task_writing_skill_path}" not in text
