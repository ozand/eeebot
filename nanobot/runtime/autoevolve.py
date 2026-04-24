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


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _self_evolution_root(workspace: Path) -> Path:
    return workspace / 'state' / 'self_evolution'


def derive_selfevo_branch_name(*, issue_number: int, source_task_id: str | None) -> str:
    raw = (source_task_id or 'self-evolution').lower()
    slug = re.sub(r'[^a-z0-9]+', '-', raw).strip('-') or 'self-evolution'
    prefix = 'fix' if 'fail' in slug or 'repair' in slug or 'analyze' in slug else 'chore'
    return f'{prefix}/issue-{issue_number}-{slug}'


def _semantic_lane_slug(value: str | None) -> str | None:
    if not value:
        return None
    slug = re.sub(r'[^a-z0-9]+', '-', str(value).lower()).strip('-')
    return slug or None


def _record_matches_source_task(record: dict[str, Any], source_task_id: str | None) -> bool:
    lane_slug = _semantic_lane_slug(source_task_id)
    if not lane_slug:
        return False
    branch = _semantic_lane_slug(str(record.get('selfevo_branch') or ''))
    issue = record.get('selfevo_issue') if isinstance(record.get('selfevo_issue'), dict) else {}
    issue_title = _semantic_lane_slug(str(issue.get('title') or record.get('issue_title') or ''))
    task_id = _semantic_lane_slug(str(record.get('source_task_id') or record.get('task_id') or record.get('current_task_id') or ''))
    return any(lane_slug in candidate for candidate in (branch or '', issue_title or '', task_id or ''))


def resolve_terminal_selfevo_issue(*, workspace: Path, source_task_id: str | None) -> dict[str, Any] | None:
    root = _self_evolution_root(workspace.resolve())
    candidates = [
        _load_json(root / 'runtime' / 'latest_issue_lifecycle.json'),
        _load_json(root / 'runtime' / 'latest_noop.json'),
    ]
    for record in candidates:
        if not isinstance(record, dict):
            continue
        if not _record_matches_source_task(record, source_task_id):
            continue
        status = str(record.get('status') or '')
        github_issue_state = str(record.get('github_issue_state') or '').upper() or None
        retry_allowed = record.get('retry_allowed')
        if status == 'terminal_merged' or github_issue_state == 'CLOSED' or (status == 'terminal_noop' and retry_allowed is False):
            issue = record.get('selfevo_issue') if isinstance(record.get('selfevo_issue'), dict) else None
            if not isinstance(issue, dict):
                issue = {
                    'number': record.get('issue_number'),
                    'title': record.get('issue_title'),
                    'url': record.get('issue_url'),
                }
            if issue.get('number') is None and not issue.get('title'):
                continue
            return {
                'number': issue.get('number'),
                'title': issue.get('title'),
                'url': issue.get('url'),
                'created': False,
                'reused_terminal_lane': True,
                'terminal_status': status,
                'github_issue_state': github_issue_state,
                'retry_allowed': retry_allowed,
                'selfevo_branch': record.get('selfevo_branch'),
                'selfevo_issue': issue,
            }
    return None


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
        'last_noop': _load(root / 'runtime' / 'latest_noop.json'),
        'last_issue_lifecycle': _load(root / 'runtime' / 'latest_issue_lifecycle.json'),
    }
    _write_json(root / 'current_state.json', payload)
    return payload


def _export_is_noop(export_result: dict[str, Any]) -> bool:
    text = f"{export_result.get('stdout_tail') or ''}\n{export_result.get('stderr_tail') or ''}".lower()
    return 'exported-noop' in text or 'no commits between' in text or 'head sha can' in text


def write_noop_export_status(
    *,
    workspace: Path,
    export_result: dict[str, Any],
    selfevo_issue: dict[str, Any] | None,
    selfevo_branch: str | None,
    reason: str = 'exported_noop',
) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = _self_evolution_root(workspace)
    payload = {
        'schema_version': 'autoevolve-noop-v1',
        'ok': True,
        'status': 'terminal_noop',
        'reason': reason,
        'selfevo_issue': selfevo_issue,
        'selfevo_branch': selfevo_branch,
        'publish_repo': export_result.get('publish_repo'),
        'publish_remote_branch': export_result.get('publish_remote_branch') or selfevo_branch,
        'export': export_result,
        'pr_creation_allowed': False,
        'retry_allowed': False,
        'recommended_next_action': 'skip PR creation for no-op export; select a new bounded mutation or close the already terminal task',
    }
    _write_json(root / 'runtime' / 'latest_noop.json', payload)
    write_guarded_evolution_state(workspace)
    return payload


