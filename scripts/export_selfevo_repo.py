#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(os.environ.get('NANOBOT_REPO_ROOT', '/home/ozand/herkoot/Projects/nanobot')).resolve()
REMOTE_URL = os.environ.get('NANOBOT_AUTOEVO_EXPORT_REMOTE_URL', 'https://github.com/ozand/eeebot-self-evolving.git')
BRANCH = os.environ.get('NANOBOT_AUTOEVO_EXPORT_BRANCH', 'main')
MESSAGE = os.environ.get('NANOBOT_AUTOEVO_EXPORT_MESSAGE', 'autoevolve: export self-evolving host runtime')
ALLOWED_REPO = os.environ.get('NANOBOT_AUTOEVO_ALLOWED_REPO', 'ozand/eeebot-self-evolving')
TOKEN = os.environ.get('NANOBOT_SELFEVO_GITHUB_TOKEN', '').strip()


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def ignore(src, names):
    ignored = {'.git', '.venv', 'workspace', '__pycache__', '.pytest_cache'}
    if Path(src).name == '.github' and 'workflows' in names:
        ignored.add('workflows')
    return ignored.intersection(names)


def _normalized_repo_id(remote_url: str) -> str:
    original = remote_url
    if remote_url.endswith('.git'):
        remote_url = remote_url[:-4]
    if remote_url.startswith('git@github.com:'):
        return remote_url.split(':', 1)[1]
    if remote_url.startswith('https://github.com/'):
        return remote_url.split('https://github.com/', 1)[1]
    if original.startswith('/'):
        return original
    return remote_url


def _remote_url_with_token(remote_url: str, token: str) -> str:
    if not token or not remote_url.startswith('https://github.com/'):
        return remote_url
    return remote_url.replace('https://github.com/', f'https://x-access-token:{token}@github.com/', 1)


def main():
    normalized = _normalized_repo_id(REMOTE_URL)
    if normalized != ALLOWED_REPO:
        raise SystemExit(f'refusing publish: target repo {normalized!r} does not match allowed repo {ALLOWED_REPO!r}')
    tmp = Path(tempfile.mkdtemp(prefix='selfevo-export-'))
    try:
        export = tmp / 'export'
        shutil.copytree(REPO_ROOT, export, ignore=ignore)
        run(['git', 'init', '-b', BRANCH], cwd=export)
        run(['git', 'config', 'user.email', 'bot@example.com'], cwd=export)
        run(['git', 'config', 'user.name', 'eeebot-self-evolving'], cwd=export)
        run(['git', 'add', '.'], cwd=export)
        run(['git', 'commit', '-m', MESSAGE], cwd=export)
        run(['git', 'remote', 'add', 'origin', _remote_url_with_token(REMOTE_URL, TOKEN)], cwd=export)
        run(['git', 'push', '--force', 'origin', f'HEAD:{BRANCH}'], cwd=export)
        auth_mode = 'dedicated_token' if TOKEN else 'ambient_git_auth'
        print(f'exported auth_mode={auth_mode} allowed_repo={ALLOWED_REPO}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
