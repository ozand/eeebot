from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
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
        'last_apply': _load(root / 'runtime' / 'latest_apply.json'),
        'last_rollback': _load(root / 'runtime' / 'latest_rollback.json'),
        'last_failure_learning': _load(root / 'failure_learning' / 'latest.json'),
        'last_export': _load(root / 'runtime' / 'latest_export.json'),
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


def apply_candidate_release(workspace: Path, candidate_record: dict[str, Any]) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = _self_evolution_root(workspace)
    if not candidate_record.get('clean_worktree'):
        raise ValueError('candidate release must come from a clean tracked worktree')
    if not candidate_record.get('remote_commit_visible'):
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
