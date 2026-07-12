"""Tests for #526: in-session closed-loop repair cycle.

Verifies _run_smoke_tests() and build_task(repair_context) without importing
the full bridge module chain (avoids loguru / nanobot import errors in dev env).
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BRIDGE_PATH = Path(__file__).parent.parent / 'nanobot' / 'runtime' / 'bridge.py'


# _run_smoke_tests depends on these module-level helpers (#668 hermetic-env
# fix) — pull them in alongside it so the AST-extraction sandbox has them.
_SMOKE_DEPS = ('_SMOKE_ENV_STRIP_PREFIXES', '_sanitized_smoke_env')

# #686: _run_smoke_tests now delegates test SELECTION to _select_gate_tests
# (mapping changed files -> affected + core test paths against the real repo
# tree at nanobot/runtime/bridge.py's own project layout — meaningless against
# an empty tmp_path). These tests only exercise the pytest-invocation/env/
# fail-safe plumbing of _run_smoke_tests itself, so a fixed stub selection
# (one always-selected dummy test path, no import targets) stands in for the
# real mapping — the same shape #526/#668 originally tested against the
# (then-unconditional) `tests/` directory.
_SELECT_GATE_TESTS_STUB = textwrap.dedent("""\
    def _select_gate_tests(repo_root, changed_files):
        return (['tests/test_dummy.py'], [])
""")


def _extract_fn(name: str, extra_setup: str = '') -> object:
    """AST-parse the bridge, extract a function (+ its known deps) by name, exec in isolation."""
    source = _BRIDGE_PATH.read_text()
    tree = ast.parse(source)
    names_needed = {name}
    if name == '_run_smoke_tests':
        names_needed.update(_SMOKE_DEPS)
        extra_setup = _SELECT_GATE_TESTS_STUB + extra_setup
    srcs: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.Assign)):
            node_name = None
            if isinstance(node, ast.FunctionDef):
                node_name = node.name
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                node_name = node.targets[0].id
            if node_name in names_needed:
                srcs[node_name] = ast.get_source_segment(source, node)
    missing = names_needed - srcs.keys()
    assert not missing, f'{missing} not found in bridge script'
    ns: dict = {}
    ordered_src = '\n'.join(srcs[n] for n in names_needed)
    exec(
        f'import subprocess, json, os, re, sys, time\nfrom pathlib import Path\n'
        f'from unittest.mock import MagicMock\n'
        f'{extra_setup}\n'
        f'{ordered_src}',
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


def test_smoke_uses_runtime_venv_interpreter_and_native_tb(tmp_path):
    """#668: argv must start with sys.executable (not bare python3) and use --tb=native."""
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='1 passed\n', stderr='')
        (tmp_path / 'tests').mkdir()
        fn(tmp_path)
    argv = mock_run.call_args.args[0]
    assert argv[0] == sys.executable
    assert '--tb=native' in argv
    assert '--tb=short' not in argv


def test_smoke_default_timeout_is_300(tmp_path):
    """#668: default timeout raised from 60s to 300s (full suite ~135s on eeepc host)."""
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='1 passed\n', stderr='')
        (tmp_path / 'tests').mkdir()
        fn(tmp_path)
    assert mock_run.call_args.kwargs['timeout'] == 300


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


def test_smoke_no_tests_dir_returns_false(tmp_path):
    """No tests/ directory → (False, ...) — #678 F2: fail-safe, not a free pass.

    A self-evolving repo always has tests; their absence (e.g. a cycle that
    `rm -rf tests/`) is suspicious and must not turn a bad change green.
    """
    fn = _extract_fn('_run_smoke_tests')
    passed, output = fn(tmp_path)
    assert passed is False
    assert 'no tests' in output


def test_smoke_no_tests_collected_returns_false(tmp_path):
    """'collected 0 items' in output → fail closed (#678 F2), not a free pass."""
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=5, stdout='collected 0 items\n', stderr='')
        (tmp_path / 'tests').mkdir()
        passed, _ = fn(tmp_path)
    assert passed is False


def test_smoke_timeout_returns_false(tmp_path):
    """TimeoutExpired → (False, 'pytest timed out')."""
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('pytest', 60)):
        (tmp_path / 'tests').mkdir()
        passed, output = fn(tmp_path)
    assert passed is False
    assert 'timed out' in output


def test_smoke_pytest_not_found_returns_false(tmp_path):
    """FileNotFoundError → (False, ...) — #678 F4: fail closed.

    pytest is always installed in the runtime venv (sys.executable is used to
    invoke it); a genuinely missing pytest is itself suspicious on the host, not
    a benign condition to skip past.
    """
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run', side_effect=FileNotFoundError('python3')):
        (tmp_path / 'tests').mkdir()
        passed, output = fn(tmp_path)
    assert passed is False
    assert 'unavailable' in output


# ─── #668: hermetic env for the smoke-test subprocess ────────────────────────

_POLLUTED_ENV_KEYS = {
    'STATE_DIR': '/var/lib/eeepc-agent/self-evolving-agent/state',
    'NANOBOT_CONFIG_PATH': '/run/user/1001/nanobot-eeepc/config.json',
    'SUBAGENT_BRIDGE_STATE_DIR': '/var/lib/eeepc-agent/self-evolving-agent/state/subagent_bridge',
    'SUBAGENT_BRIDGE_MODEL': 'cl/gemini-3.5-flash-low',
    'EEEBOT_SOME_FLAG': '1',
    'TARGET_WORKSPACE': '/opt/eeepc-agent/runtimes/self-evolving-agent/current',
    'LITELLM_API_KEY': 'sk-secret',
    'LITELLM_BASE_URL': 'https://litellm.internal',
    'GOAL_ID': 'goal-1',
    'SOURCE_COMMIT': 'deadbeef',
    'SELFEVO_SURFACES_DIR': '/opt/eeepc-agent/surfaces',
}


def test_smoke_subprocess_env_strips_runtime_state_keys(tmp_path, monkeypatch):
    """#668: pytest subprocess env must not inherit runtime-state keys from the bridge unit."""
    for key, value in _POLLUTED_ENV_KEYS.items():
        monkeypatch.setenv(key, value)
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='1 passed\n', stderr='')
        (tmp_path / 'tests').mkdir()
        fn(tmp_path)
    passed_env = mock_run.call_args.kwargs['env']
    for key in _POLLUTED_ENV_KEYS:
        assert key not in passed_env, f'{key} leaked into smoke-test subprocess env'


def test_smoke_subprocess_env_retains_path_and_home(tmp_path, monkeypatch):
    """#668: sanitization must not strip generic vars needed to run pytest at all."""
    monkeypatch.setenv('STATE_DIR', '/var/lib/eeepc-agent/self-evolving-agent/state')
    fn = _extract_fn('_run_smoke_tests')
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='1 passed\n', stderr='')
        (tmp_path / 'tests').mkdir()
        fn(tmp_path)
    passed_env = mock_run.call_args.kwargs['env']
    assert passed_env.get('PATH') == os.environ.get('PATH')
    if 'HOME' in os.environ:
        assert passed_env.get('HOME') == os.environ['HOME']


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
    # #713 added a _recent_activity_context() call inside build_task; stub it
    # fail-open (empty string, same as its real fail-open behavior) since these
    # tests assert on repair-context/previous-attempts sections, not recent
    # activity content.
    def _recent_activity_context(*a, **kw): return ''
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
