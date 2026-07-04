"""Regression guard: nanobot/ and app/ must import nanobot.*, never eeebot.*.

The eeebot/ package is preserved as an external compatibility shim (both CLI
entrypoints, ~/.eeebot fallback paths), but internal code must be unified on
the nanobot.* import name to avoid dual-name drift (see issue #598).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("nanobot", "app")


def _iter_python_files():
    for dirname in SCAN_DIRS:
        base = REPO_ROOT / dirname
        if base.exists():
            yield from base.rglob("*.py")


def _eeebot_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "eeebot" or alias.name.startswith("eeebot."):
                    offenders.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "eeebot" or module.startswith("eeebot."):
                offenders.append(f"{path}:{node.lineno}: from {module} import ...")
    return offenders


def test_no_internal_eeebot_imports():
    """nanobot/ and app/ must not import the eeebot compatibility shim."""
    offenders: list[str] = []
    for path in _iter_python_files():
        offenders.extend(_eeebot_imports(path))

    assert not offenders, "Found internal imports of the eeebot shim:\n" + "\n".join(offenders)
