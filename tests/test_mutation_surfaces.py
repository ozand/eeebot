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
                n in src for n in (
                    '_BLOCKED_FILE_PATTERNS',
                    '_BLOCKED_WORD_PATTERNS',
                    '_SENSITIVE_WORDS',
                    '_ALLOWED_SENSITIVE_BASENAMES',
                    '_BLOCKED_EXACT_PATHS',
                    '_ALLOWED_PATH_PREFIXES',
                    '_ALLOWED_EXACT_PATHS',
                    '_GATE_EXT_ALLOWLIST',
                    '_GATE_BASENAME_ALLOWLIST',
                )
            ):
                constants += src + '\n'
    # Also extract module-level helper functions needed by the extracted function
    # (_is_blocked_filename is called by _validate_mutation_surfaces and
    # _classify_mutation_surface — it must be in scope for the isolated exec).
    _needed_helpers = {'_is_blocked_filename'}
    helpers = ''
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name in _needed_helpers
            and node.name != name
        ):
            helpers += (ast.get_source_segment(source, node) or '') + '\n'
    ns: dict = {}
    exec(f'{constants}\n{helpers}\n{extra_setup}\n{func_src}', ns)
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


def test_goals_md_is_explicitly_immutable():
    fn = _get_validate()
    violations = fn(['goals.md'])
    assert violations == ['immutable file blocked from mutation: goals.md']


def test_root_agents_is_operator_owned_and_skill_files_are_allowed():
    fn = _get_validate()
    violations = fn(['AGENTS.md'])
    assert violations == ['operator_owned_path: AGENTS.md']
    assert fn(['skills/review/SKILL.md']) == []


def test_nested_agents_is_not_an_exact_root_allowance():
    fn = _get_validate()
    violations = fn(['docs/AGENTS.md'])
    assert violations == []  # allowed only because docs/ is an existing prefix
    violations = fn(['other/AGENTS.md'])
    assert len(violations) == 1
    assert 'outside allowed paths' in violations[0]


def test_structural_filename_corpus():
    fn = _get_validate()
    allowed = [
        'scripts/analyze_token_usage.py',
        'scripts/check_token_budget.py',
        'scripts/validate_no_secrets.py',
        'memory/HISTORY.md',
    ]
    blocked = [
        '.env', '.env.local', 'api_token.json', 'token.txt', 'secrets.yaml',
        'my_credentials.json', 'id_rsa', '.git/config', 'package-lock.json',
        'private_key.pem', 'scripts/private_key_backup.pem',
    ]
    assert all(fn([path]) == [] for path in allowed)
    assert all(fn([path]) for path in blocked)


def test_blocked_filename_in_surfaces_is_violation():
    """surfaces/secret_key.json → 'secret_key' is a blocked structural pattern."""
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


def test_build_task_surfaces_come_from_gate_constants():
    import nanobot.runtime.bridge as bridge
    req = {"task_title": "x", "request_id": "r", "cycle_id": "c", "goal_id": "g"}
    prompt = bridge.build_task(req, "derived", "", max_iterations=17)
    for surface in list(bridge._ALLOWED_PATH_PREFIXES) + list(bridge._ALLOWED_EXACT_PATHS):
        assert surface in prompt
    assert "AGENTS.md" not in prompt
    assert "Creating or improving skills for repeated patterns is valuable work." in prompt
