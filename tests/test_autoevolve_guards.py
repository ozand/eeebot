from __future__ import annotations

import subprocess
from pathlib import Path
import pytest

from nanobot.runtime.autoevolve import create_candidate_release, apply_candidate_release
from test_autoevolve import _git, _init_repo


def test_apply_candidate_release_rejects_dirty_or_unpushed_candidate(tmp_path: Path):
    repo = tmp_path / 'repo'
    workspace = tmp_path / 'workspace'
    _init_repo(repo)
    (repo / 'README.md').write_text('dirty\n', encoding='utf-8')
    dirty = create_candidate_release(repo_root=repo, workspace=workspace)
    assert dirty['clean_worktree'] is False
    with pytest.raises(ValueError, match='clean tracked worktree'):
        apply_candidate_release(workspace=workspace, candidate_record=dirty)

    _git(repo, 'add', 'README.md')
    _git(repo, 'commit', '-m', 'unpushed')
    unpushed = create_candidate_release(repo_root=repo, workspace=workspace)
    assert unpushed['clean_worktree'] is True
    assert unpushed['remote_commit_visible'] is False
    with pytest.raises(ValueError, match='visible on remote'):
        apply_candidate_release(workspace=workspace, candidate_record=unpushed)
