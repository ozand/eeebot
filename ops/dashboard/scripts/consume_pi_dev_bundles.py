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
SCRIPT_NAME = 'consume_pi_dev_bundles.py'

REQUEST_ELIGIBLE_STATUSES = {'requested'}
REQUEST_BUNDLED_STATUSES = {'bundled'}
QUEUE_ELIGIBLE_STATUSES = {'handed_off', 'pi_dev_requested'}
QUEUE_BUNDLED_STATUS = 'pi_dev_bundled'
BUNDLE_STATUS = 'bundled'


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


def slugify(value: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', value.strip())
    slug = re.sub(r'-{2,}', '-', slug).strip('-._')
    return slug or 'task'


def task_key(task: dict[str, Any]) -> str:
    for key in ('queue_task_key', 'dedupe_key', 'active_goal', 'report_source', 'diagnosis', 'failure_class', 'remediation_class'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return slugify(value)
    return 'task'


def request_bundle_path(request_path: Path) -> Path:
    return BUNDLE_DIR / request_path.name


def request_bundle_path_value(request: dict[str, Any]) -> str | None:
    for key in ('bundle_path', 'pi_dev_bundle_path'):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def request_bundle_created_at_value(request: dict[str, Any]) -> str | None:
    for key in ('bundled_at', 'pi_dev_bundled_at'):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def request_is_bundled(request: dict[str, Any]) -> bool:
    if request.get('status') in REQUEST_BUNDLED_STATUSES:
        return True
    return request_bundle_path_value(request) is not None or request_bundle_created_at_value(request) is not None


def queue_task_bundle_path_value(task: dict[str, Any]) -> str | None:
    value = task.get('pi_dev_bundle_path')
    if isinstance(value, str) and value.strip():
        return value
    return None


def queue_task_bundled_at_value(task: dict[str, Any]) -> str | None:
    value = task.get('pi_dev_bundled_at')
    if isinstance(value, str) and value.strip():
        return value
    return None


def queue_task_is_bundled(task: dict[str, Any]) -> bool:
    if task.get('status') == QUEUE_BUNDLED_STATUS:
        return True
    return queue_task_bundle_path_value(task) is not None or queue_task_bundled_at_value(task) is not None


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


def explicit_instruction(request: dict[str, Any]) -> str:
    goal = request.get('active_goal') or 'the active goal'
    diagnosis = request.get('diagnosis') or 'the current blocker'
    failure_class = request.get('failure_class') or 'the current failure class'
    remediation_class = request.get('remediation_class') or 'the current remediation class'
    recommendation = request.get('recommended_remediation_action') or 'the smallest safe next step'
    recommendation = recommendation.rstrip('.')
    return (
        f'Execute one bounded remediation slice for {goal}. '
        f'Focus on {diagnosis} caused by {failure_class} under {remediation_class}. '
        f'Apply this action: {recommendation}. '
        'Keep the change bounded to the smallest truthful file-level update, '
        'then verify exactly once, and if blocked, stop and report the blocker without widening scope.'
    )


def build_bundle_payload(
    request: dict[str, Any],
    request_path: Path,
    bundle_created_at: str,
    queue_task: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'created_at': bundle_created_at,
        'created_by': SCRIPT_NAME,
        'status': BUNDLE_STATUS,
        'goal': request.get('active_goal'),
        'diagnosis': request.get('diagnosis'),
        'failure_class': request.get('failure_class'),
        'remediation_class': request.get('remediation_class'),
        'recommended_remediation_action': request.get('recommended_remediation_action'),
        'bounded_instruction_text': explicit_instruction(request),
        'source_pi_dev_request_path': str(request_path),
        'source_pi_dev_request_status_before_bundle': request.get('status'),
        'source_pi_dev_request': {
            'status': request.get('status'),
            'created_at': request.get('created_at'),
            'requested_executor': request.get('requested_executor'),
            'active_goal': request.get('active_goal'),
            'diagnosis': request.get('diagnosis'),
            'failure_class': request.get('failure_class'),
            'remediation_class': request.get('remediation_class'),
            'recommended_remediation_action': request.get('recommended_remediation_action'),
            'pi_dev_request_path': str(request_path),
        },
    }
    if queue_task is not None:
        payload['source_queue_task_key'] = task_key(queue_task)
        payload['source_queue_task_status_before_bundle'] = queue_task.get('status')
        payload['source_queue_task'] = {
            'status': queue_task.get('status'),
            'created_at': queue_task.get('created_at'),
            'active_goal': queue_task.get('active_goal'),
            'diagnosis': queue_task.get('diagnosis'),
            'failure_class': queue_task.get('failure_class'),
            'remediation_class': queue_task.get('remediation_class'),
            'recommended_remediation_action': queue_task.get('recommended_remediation_action'),
            'pi_dev_request_path': queue_task.get('pi_dev_request_path'),
            'execution_request_path': queue_task.get('execution_request_path'),
            'executor_handoff_path': queue_task.get('executor_handoff_path'),
        }
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

        if request_is_bundled(request):
            continue
        if request.get('status') not in REQUEST_ELIGIBLE_STATUSES and request.get('status') != BUNDLE_STATUS:
            continue

        matched_index, matched_task = matching_queue_task(tasks, request_path, request)
        if matched_task is None:
            continue
        if matched_task.get('status') not in QUEUE_ELIGIBLE_STATUSES and matched_task.get('status') != QUEUE_BUNDLED_STATUS:
            continue

        request_bundle_marked = request_is_bundled(request)
        queue_bundle_marked = queue_task_is_bundled(matched_task)
        if request_bundle_marked and queue_bundle_marked:
            continue

        bundle_created_at = request_bundle_created_at_value(request) or queue_task_bundled_at_value(matched_task) or now_utc()
        bundle_path = Path(request_bundle_path_value(request) or queue_task_bundle_path_value(matched_task) or str(request_bundle_path(request_path)))
        bundle_payload = build_bundle_payload(request, request_path, bundle_created_at, matched_task)
        atomic_write_json(bundle_path, bundle_payload)

        updated_request = dict(request)
        updated_request['status'] = BUNDLE_STATUS
        updated_request['bundled_at'] = bundle_created_at
        updated_request['bundle_path'] = str(bundle_path)
        updated_request['bundle_status'] = BUNDLE_STATUS
        updated_request['pi_dev_bundled_at'] = bundle_created_at
        updated_request['pi_dev_bundle_path'] = str(bundle_path)
        updated_request['pi_dev_bundle_status'] = BUNDLE_STATUS
        updated_request['bundle_created_by'] = SCRIPT_NAME
        atomic_write_json(request_path, updated_request)

        updated_task = dict(matched_task)
        updated_task['status'] = QUEUE_BUNDLED_STATUS
        updated_task['pi_dev_bundled_at'] = bundle_created_at
        updated_task['pi_dev_bundle_path'] = str(bundle_path)
        updated_task['pi_dev_bundle_status'] = BUNDLE_STATUS
        updated_task['pi_dev_request_status'] = BUNDLE_STATUS
        updated_task['pi_dev_bundle_created_by'] = SCRIPT_NAME
        tasks[matched_index] = updated_task
        atomic_write_json(QUEUE_PATH, {'tasks': tasks})

        output = {
            'consumed': True,
            'status': QUEUE_BUNDLED_STATUS,
            'request_path': str(request_path),
            'bundle_path': str(bundle_path),
            'created_at': bundle_created_at,
            'queue_task_index': matched_index,
            'queue_task_key': task_key(updated_task),
            'queue_task_status_before_bundle': matched_task.get('status'),
        }
        print(json.dumps(output, ensure_ascii=False))
        return

    print(json.dumps({'consumed': False, 'reason': 'no_eligible_pi_dev_request'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
