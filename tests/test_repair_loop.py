"""Tests for #526: in-session closed-loop repair cycle.

Verifies _run_smoke_tests() and build_task(repair_context) without importing
the full bridge module chain (avoids loguru / nanobot import errors in dev env).
"""
from __future__ import annotations

import ast
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BRIDGE_PATH = Path(__file__).parent.parent / 'scripts' / 'eeepc_self_evolving_subagent_bridge.py'


def _extract_fn(name: str, extra_setup: str = '') -> object:
    """AST-parse the bridge, extract a single function by name, exec it in isolation."""
    source = _BRIDGE_PATH.read_text()
    tree = ast.parse(source)
    func_src = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            func_src = ast.get_source_segment(source, node)
            break
    assert func_src, f'{name} not found in bridge script'
    ns: dict = {}
    exec(
        f'import subprocess, json, os, re, time\nfrom pathlib import Path\n'
        f'from unittest.mock import MagicMock\n'
        f'{extra_setup}\n'
        f'{func_src}',
        ns,
    )
    return ns[name]


# ─── _run_smoke_tests ─────────────────────────────────────────────────────────

def test_smoke_pass_when_all_tests_pass(tmp_path):
    """subprocess returncode=0 → (True, output)."""
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='1 passed\n', stderr='')
        (tmp_path / 'tests').mkdir()
        passed, output = fn(tmp_path)
    assert passed is True
    assert '1 passed' in output


def test_smoke_fail_when_tests_fail(tmp_path):
    """subprocess returncode=1 → (False, traceback)."""
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1, stdout='FAILED tests/test_x.py::test_y\n1 failed\n', stderr='',
        )
        (tmp_path / 'tests').mkdir()
        passed, output = fn(tmp_path)
    assert passed is False
    assert '1 failed' in output


def test_smoke_no_tests_dir_returns_true(tmp_path):
    """No tests/ directory → (True, 'no tests directory')."""
    fn = _extract_fn('_run_smoke_tests')
    passed, output = fn(tmp_path)
    assert passed is True
    assert 'no tests' in output


def test_smoke_no_tests_collected_returns_true(tmp_path):
    """'collected 0 items' in output → treat as pass."""
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=5, stdout='collected 0 items\n', stderr='')
        (tmp_path / 'tests').mkdir()
        passed, _ = fn(tmp_path)
    assert passed is True


def test_smoke_timeout_returns_false(tmp_path):
    """TimeoutExpired → (False, 'pytest timed out')."""
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('pytest', 60)):
        (tmp_path / 'tests').mkdir()
        passed, output = fn(tmp_path)
    assert passed is False
    assert 'timed out' in output


def test_smoke_pytest_not_found_returns_true(tmp_path):
    """FileNotFoundError → (True, 'pytest unavailable')."""
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run', side_effect=FileNotFoundError('python3')):
        (tmp_path / 'tests').mkdir()
        passed, output = fn(tmp_path)
    assert passed is True
    assert 'unavailable' in output


# ─── build_task repair_context ────────────────────────────────────────────────

_FAKE_REQ = {
    'request_id': 'req-123',
    'cycle_id': 'cycle-abc',
    'goal_id': 'goal-1',
    'task_title': 'Test task',
    'semantic_task_id': 'subagent-verify',
    'source_artifact': '',
    'profile': 'bounded_execution',
}

_BUILD_TASK_SETUP = textwrap.dedent("""\
    # Minimal stubs so build_task can exec without SubagentManager etc.
    class _FakePath:
        def __init__(self, *a): self._p = Path(*a) if a else Path('.')
        def exists(self): return False
        def read_text(self, **kw): return '{}'
        def __truediv__(self, other): return _FakePath(str(self._p / other))
        def __str__(self): return str(self._p)
        def glob(self, *a): return []
    def _get_previous_attempts(*a, **kw): return []
""")


def test_build_task_no_repair_context():
    """Without repair_context, prompt has no ## Repair context section."""
    fn = _extract_fn('build_task', _BUILD_TASK_SETUP)
    prompt = fn(_FAKE_REQ, 'goal text', 'test', state_dir=None, repair_context=None)
    assert '## Repair context' not in prompt


def test_build_task_with_repair_context_adds_section():
    """With repair_context, prompt includes ## Repair context and traceback."""
    fn = _extract_fn('build_task', _BUILD_TASK_SETUP)
    traceback = 'FAILED tests/test_x.py::test_y - AssertionError: 1 != 2'
    prompt = fn(_FAKE_REQ, 'goal text', 'test', state_dir=None, repair_context=traceback)
    assert '## Repair context' in prompt
    assert 'AssertionError' in prompt
    assert 'MUST fix the failing tests' in prompt


def test_build_task_repair_context_has_mandatory_commit_instruction():
    """Repair section must include mandatory commit instruction."""
    fn = _extract_fn('build_task', _BUILD_TASK_SETUP)
    prompt = fn(_FAKE_REQ, 'goal text', 'test', state_dir=None, repair_context='FAILED assert False')
    assert 'Do NOT exit without at least one commit' in prompt


def test_build_task_repair_context_truncated_at_1500():
    """Long repair_context truncated to last 1500 chars (tail kept)."""
    fn = _extract_fn('build_task', _BUILD_TASK_SETUP)
    long_traceback = 'x' * 3000 + 'TAIL'
    prompt = fn(_FAKE_REQ, 'goal text', 'test', state_dir=None, repair_context=long_traceback)
    assert 'TAIL' in prompt
    assert 'x' * 2000 not in prompt
