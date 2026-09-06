"""Issue #713 / #1333: _duplicate_check_title was the pre-spawn title resolver.

The function was retired in #1333 alongside the fuzzy git-log gate
(_task_already_done). The title-resolution logic (backlog_title > task_title >
semantic_task_id) was inlined into _main_impl and no longer needs a separate
importable symbol.

This module is kept as a placeholder test verifying the symbol is gone and
the remaining test_bridge_already_done_retirement.py covers the retirement contract.
"""
from __future__ import annotations

import ast
from pathlib import Path

from nanobot.runtime import bridge


def test_duplicate_check_title_is_removed() -> None:
    """_duplicate_check_title was inlined into _main_impl in #1333."""
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_duplicate_check_title" not in function_names