def write_issue_lifecycle_status(
    *,
    workspace: Path,
    selfevo_issue: dict[str, Any] | None,
    selfevo_branch: str | None,
    pr: dict[str, Any] | None,
    action: str,
    github_issue_state: str | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = _self_evolution_root(workspace)
    pr = pr if isinstance(pr, dict) else {}
    merged = bool(pr.get('merged')) or str(pr.get('state') or '').upper() == 'MERGED'
    normalized_issue_state = str(github_issue_state or '').upper() or None
    issue_still_open = merged and normalized_issue_state == 'OPEN'
    if issue_still_open:
        status = 'terminal_merged_issue_still_open'
        linked_action = 'still_open_after_merge'
    else:
        status = 'terminal_merged' if merged else 'recorded'
        linked_action = action
    payload = {
        'schema_version': 'autoevolve-issue-lifecycle-v1',
        'ok': True,
        'status': status,
        'selfevo_issue': selfevo_issue,
        'issue_number': (selfevo_issue or {}).get('number'),
        'issue_title': (selfevo_issue or {}).get('title'),
        'selfevo_branch': selfevo_branch,
        'pr': pr,
        'pr_number': pr.get('number'),
        'linked_issue_action': linked_action,
        'github_issue_state': normalized_issue_state,
        'retry_allowed': (not merged) or issue_still_open,
    }
    _write_json(root / 'runtime' / 'latest_issue_lifecycle.json', payload)
    write_guarded_evolution_state(workspace)
    return payload


def runtime_parity_summary(
    *,
    local_plan: dict[str, Any] | None,
    live_plan: dict[str, Any] | None,
    live_artifacts: dict[str, bool] | None = None,
) -> dict[str, Any]:
    local_plan = local_plan if isinstance(local_plan, dict) else {}
    live_plan = live_plan if isinstance(live_plan, dict) else {}
    live_artifacts = live_artifacts if isinstance(live_artifacts, dict) else {}
    reasons: list[str] = []
    local_feedback = local_plan.get('feedback_decision') if isinstance(local_plan.get('feedback_decision'), dict) else None
    live_feedback = live_plan.get('feedback_decision') if isinstance(live_plan.get('feedback_decision'), dict) else None
    if local_feedback and not live_feedback:
        reasons.append('feedback_decision_missing_on_live')
    if live_plan and not live_feedback:
        reasons.append('live_feedback_decision_missing')
    local_task = local_plan.get('current_task_id') or local_plan.get('current_task')
    live_task = live_plan.get('current_task_id') or live_plan.get('current_task') or live_plan.get('selected_tasks')
    if local_task and live_task and local_task not in str(live_task):
        reasons.append('current_task_drift')
    missing_live_artifacts = [name for name, present in live_artifacts.items() if not present]
    if missing_live_artifacts:
        reasons.append('live_hadi_artifacts_missing')
    live_source = live_plan.get('task_selection_source')
    legacy_reward_loop = (
        str(live_source or '') == 'recorded_current_task'
        and 'record-reward' in str(live_task or '')
        and not live_feedback
    ) or (bool(missing_live_artifacts) and not live_feedback and 'record-reward' in str(live_task or ''))
    state = 'legacy_reward_loop' if legacy_reward_loop else ('healthy' if not reasons else 'degraded')
    return {
        'schema_version': 'runtime-parity-v1',
        'state': state,
        'reasons': reasons,
        'missing_live_artifacts': missing_live_artifacts,
        'local_current_task_id': local_task,
        'live_current_task_id': live_task,
        'local_feedback_decision': local_feedback,
        'live_feedback_decision': live_feedback,
        'live_task_selection_source': live_source,
    }



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
