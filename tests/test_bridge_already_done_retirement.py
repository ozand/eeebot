"""#1333: fuzzy git-log completion bookkeeping is retired, exact tags remain."""
from __future__ import annotations

import ast
from pathlib import Path

from nanobot.runtime import bridge


def test_fuzzy_already_done_symbols_and_outcome_writer_are_removed() -> None:
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_task_already_done" not in function_names
    assert "_task_already_done_for_path" not in function_names
    assert "_duplicate_check_title" not in function_names
    assert "'skipped-duplicate', 'already_done', [], None" not in source


def test_exact_success_tag_replay_protection_remains_distinct() -> None:
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    assert "_cycle_success_tag = f'cycle-{_safe_ref_id(_cycle_id)}-success'" in source
    assert "_cycle_tag_exists(_selfevo_repo_check, _cycle_success_tag)" in source
    assert "'already_done_tag'" in source
