from pathlib import Path
import os
import subprocess

ROOT_EXPORT_SCRIPT = Path('/home/ozand/herkoot/Projects/nanobot/scripts/export_selfevo_repo.py')

from test_autoevolve import _git, _init_repo


def _run_export(repo: Path, env: dict[str, str]):
    merged = os.environ.copy()
    merged.update(env)
    return subprocess.run(['python3', str(ROOT_EXPORT_SCRIPT)], cwd=repo, env=merged, text=True, capture_output=True)


def test_export_selfevo_refuses_non_allowlisted_repo(tmp_path: Path):
    repo = tmp_path / 'repo'
    _init_repo(repo)
    result = _run_export(repo, {
        'NANOBOT_REPO_ROOT': str(repo),
        'NANOBOT_AUTOEVO_EXPORT_REMOTE_URL': 'https://github.com/ozand/eeebot.git',
        'NANOBOT_AUTOEVO_ALLOWED_REPO': 'ozand/eeebot-self-evolving',
    })
    assert result.returncode != 0
    assert 'allowed repo' in (result.stderr + result.stdout)


def test_export_selfevo_accepts_allowlisted_repo(tmp_path: Path):
    repo = tmp_path / 'repo'
    _init_repo(repo)
    bare = repo.parent / 'selfevo.git'
    _git(repo.parent, 'init', '--bare', str(bare))
    result = _run_export(repo, {
        'NANOBOT_REPO_ROOT': str(repo),
        'NANOBOT_AUTOEVO_EXPORT_REMOTE_URL': str(bare),
        'NANOBOT_AUTOEVO_ALLOWED_REPO': str(bare),
    })
    assert result.returncode == 0
    assert 'exported' in result.stdout
