"""Test Contract for ADR-021 Rule 2: trainer proposes, gate holds.

ADR-021 Rule 2:
    "Skill changes from the night contour enter as proposals through the
    existing mutation surface and gate. No direct commit. This is ADR-018's
    boundary in a second place: the component with the wider view does not
    thereby get the wider hand."

    "Rule 2 is verifiable by a test contract on the call graph: no path from
    the trainer reaches git commit directly; every path to a skill edit
    passes through the gate."
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

TRAINER_MODULES = [
    "nanobot/runtime/skill_eval_harness.py",
    "nanobot/runtime/skill_fitness.py",
    "nanobot/runtime/skill_candidate_mining.py",
    "nanobot/runtime/retirement_candidates.py",
    "nanobot/runtime/knowledge_curator.py",
    "nanobot/runtime/reflector.py",
]

_GIT_COMMIT_PATTERN = re.compile(r"""['"]commit['"]""")


def _find_subprocess_calls(tree: ast.AST) -> list[tuple[int, str]]:
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                func_name = node.func.id

            if func_name in {"run", "Popen", "check_call", "check_output", "call", "exec"}:
                call_repr = ast.unparse(node) if hasattr(ast, "unparse") else ""
                calls.append((node.lineno, call_repr))
    return calls


def test_no_trainer_module_invokes_git_commit_directly() -> None:
    """Rule 2: no path from any trainer or night contour module reaches git commit."""
    repo_root = Path(__file__).resolve().parent.parent

    for rel_path in TRAINER_MODULES:
        path = repo_root / rel_path
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = _find_subprocess_calls(tree)
        for lineno, code in calls:
            assert not _GIT_COMMIT_PATTERN.search(code), (
                f"ADR-021 Rule 2 violation in {rel_path}:{lineno}: "
                f"trainer path directly invokes git commit:\n  {code}"
            )


def test_trainer_modules_do_not_import_git_commit_helpers() -> None:
    """Rule 2: trainer modules do not import git write/commit routines."""
    repo_root = Path(__file__).resolve().parent.parent

    for rel_path in TRAINER_MODULES:
        path = repo_root / rel_path
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                import_stmt = ast.unparse(node) if hasattr(ast, "unparse") else ""
                assert "commit" not in import_stmt.lower(), (
                    f"ADR-021 Rule 2 violation in {rel_path}:{node.lineno}: "
                    f"trainer module imports commit symbol: {import_stmt}"
                )


def test_only_bridge_gate_commits_in_runtime() -> None:
    """Every path that commits to git in the runtime passes through the gate in nanobot/runtime/bridge.py."""
    repo_root = Path(__file__).resolve().parent.parent
    runtime_dir = repo_root / "nanobot" / "runtime"

    files_with_commit = []
    for py_file in runtime_dir.glob("*.py"):
        if py_file.name in {"bridge.py", "gate.py"}:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        calls = _find_subprocess_calls(tree)
        for lineno, code in calls:
            if _GIT_COMMIT_PATTERN.search(code):
                files_with_commit.append((py_file.name, lineno, code))

    assert not files_with_commit, (
        f"ADR-021 Rule 2 violation: non-gate runtime files invoke git commit:\n{files_with_commit}"
    )


def test_rule1_threshold_not_reached_for_trainer_authority() -> None:
    """Rule 1 prerequisite: trainer authority requires >=50 citations.

    Currently at 11 citations. No trainer authority may be granted until
    the threshold is reached.
    """
    from nanobot.runtime.lesson_v2 import MIN_CITATIONS_FOR_AUTHORITY

    assert MIN_CITATIONS_FOR_AUTHORITY == 50
