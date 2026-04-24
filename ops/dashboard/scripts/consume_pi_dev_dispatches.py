#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
REQUEST_DIR = ROOT / 'control' / 'pi_dev_requests'
BUNDLE_DIR = ROOT / 'control' / 'pi_dev_bundles'
DISPATCH_DIR = ROOT / 'control' / 'pi_dev_dispatches'
LATEST_DISPATCH_PATH = ROOT / 'control' / 'pi_dev_dispatch.json'
SCRIPT_NAME = 'consume_pi_dev_dispatches.py'

REQUEST_ELIGIBLE_STATUSES = {'bundled'}
QUEUE_ELIGIBLE_STATUSES = {'pi_dev_bundled'}
DISPATCH_READY_STATUS = 'pi_dev_dispatch_ready'


def now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f'.{path.name}.tmp')
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp_path.replace(path)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f'.{path.name}.tmp')
    tmp_path.write_text(content, encoding='utf-8')
    tmp_path.replace(path)


def slugify(value: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', value.strip())
    slug = re.sub(r'-{2,}', '-', slug).strip('-._')
    return slug or 'task'


def timestamp_slug(timestamp: str) -> str:
    return timestamp.replace('-', '').replace(':', '').replace('.', '')


def task_key(task: dict[str, Any]) -> str:
    for key in ('queue_task_key', 'dedupe_key', 'active_goal', 'report_source', 'diagnosis', 'failure_class', 'remediation_class'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return slugify(value)
    return 'task'


def candidate_paths(request_path: Path, request: dict[str, Any]) -> list[str]:
    candidates: list[str] = [str(request_path)]
    for key in ('source_execution_request_path', 'execution_request_path', 'source_pi_dev_request_path'):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value)
    return candidates


def matching_queue_task(tasks: list[Any], request_path: Path, request: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None]:
    request_candidates = set(candidate_paths(request_path, request))
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        task_candidates = []
        for key in ('pi_dev_request_path', 'execution_request_path', 'source_execution_request_path', 'executor_handoff_path'):
            value = task.get(key)
            if isinstance(value, str) and value.strip():
                task_candidates.append(value)
        if request_candidates.intersection(task_candidates):
            return index, task
    return None, None


def bundle_path_value(request: dict[str, Any]) -> str | None:
    for key in ('bundle_path', 'pi_dev_bundle_path'):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def bundle_created_at_value(request: dict[str, Any]) -> str | None:
    for key in ('bundled_at', 'pi_dev_bundled_at'):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def request_is_dispatched(request: dict[str, Any]) -> bool:
    return request.get('status') == DISPATCH_READY_STATUS or request.get('pi_dev_dispatch_path') is not None


def queue_task_is_dispatched(task: dict[str, Any]) -> bool:
    return task.get('status') == DISPATCH_READY_STATUS or task.get('pi_dev_dispatch_path') is not None


def source_bundle_path(request: dict[str, Any], request_path: Path, queue_task: dict[str, Any] | None) -> Path:
    request_value = bundle_path_value(request)
    if request_value:
        return Path(request_value)
    if queue_task is not None:
        queue_value = queue_task.get('pi_dev_bundle_path')
        if isinstance(queue_value, str) and queue_value.strip():
            return Path(queue_value)
    return BUNDLE_DIR / request_path.name


def prompt_text(request: dict[str, Any]) -> str:
    goal = request.get('active_goal') or 'the active goal'
    diagnosis = request.get('diagnosis') or 'the current blocker'
    failure_class = request.get('failure_class') or 'the current failure class'
    remediation_class = request.get('remediation_class') or 'the current remediation class'
    recommendation = request.get('recommended_remediation_action') or 'the smallest safe next step'
    recommendation = recommendation.rstrip('.')
    return (
        f'Execute one bounded remediation slice for {goal}. '\
        f'Focus on {diagnosis} caused by {failure_class} under {remediation_class}. '\
        f'Apply this action: {recommendation}. '\
        'Keep the change bounded to the smallest truthful file-level update, '\
        'then verify exactly once, and if blocked, stop and report the blocker without widening scope.'
    )


def runnable_command(prompt_path: Path) -> str:
    return (
        f'cd {DISPATCH_DIR} && '
        'env PATH="$HOME/.hermes/node/bin:$PATH" '
        'pi --mode json -p --no-session --no-tools --provider hermes_pi_qwen --model coder-model '
        f'< {prompt_path.name}'
    )


def build_dispatch_payload(
    request: dict[str, Any],
    request_path: Path,
    bundle_path: Path,
    dispatch_created_at: str,
    prompt_path: Path,
    script_path: Path,
    queue_task: dict[str, Any] | None,
) -> dict[str, Any]:
    command = runnable_command(prompt_path)
    payload: dict[str, Any] = {
        'dispatch_created_at': dispatch_created_at,
        'dispatch_created_by': SCRIPT_NAME,
        'dispatch_status': DISPATCH_READY_STATUS,
        'dispatch_mode': 'bridge_only',
        'direct_invocation_viable': False,
        'direct_invocation_result_status': 'not_run',
        'source_pi_dev_request_path': str(request_path),
        'source_pi_dev_request_status_before_dispatch': request.get('status'),
        'source_pi_dev_bundle_path': str(bundle_path),
        'source_pi_dev_bundle_status_before_dispatch': request.get('status'),
        'source_queue_path': str(QUEUE_PATH),
        'source_queue_task_key': task_key(queue_task) if queue_task is not None else None,
        'source_queue_task_status_before_dispatch': queue_task.get('status') if queue_task is not None else None,
        'prompt_path': str(prompt_path),
        'command_path': str(script_path),
        'runnable_command': command,
        'prompt_text': prompt_text(request),
        'pi_binary': str(Path.home() / '.hermes' / 'node' / 'bin' / 'pi'),
        'node_binary': str(Path.home() / '.hermes' / 'node' / 'bin' / 'node'),
        'auth_path': str(Path.home() / '.pi' / 'agent' / 'auth.json'),
        'models_path': str(Path.home() / '.pi' / 'agent' / 'models.json'),
    }
    if queue_task is not None:
        payload['source_queue_task'] = {
            'status': queue_task.get('status'),
            'created_at': queue_task.get('created_at'),
            'active_goal': queue_task.get('active_goal'),
            'diagnosis': queue_task.get('diagnosis'),
            'failure_class': queue_task.get('failure_class'),
            'remediation_class': queue_task.get('remediation_class'),
            'recommended_remediation_action': queue_task.get('recommended_remediation_action'),
            'pi_dev_request_path': queue_task.get('pi_dev_request_path'),
            'pi_dev_bundle_path': queue_task.get('pi_dev_bundle_path'),
        }
    payload['source_pi_dev_request'] = {
        'status': request.get('status'),
        'created_at': request.get('created_at'),
        'requested_executor': request.get('requested_executor'),
        'active_goal': request.get('active_goal'),
        'diagnosis': request.get('diagnosis'),
        'failure_class': request.get('failure_class'),
        'remediation_class': request.get('remediation_class'),
        'recommended_remediation_action': request.get('recommended_remediation_action'),
        'pi_dev_request_path': str(request_path),
        'pi_dev_bundle_path': str(bundle_path),
    }
    payload['source_pi_dev_bundle'] = load_json(bundle_path, {'bundle_path': str(bundle_path)})
    return payload


def main() -> None:
    queue = load_json(QUEUE_PATH, {'tasks': []})
    tasks = queue.get('tasks') if isinstance(queue, dict) else []
    if not isinstance(tasks, list) or not tasks:
        print(json.dumps({'consumed': False, 'reason': 'no_queue_task'}, ensure_ascii=False))
        return

    request_files = sorted(REQUEST_DIR.glob('*.json'), key=lambda path: path.name)
    if not request_files:
        print(json.dumps({'consumed': False, 'reason': 'no_pi_dev_request'}, ensure_ascii=False))
        return

    for request_path in request_files:
        request = load_json(request_path, None)
        if not isinstance(request, dict):
            continue

        if request_is_dispatched(request):
            continue
        if request.get('status') not in REQUEST_ELIGIBLE_STATUSES:
            continue

        matched_index, matched_task = matching_queue_task(tasks, request_path, request)
        if matched_task is None:
            continue
        if matched_task.get('status') not in QUEUE_ELIGIBLE_STATUSES:
            continue
        if queue_task_is_dispatched(matched_task):
            continue

        bundle_path = source_bundle_path(request, request_path, matched_task)
        if not bundle_path.exists():
            continue

        dispatch_created_at = now_utc()
        dispatch_stamp = timestamp_slug(dispatch_created_at)
        dispatch_name = f'{dispatch_stamp}-{task_key(matched_task)}.json'
        dispatch_path = DISPATCH_DIR / dispatch_name
        prompt_path = DISPATCH_DIR / f'{dispatch_stamp}-{task_key(matched_task)}.prompt.txt'
        script_path = DISPATCH_DIR / f'{dispatch_stamp}-{task_key(matched_task)}.sh'

        prompt = prompt_text(request)
        script = '\n'.join([
            '#!/usr/bin/env bash',
            'set -euo pipefail',
            f'cd {DISPATCH_DIR}',
            'export PATH="$HOME/.hermes/node/bin:$PATH"',
            'pi --mode json -p --no-session --no-tools --provider hermes_pi_qwen --model coder-model < "$(dirname "$0")/' + prompt_path.name + '"',
            '',
        ])

        atomic_write_text(prompt_path, prompt + '\n')
        atomic_write_text(script_path, script)
        dispatch_payload = build_dispatch_payload(
            request,
            request_path,
            bundle_path,
            dispatch_created_at,
            prompt_path,
            script_path,
            matched_task,
        )
        atomic_write_json(dispatch_path, dispatch_payload)
        atomic_write_json(LATEST_DISPATCH_PATH, dispatch_payload)

        updated_request = dict(request)
        updated_request['status'] = DISPATCH_READY_STATUS
        updated_request['pi_dev_dispatch_created_at'] = dispatch_created_at
        updated_request['pi_dev_dispatch_path'] = str(dispatch_path)
        updated_request['pi_dev_dispatch_prompt_path'] = str(prompt_path)
        updated_request['pi_dev_dispatch_script_path'] = str(script_path)
        updated_request['pi_dev_dispatch_status'] = DISPATCH_READY_STATUS
        updated_request['pi_dev_dispatch_mode'] = 'bridge_only'
        updated_request['pi_dev_dispatch_created_by'] = SCRIPT_NAME
        atomic_write_json(request_path, updated_request)

        updated_task = dict(matched_task)
        updated_task['status'] = DISPATCH_READY_STATUS
        updated_task['pi_dev_dispatch_created_at'] = dispatch_created_at
        updated_task['pi_dev_dispatch_path'] = str(dispatch_path)
        updated_task['pi_dev_dispatch_prompt_path'] = str(prompt_path)
        updated_task['pi_dev_dispatch_script_path'] = str(script_path)
        updated_task['pi_dev_dispatch_status'] = DISPATCH_READY_STATUS
        updated_task['pi_dev_dispatch_mode'] = 'bridge_only'
        updated_task['pi_dev_dispatch_created_by'] = SCRIPT_NAME
        tasks[matched_index] = updated_task
        atomic_write_json(QUEUE_PATH, {'tasks': tasks})

        updated_bundle = load_json(bundle_path, None)
        if isinstance(updated_bundle, dict):
            updated_bundle['pi_dev_dispatch_created_at'] = dispatch_created_at
            updated_bundle['pi_dev_dispatch_path'] = str(dispatch_path)
            updated_bundle['pi_dev_dispatch_prompt_path'] = str(prompt_path)
            updated_bundle['pi_dev_dispatch_script_path'] = str(script_path)
            updated_bundle['pi_dev_dispatch_status'] = DISPATCH_READY_STATUS
            updated_bundle['pi_dev_dispatch_mode'] = 'bridge_only'
            updated_bundle['pi_dev_dispatch_created_by'] = SCRIPT_NAME
            atomic_write_json(bundle_path, updated_bundle)

        output = {
            'consumed': True,
            'status': DISPATCH_READY_STATUS,
            'dispatch_mode': 'bridge_only',
            'request_path': str(request_path),
            'bundle_path': str(bundle_path),
            'dispatch_path': str(dispatch_path),
            'prompt_path': str(prompt_path),
            'script_path': str(script_path),
            'dispatch_created_at': dispatch_created_at,
            'queue_task_index': matched_index,
            'queue_task_key': task_key(updated_task),
            'queue_task_status_before_dispatch': matched_task.get('status'),
        }
        print(json.dumps(output, ensure_ascii=False))
        return

    print(json.dumps({'consumed': False, 'reason': 'no_eligible_pi_dev_bundle'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
