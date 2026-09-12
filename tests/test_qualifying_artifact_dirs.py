"""Structural guard for the shared qualifying-artifact directory policy (#1531)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
RUNTIME_MODULES = (
    "usage_evidence.py",
    "system_map.py",
    "demand.py",
    "scorecard.py",
)


def _qualifying_tuple_declarations(source: str) -> list[int]:
    """Return lines that locally spell the canonical directory tuple."""
    tree = ast.parse(source)
    lines: list[int] = []
    for node in ast.walk(tree):
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if not isinstance(value, ast.Tuple):
            continue
        if not all(isinstance(item, ast.Constant) for item in value.elts):
            continue
        values = [item.value for item in value.elts]
        if values == ["scripts", "surfaces"]:
            lines.append(node.lineno)
    return lines


def _assert_no_local_tuple_declarations(paths: list[Path]) -> None:
    declarations = {
        path.name: _qualifying_tuple_declarations(path.read_text(encoding="utf-8"))
        for path in paths
    }
    assert declarations == {path.name: [] for path in paths}


def test_runtime_modules_import_the_shared_directory_policy() -> None:
    _assert_no_local_tuple_declarations([
        ROOT / "nanobot" / "runtime" / name for name in RUNTIME_MODULES
    ])


def test_redeclaration_guard_is_non_vacuous_on_isolated_copy(tmp_path: Path) -> None:
    source_path = ROOT / "nanobot" / "runtime" / "demand.py"
    source = source_path.read_text(encoding="utf-8")
    broken = source.replace(
        "from nanobot.runtime.schemas import QUALIFYING_ARTIFACT_DIRS\n",
        "from nanobot.runtime.schemas import QUALIFYING_ARTIFACT_DIRS\n_SCRIPT_DIRS = (\"scripts\", \"surfaces\")\n",
        1,
    )
    assert broken != source
    isolated = tmp_path / "demand_with_redeclaration.py"
    isolated.write_text(broken, encoding="utf-8")

    clean_paths = [ROOT / "nanobot" / "runtime" / name for name in RUNTIME_MODULES]
    with pytest.raises(AssertionError):
        _assert_no_local_tuple_declarations([*clean_paths, isolated])
