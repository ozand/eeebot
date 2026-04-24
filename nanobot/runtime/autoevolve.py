from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_stamp(now: datetime | None = None) -> str:
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    return current.strftime('%Y%m%dT%H%M%SZ')


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(['git', *args], cwd=repo_root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')


def _self_evolution_root(workspace: Path) -> Path:
    return workspace / 'state' / 'self_evolution'


def derive_selfevo_branch_name(*, issue_number: int, source_task_id: str | None) -> str:
    raw = (source_task_id or 'self-evolution').lower()
    slug = re.sub(r'[^a-z0-9]+', '-', raw).strip('-') or 'self-evolution'
    prefix = 'fix' if 'fail' in slug or 'repair' in slug or 'analyze' in slug else 'chore'
    return f'{prefix}/issue-{issue_number}-{slug}'


def ensure_selfevo_issue(*, repo: str, title: str, body: str) -> dict[str, Any]:
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


def create_self_mutation_request(
    *,
    workspace: Path,
    objective: str,
    source_task_id: str | None,
    commit_message: str,
    goal_id: str | None = None,
    current_task_id: str | None = None,
    selected_task_id: str | None = None,
    selected_task_title: str | None = None,
    selection_source: str | None = None,
    selected_tasks: str | None = None,
    feedback_decision: dict[str, Any] | None = None,
    mutation_lane: dict[str, Any] | None = None,
    selfevo_issue: dict[str, Any] | None = None,
    selfevo_branch: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = _self_evolution_root(workspace)
    requests_dir = root / 'requests'
    stamp = _utc_stamp(now)
    request_id = f'request-{stamp}'
    payload = {
        'schema_version': 'autoevolve-request-v1',
        'request_id': request_id,
        'created_at_utc': stamp,
        'objective': objective,
        'source_task_id': source_task_id,
        'goal_id': goal_id,
        'current_task_id': current_task_id,
        'selected_task_id': selected_task_id,
        'selected_task_title': selected_task_title,
        'selection_source': selection_source,
        'selected_tasks': selected_tasks,
        'feedback_decision': feedback_decision,
        'mutation_lane': mutation_lane,
        'selfevo_issue': selfevo_issue,
        'selfevo_branch': selfevo_branch,
        'commit_message': commit_message,
        'status': 'pending',
    }
    _write_json(requests_dir / f'{request_id}.json', payload)
    _write_json(requests_dir / 'latest.json', payload)
    return payload


def write_guarded_evolution_state(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = _self_evolution_root(workspace)
    def _load(path: Path):
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return None
    payload = {
        'schema_version': 'autoevolve-state-v1',
        'current_candidate': _load(root / 'candidates' / 'latest.json'),
        'latest_request': _load(root / 'requests' / 'latest.json'),
        'selfevo_issue': (_load(root / 'requests' / 'latest.json') or {}).get('selfevo_issue') if isinstance(_load(root / 'requests' / 'latest.json'), dict) else None,
        'selfevo_branch': (_load(root / 'requests' / 'latest.json') or {}).get('selfevo_branch') if isinstance(_load(root / 'requests' / 'latest.json'), dict) else None,
        'last_apply': _load(root / 'runtime' / 'latest_apply.json'),
        'last_rollback': _load(root / 'runtime' / 'latest_rollback.json'),
        'last_failure_learning': _load(root / 'failure_learning' / 'latest.json'),
        'last_export': _load(root / 'runtime' / 'latest_export.json'),
        'last_pr': _load(root / 'runtime' / 'latest_pr.json'),
        'last_merge': _load(root / 'runtime' / 'latest_merge.json'),
    }
    _write_json(root / 'current_state.json', payload)
    return payload


def create_candidate_release(repo_root: Path, workspace: Path, remote_name: str = 'origin', branch: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    workspace = workspace.resolve()
    commit = _git(repo_root, 'rev-parse', 'HEAD')
    current_branch = _git(repo_root, 'branch', '--show-current') or 'detached'
    push_branch = branch or current_branch
    remote_url = _git(repo_root, 'remote', 'get-url', remote_name)
    clean_worktree = _git(repo_root, 'status', '--porcelain', '--untracked-files=no') == ''
    remote_head = _git(repo_root, 'rev-parse', f'{remote_name}/{push_branch}') if push_branch != 'detached' else ''
    remote_commit_visible = bool(remote_head) and remote_head == commit
    short = commit[:12]
    stamp = _utc_stamp(now)
    candidate_id = f'candidate-{stamp}-{short}'
    root = _self_evolution_root(workspace)
    artifacts_dir = root / 'artifacts'
    candidates_dir = root / 'candidates'
    archive_path = artifacts_dir / f'{candidate_id}.tar.gz'
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['git', 'archive', '--format=tar.gz', f'--output={archive_path}', commit],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )
    record = {
        'schema_version': 'autoevolve-candidate-v1',
        'candidate_id': candidate_id,
        'created_at_utc': stamp,
        'repo_root': str(repo_root),
        'workspace': str(workspace),
        'commit': commit,
        'branch': push_branch,
        'remote_name': remote_name,
        'remote_url': remote_url,
        'remote_head': remote_head,
        'remote_commit_visible': remote_commit_visible,
        'clean_worktree': clean_worktree,
        'archive_path': str(archive_path),
    }
    record_path = candidates_dir / f'{candidate_id}.json'
    _write_json(record_path, record)
    _write_json(candidates_dir / 'latest.json', record)
    return record


def write_candidate_blocked_status(workspace: Path, candidate_record: dict[str, Any], reason: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = _self_evolution_root(workspace)
    runtime_dir = root / 'runtime'
    stale = reason == 'remote_commit_not_visible'
    payload = {
        'schema_version': 'autoevolve-blocked-v1',
        'ok': False,
        'status': 'blocked',
        'reason': reason,
        'candidate_id': candidate_record.get('candidate_id'),
        'commit': candidate_record.get('commit'),
        'remote_name': candidate_record.get('remote_name'),
        'branch': candidate_record.get('branch'),
        'remote_head': candidate_record.get('remote_head'),
        'remote_commit_visible': bool(candidate_record.get('remote_commit_visible')),
        'clean_worktree': bool(candidate_record.get('clean_worktree')),
        'stale_candidate': stale,
        'recommended_next_action': (
            'regenerate candidate from current remote-visible branch head before apply'
            if stale else
            'clean tracked worktree before creating/applying candidate'
        ),
    }
    _write_json(runtime_dir / 'latest_blocked.json', payload)
    if stale:
        marked = dict(candidate_record)
        marked['status'] = 'stale'
        marked['stale_reason'] = reason
        marked['recommended_next_action'] = payload['recommended_next_action']
        candidates_dir = root / 'candidates'
        _write_json(candidates_dir / 'latest.json', marked)
    write_guarded_evolution_state(workspace)
    return payload


def apply_candidate_release(workspace: Path, candidate_record: dict[str, Any]) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = _self_evolution_root(workspace)
    if not candidate_record.get('clean_worktree'):
        write_candidate_blocked_status(workspace, candidate_record, 'dirty_worktree')
        raise ValueError('candidate release must come from a clean tracked worktree')
    if not candidate_record.get('remote_commit_visible'):
        write_candidate_blocked_status(workspace, candidate_record, 'remote_commit_not_visible')
        raise ValueError('candidate release commit must be visible on remote before apply')
    runtime_dir = root / 'runtime'
    releases_dir = runtime_dir / 'releases'
    current_link = runtime_dir / 'current'
    release_dir = releases_dir / candidate_record['candidate_id']
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(candidate_record['archive_path'], 'r:gz') as tar:
        tar.extractall(release_dir, filter='data')
    previous_release_dir = None
    if current_link.exists() or current_link.is_symlink():
        try:
            previous_release_dir = str(current_link.resolve())
        except FileNotFoundError:
            previous_release_dir = None
        if current_link.is_symlink() or current_link.exists():
            current_link.unlink()
    current_link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(release_dir, current_link)
    record = {
        'schema_version': 'autoevolve-apply-v1',
        'candidate_id': candidate_record['candidate_id'],
        'release_dir': str(release_dir.resolve()),
        'previous_release_dir': previous_release_dir,
        'current_link': str(current_link),
    }
    _write_json(root / 'runtime' / 'latest_apply.json', record)
    return record


def health_check_release(workspace: Path, max_report_age_seconds: int = 600, now: datetime | None = None) -> dict[str, Any]:
    workspace = workspace.resolve()
    state = workspace / 'state'
    report_dir = state / 'reports'
    summary = state / 'control_plane' / 'current_summary.json'
    current = state / 'goals' / 'current.json'
    reasons: list[str] = []
    latest_report = None
    reports = sorted(report_dir.glob('evolution-*.json'), key=lambda p: p.stat().st_mtime)
    if not reports:
        reasons.append('missing_report')
    else:
        latest_report = reports[-1]
        current_ts = (now or datetime.now(timezone.utc)).timestamp()
        age = current_ts - latest_report.stat().st_mtime
        if age > max_report_age_seconds:
            reasons.append('stale_report')
    if not summary.exists():
        reasons.append('missing_control_plane_summary')
    if not current.exists():
        reasons.append('missing_current_plan')
    return {
        'schema_version': 'autoevolve-health-v1',
        'ok': not reasons,
        'reasons': reasons,
        'latest_report_path': str(latest_report) if latest_report else None,
    }


def rollback_release(workspace: Path, failed_candidate_record: dict[str, Any], previous_release_dir: Path | None) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = _self_evolution_root(workspace)
    current_link = root / 'runtime' / 'current'
    target = previous_release_dir.resolve() if previous_release_dir else None
    if target is None:
        raise ValueError('previous_release_dir is required for rollback')
    if current_link.exists() or current_link.is_symlink():
        current_link.unlink()
    os.symlink(target, current_link)
    record = {
        'schema_version': 'autoevolve-rollback-v1',
        'candidate_id': failed_candidate_record['candidate_id'],
        'rolled_back_to_release_dir': str(target),
    }
    _write_json(root / 'runtime' / 'latest_rollback.json', record)
    return record


def write_failure_learning_artifact(workspace: Path, failed_candidate_record: dict[str, Any], health_result: dict[str, Any], rollback_result: dict[str, Any]) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = _self_evolution_root(workspace)
    payload = {
        'schema_version': 'autoevolve-failure-learning-v1',
        'candidate_id': failed_candidate_record['candidate_id'],
        'failed_commit': failed_candidate_record.get('commit'),
        'health_reasons': list(health_result.get('reasons') or []),
        'rollback_target': rollback_result.get('rolled_back_to_release_dir'),
        'learning_summary': 'Candidate failed health gate; next cycle should avoid the same failure pattern and inspect rollback evidence first.',
    }
    path = root / 'failure_learning' / f"{failed_candidate_record['candidate_id']}.json"
    payload['path'] = str(path)
    _write_json(path, payload)
    _write_json(root / 'failure_learning' / 'latest.json', payload)
    return payload
