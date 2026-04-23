from __future__ import annotations

import subprocess
from pathlib import Path
import pytest

from nanobot.runtime.autoevolve import commit_and_push_self_evolution
from test_autoevolve import _git, _init_repo


def test_commit_and_push_self_evolution_creates_commit_and_pushes(tmp_path: Path):
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'README.md').write_text('changed\n', encoding='utf-8')
    result = commit_and_push_self_evolution(repo_root=repo, message='autoevolve: test commit')
    assert result['created_commit'] is True
    assert result['pushed'] is True
    assert result['message'] == 'autoevolve: test commit'
    assert result['commit'] == _git(repo, 'rev-parse', 'HEAD')
    assert _git(repo, 'rev-parse', 'origin/master') == result['commit']


def test_commit_and_push_self_evolution_skips_when_no_tracked_changes(tmp_path: Path):
    repo = tmp_path / 'repo'
    _init_repo(repo)
    result = commit_and_push_self_evolution(repo_root=repo, message='autoevolve: no-op')
    assert result['created_commit'] is False
    assert result['pushed'] is False


def test_commit_and_push_self_evolution_ignores_untracked_runtime_files(tmp_path: Path):
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'workspace').mkdir()
    (repo / 'workspace' / 'temp.json').write_text('{}', encoding='utf-8')
    result = commit_and_push_self_evolution(repo_root=repo, message='autoevolve: ignore untracked')
    assert result['created_commit'] is False
