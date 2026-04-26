from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from test_autoevolve import _init_repo


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_run_local_cycle_refreshes_observed_product_head_after_runtime_exits(tmp_path: Path):
    repo = tmp_path / 'repo'
    _init_repo(repo)
    workspace = repo / 'workspace'
    app_dir = tmp_path / 'runtime' / 'app'
    app_dir.mkdir(parents=True)
    runtime_pkg = tmp_path / 'runtime' / 'nanobot' / 'runtime'
    runtime_pkg.mkdir(parents=True)
    (runtime_pkg.parent / '__init__.py').write_text('', encoding='utf-8')
    (runtime_pkg / '__init__.py').write_text('', encoding='utf-8')
    (runtime_pkg / 'autoevolve.py').write_text(
        "def write_guarded_evolution_state(workspace):\n"
        "    raise RuntimeError('stale pinned runtime writer should not be imported')\n",
        encoding='utf-8',
    )
    (app_dir / 'main.py').write_text("print('dummy cycle PASS')\n", encoding='utf-8')
    state = workspace / 'state' / 'self_evolution' / 'candidates'
    state.mkdir(parents=True)
    stale_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    (repo / 'README.md').write_text('advanced product head\n', encoding='utf-8')
    subprocess.run(['git', 'commit', '-q', '-am', 'advance product head'], cwd=repo, check=True)
    product_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    (state / 'latest.json').write_text(json.dumps({'candidate_id': 'candidate-1', 'commit': stale_commit}), encoding='utf-8')
    env = os.environ.copy()
    env.update({
        'NANOBOT_RUNTIME_ROOT': str(app_dir.parent),
        'NANOBOT_REPO_ROOT': str(REPO_ROOT),
        'NANOBOT_WORKSPACE': str(workspace),
        'PYTHONPATH': str(REPO_ROOT),
    })

    result = subprocess.run([str(REPO_ROOT / 'scripts' / 'run_local_cycle.sh')], env=env, text=True, capture_output=True, check=True)

    current_state = json.loads((workspace / 'state' / 'self_evolution' / 'current_state.json').read_text(encoding='utf-8'))
    assert 'dummy cycle PASS' in result.stdout
    assert current_state['current_candidate']['commit'] == stale_commit
    assert current_state['observed_product_head']['commit'] == product_head
    assert current_state['product_head'] == product_head


def test_run_local_cycle_still_refreshes_observed_product_head_when_cycle_fails(tmp_path: Path):
    repo = tmp_path / 'repo'
    _init_repo(repo)
    workspace = repo / 'workspace'
    app_dir = tmp_path / 'runtime' / 'app'
    app_dir.mkdir(parents=True)
    (app_dir / 'main.py').write_text("raise SystemExit(7)\n", encoding='utf-8')
    state = workspace / 'state' / 'self_evolution' / 'candidates'
    state.mkdir(parents=True)
    stale_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    (repo / 'README.md').write_text('advanced product head\n', encoding='utf-8')
    subprocess.run(['git', 'commit', '-q', '-am', 'advance product head'], cwd=repo, check=True)
    product_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    (state / 'latest.json').write_text(json.dumps({'candidate_id': 'candidate-1', 'commit': stale_commit}), encoding='utf-8')
    env = os.environ.copy()
    env.update({
        'NANOBOT_RUNTIME_ROOT': str(app_dir.parent),
        'NANOBOT_REPO_ROOT': str(REPO_ROOT),
        'NANOBOT_WORKSPACE': str(workspace),
        'PYTHONPATH': str(REPO_ROOT),
    })

    result = subprocess.run([str(REPO_ROOT / 'scripts' / 'run_local_cycle.sh')], env=env, text=True, capture_output=True)

    current_state = json.loads((workspace / 'state' / 'self_evolution' / 'current_state.json').read_text(encoding='utf-8'))
    assert result.returncode == 7
    assert current_state['current_candidate']['commit'] == stale_commit
    assert current_state['observed_product_head']['commit'] == product_head
