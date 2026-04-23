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
BASE_BRANCH = os.environ.get('NANOBOT_AUTOEVO_EXPORT_BASE_BRANCH', 'main')
MESSAGE = os.environ.get('NANOBOT_AUTOEVO_EXPORT_MESSAGE', 'autoevolve: export self-evolving host runtime')
ALLOWED_REPO = os.environ.get('NANOBOT_AUTOEVO_ALLOWED_REPO', 'ozand/eeebot-self-evolving')
TOKEN = os.environ.get('NANOBOT_SELFEVO_GITHUB_TOKEN', '').strip()


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def copy_ignore(src, names):
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


def _clear_export_tree(export: Path) -> None:
    for child in export.iterdir():
        if child.name == '.git':
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def main():
    normalized = _normalized_repo_id(REMOTE_URL)
    if normalized != ALLOWED_REPO:
        raise SystemExit(f'refusing publish: target repo {normalized!r} does not match allowed repo {ALLOWED_REPO!r}')
    tmp = Path(tempfile.mkdtemp(prefix='selfevo-export-'))
    try:
        export = tmp / 'export'
        run(['git', 'clone', _remote_url_with_token(REMOTE_URL, TOKEN), str(export)])
        base_checkout = subprocess.run(['git', 'checkout', BASE_BRANCH], cwd=export, text=True, capture_output=True)
        if base_checkout.returncode == 0:
            run(['git', 'checkout', '-B', BRANCH], cwd=export)
        else:
            run(['git', 'checkout', '--orphan', BRANCH], cwd=export)
        _clear_export_tree(export)
        for item in REPO_ROOT.iterdir():
            if item.name in {'.git', '.venv', 'workspace', '__pycache__', '.pytest_cache'}:
                continue
            if item.name == '.github':
                target = export / item.name
                target.mkdir(parents=True, exist_ok=True)
                for sub in item.iterdir():
                    if sub.name == 'workflows':
                        continue
                    dst = target / sub.name
                    if sub.is_dir():
                        shutil.copytree(sub, dst, ignore=copy_ignore)
                    else:
                        shutil.copy2(sub, dst)
                continue
            dst = export / item.name
            if item.is_dir():
                shutil.copytree(item, dst, ignore=copy_ignore)
            else:
                shutil.copy2(item, dst)
        run(['git', 'config', 'user.email', 'bot@example.com'], cwd=export)
        run(['git', 'config', 'user.name', 'eeebot-self-evolving'], cwd=export)
        run(['git', 'add', '.'], cwd=export)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=export, text=True, capture_output=True, check=True)
        if not status.stdout.strip():
            auth_mode = 'dedicated_token' if TOKEN else 'ambient_git_auth'
            print(f'exported-noop auth_mode={auth_mode} allowed_repo={ALLOWED_REPO}')
            return
        run(['git', 'commit', '-m', MESSAGE], cwd=export)
        run(['git', 'push', '--force-with-lease', 'origin', f'HEAD:{BRANCH}'], cwd=export)
        auth_mode = 'dedicated_token' if TOKEN else 'ambient_git_auth'
        print(f'exported auth_mode={auth_mode} allowed_repo={ALLOWED_REPO}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
