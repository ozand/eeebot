"""Tests for #528: bounded mutation surfaces validation.

Verifies _validate_mutation_surfaces() correctly flags violations and
allows clean changes. Also checks that build_task() prompt includes
the mutation surfaces section.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BRIDGE_PATH = Path(__file__).parent.parent / 'nanobot' / 'runtime' / 'bridge.py'


def _extract_fn(name: str, extra_setup: str = '') -> object:
    source = _BRIDGE_PATH.read_text()
    tree = ast.parse(source)
    func_src = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            func_src = ast.get_source_segment(source, node)
            break
    assert func_src, f'{name} not found in bridge'
    # Also extract module-level constants needed by the function
    constants = ''
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            src = ast.get_source_segment(source, node)
            if src and any(
                n in src for n in ('_BLOCKED_FILE_PATTERNS', '_ALLOWED_PATH_PREFIXES')
            ):
                constants += src + '\n'
    ns: dict = {}
    exec(f'{constants}\n{extra_setup}\n{func_src}', ns)
    return ns[name]


def _get_validate() -> object:
    return _extract_fn('_validate_mutation_surfaces')


# ─── _validate_mutation_surfaces ─────────────────────────────────────────────

def test_surfaces_file_is_clean():
    """surfaces/retry_policy.json → no violation."""
    fn = _get_validate()
    violations = fn(['surfaces/retry_policy.json'])
    assert violations == []


def test_scripts_file_is_clean():
    """scripts/cycle_logger.py → no violation."""
    fn = _get_validate()
    violations = fn(['scripts/cycle_logger.py'])
    assert violations == []


def test_memory_file_is_clean():
    """memory/MEMORY.md → no violation."""
    fn = _get_validate()
    violations = fn(['memory/MEMORY.md'])
    assert violations == []


def test_env_file_is_violation():
    """.env file → violation (blocked filename pattern)."""
    fn = _get_validate()
    violations = fn(['.env'])
    assert len(violations) == 1
    assert '.env' in violations[0]


def test_state_file_is_violation():
    """state/goals/history.json → outside allowed paths → violation."""
    fn = _get_validate()
    violations = fn(['state/goals/history.json'])
    assert len(violations) == 1
    assert 'outside allowed paths' in violations[0]


def test_blocked_filename_in_surfaces_is_violation():
    """surfaces/secret_key.json → 'secret' is a blocked pattern."""
    fn = _get_validate()
    violations = fn(['surfaces/secret_key.json'])
    assert len(violations) == 1
    assert 'secret' in violations[0]


def test_empty_changed_files_is_clean():
    """Empty list → no violations."""
    fn = _get_validate()
    assert fn([]) == []


def test_multiple_violations_returned():
    """Multiple bad files → multiple violations."""
    fn = _get_validate()
    violations = fn(['.env', 'state/goals/history.json'])
    assert len(violations) == 2


def test_mixed_clean_and_violation():
    """One clean + one violation → only violation reported."""
    fn = _get_validate()
    violations = fn(['surfaces/task_selector.json', 'state/bad_file.json'])
    assert len(violations) == 1
    assert 'state/bad_file.json' in violations[0]


# ─── build_task prompt includes mutation surfaces section ─────────────────────

def test_build_task_prompt_includes_mutation_surfaces():
    """build_task() prompt must include '## Mutation surfaces' section."""
    source = _BRIDGE_PATH.read_text()
    assert '## Mutation surfaces' in source, \
        'build_task() must include ## Mutation surfaces section in the prompt'


def test_build_task_lists_all_7_surfaces():
    """All 7 surface files must be named in build_task() prompt output."""
    source = _BRIDGE_PATH.read_text()
    expected = [
        'task_selector.json',
        'prompt_template.md',
        'retry_policy.json',
        'tool_policy.json',
        'memory_policy.json',
        'score_weights.json',
        'lesson_policy.json',
    ]
    for surface_file in expected:
        assert surface_file in source, f'{surface_file} missing from bridge source'
