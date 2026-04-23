#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from nanobot.runtime.autoevolve import (
    apply_candidate_release,
    commit_and_push_self_evolution,
    create_candidate_release,
    create_self_mutation_request,
    health_check_release,
    rollback_release,
    write_failure_learning_artifact,
    write_guarded_evolution_state,
)

repo_root = Path(os.environ.get('NANOBOT_REPO_ROOT', '/home/ozand/herkoot/Projects/nanobot'))
workspace = Path(os.environ.get('NANOBOT_WORKSPACE', '/home/ozand/herkoot/Projects/nanobot/workspace'))
wait_seconds = int(os.environ.get('NANOBOT_AUTOEVO_WAIT_SECONDS', '300'))
max_age = int(os.environ.get('NANOBOT_AUTOEVO_MAX_REPORT_AGE_SECONDS', '600'))
commit_message = os.environ.get('NANOBOT_AUTOEVO_COMMIT_MESSAGE', 'autoevolve: bounded self-update')
publish_remote_name = os.environ.get('NANOBOT_AUTOEVO_REMOTE_NAME', 'origin')
publish_remote_branch = os.environ.get('NANOBOT_AUTOEVO_REMOTE_BRANCH', 'main')
source_remote_name = os.environ.get('NANOBOT_AUTOEVO_SOURCE_REMOTE_NAME', 'origin')
source_remote_branch = os.environ.get('NANOBOT_AUTOEVO_SOURCE_REMOTE_BRANCH', 'main')

def _load_json(path: Path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

try:
    current_plan = _load_json(workspace / 'state' / 'goals' / 'current.json')
    feedback = current_plan.get('feedback_decision') if isinstance(current_plan.get('feedback_decision'), dict) else None
    request = create_self_mutation_request(
        workspace=workspace,
        objective=(feedback.get('selected_task_title') if feedback else None) or current_plan.get('current_task') or 'apply the next bounded self-evolution change safely',
        source_task_id=(feedback.get('selected_task_id') if feedback else None) or current_plan.get('current_task_id') or 'guarded-self-evolve',
        commit_message=commit_message,
        goal_id=current_plan.get('goal_id') or current_plan.get('active_goal'),
        current_task_id=current_plan.get('current_task_id'),
        selected_task_id=feedback.get('selected_task_id') if feedback else None,
        selected_task_title=feedback.get('selected_task_title') if feedback else None,
        selection_source=feedback.get('selection_source') if feedback else current_plan.get('task_selection_source'),
        selected_tasks=current_plan.get('selected_tasks'),
        feedback_decision=feedback,
        mutation_lane=current_plan.get('mutation_lane') if isinstance(current_plan.get('mutation_lane'), dict) else None,
    )
    commit_result = commit_and_push_self_evolution(repo_root=repo_root, message=commit_message, remote_name=source_remote_name, branch=source_remote_branch)
    candidate = create_candidate_release(repo_root=repo_root, workspace=workspace, remote_name=source_remote_name, branch=source_remote_branch)
    apply_record = apply_candidate_release(workspace=workspace, candidate_record=candidate)
    if wait_seconds:
        time.sleep(wait_seconds)
    health = health_check_release(workspace=workspace, max_report_age_seconds=max_age)
    state = write_guarded_evolution_state(workspace=workspace)
    result = {
        'ok': health.get('ok'),
        'request': request,
        'commit': commit_result,
        'candidate': candidate,
        'apply': apply_record,
        'health': health,
        'state': state,
    }
    if not health.get('ok'):
        previous = apply_record.get('previous_release_dir')
        rollback = None
        learning = None
        if previous:
            rollback = rollback_release(
                workspace=workspace,
                failed_candidate_record=candidate,
                previous_release_dir=Path(previous),
            )
            learning = write_failure_learning_artifact(
                workspace=workspace,
                failed_candidate_record=candidate,
                health_result=health,
                rollback_result=rollback,
            )
        result['rollback'] = rollback
        result['learning'] = learning
        result['state'] = write_guarded_evolution_state(workspace=workspace)
    export_result = None
    if publish_remote_name == 'selfevo':
        export_proc = subprocess.run(['python3', str(repo_root / 'scripts' / 'export_selfevo_repo.py')], cwd=repo_root, text=True, capture_output=True)
        export_result = {
            'ok': export_proc.returncode == 0,
            'exit_code': export_proc.returncode,
            'stdout_tail': (export_proc.stdout or '')[-1000:],
            'stderr_tail': (export_proc.stderr or '')[-1000:],
            'publish_remote_name': publish_remote_name,
            'publish_remote_branch': publish_remote_branch,
            'source_remote_name': source_remote_name,
            'source_remote_branch': source_remote_branch,
            'allowed_repo': os.environ.get('NANOBOT_AUTOEVO_ALLOWED_REPO', 'ozand/eeebot-self-evolving'),
            'auth_mode': 'dedicated_token' if os.environ.get('NANOBOT_SELFEVO_GITHUB_TOKEN') else 'ambient_git_auth',
        }
        export_path = workspace / 'state' / 'self_evolution' / 'runtime' / 'latest_export.json'
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(json.dumps(export_result, indent=2, ensure_ascii=False), encoding='utf-8')
        result['export'] = export_result
        result['state'] = write_guarded_evolution_state(workspace=workspace)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if health.get('ok') else 1)
except Exception as exc:
    print(json.dumps({'ok': False, 'error': str(exc)}, indent=2, ensure_ascii=False))
    raise SystemExit(1)
