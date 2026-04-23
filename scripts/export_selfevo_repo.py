#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(os.environ.get('NANOBOT_REPO_ROOT', '/home/ozand/herkoot/Projects/nanobot')).resolve()
REMOTE_URL = os.environ.get('NANOBOT_AUTOEVO_EXPORT_REMOTE_URL', 'https://github.com/ozand/eeebot-self-evolving.git')
BRANCH = os.environ.get('NANOBOT_AUTOEVO_EXPORT_BRANCH', 'main')
MESSAGE = os.environ.get('NANOBOT_AUTOEVO_EXPORT_MESSAGE', 'autoevolve: export self-evolving host runtime')


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def ignore(src, names):
    ignored = {'.git', '.venv', 'workspace', '__pycache__', '.pytest_cache'}
    if Path(src).name == '.github' and 'workflows' in names:
        ignored.add('workflows')
    return ignored.intersection(names)


def main():
    tmp = Path(tempfile.mkdtemp(prefix='selfevo-export-'))
    try:
        export = tmp / 'export'
        shutil.copytree(REPO_ROOT, export, ignore=ignore)
        run(['git', 'init', '-b', BRANCH], cwd=export)
        run(['git', 'config', 'user.email', 'bot@example.com'], cwd=export)
        run(['git', 'config', 'user.name', 'eeebot-self-evolving'], cwd=export)
        run(['git', 'add', '.'], cwd=export)
        run(['git', 'commit', '-m', MESSAGE], cwd=export)
        run(['git', 'remote', 'add', 'origin', REMOTE_URL], cwd=export)
        run(['git', 'push', '--force', 'origin', f'HEAD:{BRANCH}'], cwd=export)
        print('exported')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
