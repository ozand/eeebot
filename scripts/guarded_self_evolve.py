#!/usr/bin/env python3
from __future__ import annotations

import json
import os
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

try:
    request = create_self_mutation_request(
        workspace=workspace,
        objective='apply the next bounded self-evolution change safely',
        source_task_id='guarded-self-evolve',
        commit_message=commit_message,
    )
    commit_result = commit_and_push_self_evolution(repo_root=repo_root, message=commit_message)
    candidate = create_candidate_release(repo_root=repo_root, workspace=workspace)
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
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if health.get('ok') else 1)
except Exception as exc:
    print(json.dumps({'ok': False, 'error': str(exc)}, indent=2, ensure_ascii=False))
    raise SystemExit(1)
