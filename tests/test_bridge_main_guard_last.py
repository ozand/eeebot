"""Regression guard for the def-after-__main__ incident (2026-09-01).

``python -m nanobot.runtime.bridge`` executes the module body top-to-bottom
with ``__name__ == '__main__'``: the guard block starts the whole bridge loop
the moment it is reached, so any top-level ``def``/``class``/assignment placed
BELOW the guard does not exist in module scope while the loop runs. Tests
import the module (all defs load), so CI stays green while every live cycle
dies with a NameError — exactly what happened when ``_parse_explore_mode``
was appended below the guard: the loop crashed on every timer fire for 8
hours (release 20260901T041403Z-canonical-fe03357c).

This test statically asserts the ``if __name__ == '__main__'`` guard is the
LAST top-level statement in bridge.py, so nothing can hide below it.
"""

import ast
from pathlib import Path

BRIDGE_PATH = Path(__file__).resolve().parents[1] / "nanobot" / "runtime" / "bridge.py"


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def test_main_guard_is_last_top_level_statement() -> None:
    tree = ast.parse(BRIDGE_PATH.read_text(encoding="utf-8"))
    guards = [i for i, node in enumerate(tree.body) if _is_main_guard(node)]
    assert guards, "bridge.py must contain an if __name__ == '__main__' guard"
    last_guard = guards[-1]
    trailing = tree.body[last_guard + 1 :]
    offenders = [
        f"line {node.lineno}: {type(node).__name__}"
        for node in trailing
    ]
    assert not offenders, (
        "top-level statements found AFTER the __main__ guard in bridge.py — "
        "they do not exist in module scope while the live loop runs "
        "(def-after-guard incident class): " + "; ".join(offenders)
    )
