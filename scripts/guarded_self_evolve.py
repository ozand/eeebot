#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from nanobot.runtime.autoevolve import (
    apply_candidate_release,
    create_candidate_release,
    create_self_mutation_request,
    derive_selfevo_branch_name,
    health_check_release,
    rollback_release,
    write_candidate_blocked_status,
    write_failure_learning_artifact,
    write_guarded_evolution_state,
)

repo_root = Path(os.environ.get('NANOBOT_REPO_ROOT', '/home/ozand/herkoot/Projects/nanobot'))
workspace = Path(os.environ.get('NANOBOT_WORKSPACE', '/home/ozand/herkoot/Projects/nanobot/workspace'))
wait_seconds = int(os.environ.get('NANOBOT_AUTOEVO_WAIT_SECONDS', '300'))
max_age = int(os.environ.get('NANOBOT_AUTOEVO_MAX_REPORT_AGE_SECONDS', '600'))
commit_message = os.environ.get('NANOBOT_AUTOEVO_COMMIT_MESSAGE', 'autoevolve: bounded self-update')

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
    
    # Generate deterministic local IDs instead of network calls
    source_task_id = (feedback.get('selected_task_id') if feedback else None) or current_plan.get('current_task_id') or current_plan.get('task_selection_source')
    pseudo_issue_number = int(time.time())
    selfevo_branch = derive_selfevo_branch_name(issue_number=pseudo_issue_number, source_task_id=source_task_id)
    selfevo_issue = {'number': pseudo_issue_number, 'title': 'Local Bounded Evolution', 'created': False}

    request = create_self_mutation_request(
        workspace=workspace,
        objective=(feedback.get('selected_task_title') if feedback else None) or current_plan.get('current_task') or 'apply the next bounded self-evolution change safely',
        source_task_id=source_task_id,
        commit_message=commit_message,
        goal_id=current_plan.get('goal_id') or current_plan.get('active_goal'),
        current_task_id=current_plan.get('current_task_id'),
        selected_task_id=feedback.get('selected_task_id') if feedback else None,
        selected_task_title=feedback.get('selected_task_title') if feedback else None,
        selection_source=feedback.get('selection_source') if feedback else current_plan.get('task_selection_source'),
        selected_tasks=current_plan.get('selected_tasks'),
        feedback_decision=feedback,
        mutation_lane=current_plan.get('mutation_lane') if isinstance(current_plan.get('mutation_lane'), dict) else None,
        selfevo_issue=selfevo_issue,
        selfevo_branch=selfevo_branch,
    )

    # Local commit instead of push
    subprocess.run(['git', 'add', '-A'], cwd=repo_root, check=False)
    subprocess.run(['git', 'commit', '-m', commit_message], cwd=repo_root, check=False)

    candidate = create_candidate_release(repo_root=repo_root, workspace=workspace)
    
    if not candidate.get('clean_worktree'):
        blocked = write_candidate_blocked_status(workspace, candidate, 'dirty_worktree')
        result = {'ok': False, 'controlled_block': True, 'request': request, 'candidate': candidate, 'blocked': blocked, 'state': write_guarded_evolution_state(workspace)}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        raise SystemExit(0)

    apply_record = apply_candidate_release(workspace=workspace, candidate_record=candidate)
    
    if wait_seconds:
        time.sleep(wait_seconds)
    
    health = health_check_release(workspace=workspace, max_report_age_seconds=max_age)
    state = write_guarded_evolution_state(workspace=workspace)
    
    result = {
        'ok': health.get('ok'),
        'request': request,
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
    
    # Write a promotion packet for async export
    promo_packet = {
        'schema_version': 'promotion-packet-v1',
        'candidate_id': candidate.get('candidate_id'),
        'commit': candidate.get('commit'),
        'branch': selfevo_branch,
        'health_ok': health.get('ok'),
        'request': request
    }
    promo_dir = workspace / 'state' / 'promotions'
    promo_dir.mkdir(parents=True, exist_ok=True)
    promo_file = promo_dir / f"{candidate.get('candidate_id')}.json"
    promo_file.write_text(json.dumps(promo_packet, indent=2, ensure_ascii=False), encoding='utf-8')

    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if health.get('ok') else 1)

except Exception as exc:
    print(json.dumps({'ok': False, 'error': str(exc)}, indent=2, ensure_ascii=False))
    raise SystemExit(1)
