from pathlib import Path

from nanobot.runtime.autoevolve import commit_and_push_self_evolution, create_candidate_release
from test_autoevolve import _git, _init_repo


def test_autoevolve_can_push_to_separate_remote_branch(tmp_path: Path):
    repo = tmp_path / 'repo'
    workspace = tmp_path / 'workspace'
    _init_repo(repo)
    bare2 = repo.parent / 'selfevo.git'
    _git(repo.parent, 'init', '--bare', str(bare2))
    _git(repo, 'remote', 'add', 'selfevo', str(bare2))

    (repo / 'README.md').write_text('selfevo\n', encoding='utf-8')
    pushed = commit_and_push_self_evolution(repo_root=repo, message='autoevolve: separate remote', remote_name='selfevo', branch='main')
    assert pushed['remote_name'] == 'selfevo'
    assert _git(repo, 'rev-parse', 'selfevo/main') == pushed['commit']

    candidate = create_candidate_release(repo_root=repo, workspace=workspace, remote_name='selfevo', branch='main')
    assert candidate['remote_name'] == 'selfevo'
    assert candidate['remote_commit_visible'] is True
