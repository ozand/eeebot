"""Network-bound GitHub ops and git exports decoupled from the core runtime."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from nanobot.runtime.autoevolve import _git, resolve_terminal_selfevo_issue


def ensure_selfevo_issue(*, repo: str, title: str, body: str, workspace: Path | None = None, source_task_id: str | None = None) -> dict[str, Any]:
    if workspace is not None and source_task_id:
        terminal_issue = resolve_terminal_selfevo_issue(workspace=workspace, source_task_id=source_task_id)
        if terminal_issue is not None:
            return terminal_issue
    lookup = subprocess.run(['gh', 'issue', 'list', '--repo', repo, '--state', 'open', '--search', f'in:title "{title}"', '--json', 'number,title,url'], text=True, capture_output=True, check=True)
    items = json.loads(lookup.stdout or '[]')
    if items:
        item = items[0]
        return {'number': item['number'], 'title': item['title'], 'url': item['url'], 'created': False}
    created = subprocess.run(['gh', 'issue', 'create', '--repo', repo, '--title', title, '--body', body], text=True, capture_output=True, check=True)
    url = created.stdout.strip().splitlines()[-1]
    number = int(url.rstrip('/').split('/')[-1])
    return {'number': number, 'title': title, 'url': url, 'created': True}


def ensure_selfevo_pr(*, repo: str, head_branch: str, base_branch: str, title: str, body: str, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return {
            'number': None,
            'url': None,
            'head_branch': head_branch,
            'base_branch': base_branch,
            'title': title,
            'created': False,
            'dry_run': True,
        }
    lookup = subprocess.run(['gh', 'pr', 'list', '--repo', repo, '--state', 'open', '--head', head_branch, '--json', 'number,title,url,headRefName,baseRefName'], text=True, capture_output=True, check=True)
    items = json.loads(lookup.stdout or '[]')
    if items:
        item = items[0]
        return {
            'number': item['number'],
            'url': item['url'],
            'head_branch': item.get('headRefName') or head_branch,
            'base_branch': item.get('baseRefName') or base_branch,
            'title': item['title'],
            'created': False,
            'dry_run': False,
        }
    created = subprocess.run(['gh', 'pr', 'create', '--repo', repo, '--head', head_branch, '--base', base_branch, '--title', title, '--body', body], text=True, capture_output=True, check=True)
    url = created.stdout.strip().splitlines()[-1]
    number = int(url.rstrip('/').split('/')[-1])
    return {
        'number': number,
        'url': url,
        'head_branch': head_branch,
        'base_branch': base_branch,
        'title': title,
        'created': True,
        'dry_run': False,
    }


def merge_selfevo_pr(*, repo: str, pr_number: int, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return {'pr_number': pr_number, 'merged': True, 'dry_run': True}
    subprocess.run(['gh', 'pr', 'merge', '--repo', repo, str(pr_number), '--squash', '--delete-branch'], text=True, capture_output=True, check=True)
    return {'pr_number': pr_number, 'merged': True, 'dry_run': False}


def _github_issue_state(*, repo: str, issue_number: int) -> str | None:
    try:
        result = subprocess.run(
            ['gh', 'issue', 'view', str(issue_number), '--repo', repo, '--json', 'state', '--jq', '.state'],
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip().upper() or None
    except Exception:
        return None


def close_selfevo_issue_if_open(*, repo: str, issue_number: int) -> dict[str, Any]:
    before = _github_issue_state(repo=repo, issue_number=issue_number)
    attempted_close = False
    close_error = None
    if before == 'OPEN':
        attempted_close = True
        try:
            subprocess.run(['gh', 'issue', 'close', str(issue_number), '--repo', repo, '--reason', 'completed'], text=True, capture_output=True, check=True)
        except subprocess.CalledProcessError as exc:
            close_error = (exc.stderr or exc.stdout or str(exc)).strip()
    after = _github_issue_state(repo=repo, issue_number=issue_number)
    return {'issue_number': issue_number, 'state_before': before, 'state_after': after, 'attempted_close': attempted_close, 'close_error': close_error}


def commit_and_push_self_evolution(repo_root: Path, message: str, remote_name: str = 'origin', branch: str | None = None) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    current_branch = _git(repo_root, 'branch', '--show-current') or 'detached'
    push_branch = branch or current_branch
    tracked_status = _git(repo_root, 'status', '--porcelain', '--untracked-files=no')
    if not tracked_status:
        return {
            'created_commit': False,
            'pushed': False,
            'branch': push_branch,
            'message': message,
            'commit': _git(repo_root, 'rev-parse', 'HEAD'),
            'remote_name': remote_name,
        }
    _git(repo_root, 'add', '-u')
    subprocess.run(['git', 'commit', '-m', message], cwd=repo_root, check=True, text=True, capture_output=True)
    commit = _git(repo_root, 'rev-parse', 'HEAD')
    subprocess.run(['git', 'push', remote_name, f'HEAD:{push_branch}'], cwd=repo_root, check=True, text=True, capture_output=True)
    return {
        'created_commit': True,
        'pushed': True,
        'branch': push_branch,
        'message': message,
        'commit': commit,
        'remote_name': remote_name,
    }
