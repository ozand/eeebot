"""Tests for Bug 2 fix: _capture_pre_spawn_sha and _count_commits_since.

Verifies that commits_pushed is correctly counted relative to pre-spawn SHA,
even when the subagent pushes commits itself (making origin/main..HEAD = 0).
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BRIDGE_PATH = Path(__file__).parent.parent / 'scripts' / 'eeepc_self_evolving_subagent_bridge.py'


def _extract_fn(name: str, extra_setup: str = '') -> object:
    """AST-extract a single function from bridge script and exec in isolation."""
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
        f'import subprocess, json, os, re\nfrom pathlib import Path\n'
        f'{extra_setup}\n'
        f'{func_src}',
        ns,
    )
    return ns[name]


# ── _capture_pre_spawn_sha ─────────────────────────────────────────────────────

def test_capture_pre_spawn_sha_writes_file(tmp_path):
    """`_capture_pre_spawn_sha` writes SHA to sha_file and returns it."""
    fn = _extract_fn('_capture_pre_spawn_sha')
    fake_sha = 'abc123def456abc123def456abc123def456abc12'
    sha_file = tmp_path / 'bridge_pre_spawn.sha'
    repo = tmp_path / 'repo'

    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_sha + '\n')
        result = fn(repo, sha_file)

    assert result == fake_sha
    assert sha_file.exists()
    assert sha_file.read_text().strip() == fake_sha


def test_capture_pre_spawn_sha_returns_empty_on_error(tmp_path):
    """Returns '' and does not write file when git fails."""
    fn = _extract_fn('_capture_pre_spawn_sha')
    sha_file = tmp_path / 'bridge_pre_spawn.sha'
    repo = tmp_path / 'repo'

    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout='')
        result = fn(repo, sha_file)

    assert result == ''
    assert not sha_file.exists()


def test_capture_pre_spawn_sha_overwrites_existing(tmp_path):
    """Overwrites sha_file unconditionally on each call."""
    fn = _extract_fn('_capture_pre_spawn_sha')
    sha_file = tmp_path / 'bridge_pre_spawn.sha'
    sha_file.write_text('old_sha')
    repo = tmp_path / 'repo'
    new_sha = 'newsha' * 6 + 'abcd'  # 40 chars

    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=new_sha)
        fn(repo, sha_file)

    assert sha_file.read_text() == new_sha


# ── _count_commits_since ───────────────────────────────────────────────────────

def test_count_commits_since_returns_int(tmp_path):
    """`_count_commits_since` parses git output and returns int."""
    fn = _extract_fn('_count_commits_since')
    repo = tmp_path / 'repo'

    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='3\n')
        result = fn(repo, 'abc123')

    assert result == 3


def test_count_commits_since_zero_on_error(tmp_path):
    """Returns 0 when subprocess fails."""
    fn = _extract_fn('_count_commits_since')
    repo = tmp_path / 'repo'

    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=128, stdout='')
        result = fn(repo, 'abc123')

    assert result == 0


def test_count_commits_since_empty_sha(tmp_path):
    """Returns 0 immediately when pre_spawn_sha is empty (no git call)."""
    fn = _extract_fn('_count_commits_since')
    repo = tmp_path / 'repo'

    with patch('subprocess.run') as mock_run:
        result = fn(repo, '')

    assert result == 0
    mock_run.assert_not_called()


def test_count_commits_since_zero_output(tmp_path):
    """Returns 0 when git reports 0 commits since SHA."""
    fn = _extract_fn('_count_commits_since')
    repo = tmp_path / 'repo'

    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='0\n')
        result = fn(repo, 'abc123')

    assert result == 0


# ── Integration: real git repo ─────────────────────────────────────────────────

def test_integration_real_git(tmp_path):
    """End-to-end: capture SHA, make commits, count_commits_since returns correct int."""
    # Skip if git not available
    r = subprocess.run(['git', '--version'], capture_output=True)
    if r.returncode != 0:
        pytest.skip('git not available')

    repo = tmp_path / 'testrepo'
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ['git', '-c', f'safe.directory={repo}', '-C', str(repo)] + list(args),
            capture_output=True, check=True,
        )

    git('init')
    git('config', 'user.email', 'test@test.com')
    git('config', 'user.name', 'Test')
    (repo / 'a.txt').write_text('init')
    git('add', 'a.txt')
    git('commit', '-m', 'init')

    # Capture SHA at this point
    fn_capture = _extract_fn('_capture_pre_spawn_sha')
    fn_count = _extract_fn('_count_commits_since')
    sha_file = tmp_path / 'pre_spawn.sha'

    pre_sha = fn_capture(repo, sha_file)
    assert pre_sha, 'SHA should be captured'

    # Make 2 more commits (simulating subagent work)
    (repo / 'b.txt').write_text('change1')
    git('add', 'b.txt')
    git('commit', '-m', 'feat: first')

    (repo / 'c.txt').write_text('change2')
    git('add', 'c.txt')
    git('commit', '-m', 'feat: second')

    count = fn_count(repo, pre_sha)
    assert count == 2, f'Expected 2 new commits, got {count}'
