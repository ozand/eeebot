#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
ACTIVE_EXECUTION_PATH = ROOT / 'control' / 'active_execution.json'
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
DEFAULT_THRESHOLD_MINUTES = 30

IN_PROGRESS_STATUSES = {'in_progress'}
TERMINAL_STATUSES = {'completed', 'cancelled'}
WAITING_STATUSES = {
    'requested_execution',
    'dispatched',
    'handed_off',
    'pi_dev_requested',
    'bundled',
    'pi_dev_bundled',
    'pi_dev_dispatch_ready',
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.isoformat().replace('+00:00', 'Z')


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_now(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = parse_timestamp(value)
        if parsed is not None:
            return parsed
    return now_utc()


def format_duration(seconds: float | int) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f'{hours}h')
    if minutes or hours:
        parts.append(f'{minutes}m')
    parts.append(f'{secs}s')
    return ''.join(parts)


def task_key(task: dict[str, Any]) -> str:
    for key in ('task_key', 'dedupe_key', 'active_goal', 'report_source', 'diagnosis'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return 'task'


def executor_name(task: dict[str, Any]) -> str | None:
    for key in ('delegated_executor_request_executor', 'requested_executor', 'executor', 'source'):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def task_state(task: dict[str, Any]) -> str:
    state = task.get('execution_state')
    if isinstance(state, str) and state.strip():
        return state.strip()

    status = task.get('status')
    if status in TERMINAL_STATUSES:
        return 'completed'
    if status in IN_PROGRESS_STATUSES:
        return 'in_progress'
    if status == 'queued':
        return 'queued'
    if status in WAITING_STATUSES:
        return 'waiting_for_dispatch'
    if task.get('blocked_next_step'):
        return 'blocked'
    return 'unknown'


def started_at_for_task(task: dict[str, Any]) -> tuple[str | None, datetime | None]:
    for key in ('delegated_executor_started_at', 'execution_requested_at', 'executor_handoff_at', 'dispatched_at', 'created_at', 'started_at'):
        parsed = parse_timestamp(task.get(key))
        if parsed is not None:
            return key, parsed
    return None, None


def collect_task_candidates(active_execution: dict[str, Any] | None, queue: dict[str, Any] | None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    active_tasks = []
    if isinstance(active_execution, dict):
        maybe_tasks = active_execution.get('active_tasks')
        if isinstance(maybe_tasks, list):
            active_tasks = maybe_tasks

    if active_tasks:
        for index, task in enumerate(active_tasks):
            if isinstance(task, dict):
                candidates.append({'source': 'active_execution', 'task_index': index, 'task': task})
        return candidates

    queue_tasks = []
    if isinstance(queue, dict):
        maybe_tasks = queue.get('tasks')
        if isinstance(maybe_tasks, list):
            queue_tasks = maybe_tasks

    for index, task in enumerate(queue_tasks):
        if isinstance(task, dict):
            candidates.append({'source': 'execution_queue', 'task_index': index, 'task': task})
    return candidates


def build_candidate(task: dict[str, Any], task_index: int, source: str, now: datetime) -> dict[str, Any] | None:
    state = task_state(task)
    if state != 'in_progress':
        return None

    started_at_key, started_at = started_at_for_task(task)
    if started_at is None:
        age_seconds = None
        age = None
    else:
        age_seconds = max(0.0, (now - started_at).total_seconds())
        age = format_duration(age_seconds)

    return {
        'source': source,
        'task_index': task_index,
        'task_key': task_key(task),
        'executor': executor_name(task),
        'started_at_key': started_at_key,
        'started_at': format_utc(started_at) if started_at is not None else None,
        'age_seconds': age_seconds,
        'age': age,
        'task_status': task.get('execution_state') or task.get('status'),
        'recommended_next_action': None,
    }


def detect_stale_execution(
    active_execution: dict[str, Any] | None = None,
    queue: dict[str, Any] | None = None,
    threshold_minutes: int = DEFAULT_THRESHOLD_MINUTES,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    reference_now = normalize_now(now)
    threshold_seconds = max(0, int(threshold_minutes)) * 60

    candidates: list[dict[str, Any]] = []
    for candidate_info in collect_task_candidates(active_execution, queue):
        candidate = build_candidate(candidate_info['task'], candidate_info['task_index'], candidate_info['source'], reference_now)
        if candidate is not None:
            candidates.append(candidate)

    best_candidate: dict[str, Any] | None = None
    if candidates:
        best_candidate = max(
            candidates,
            key=lambda item: (
                item['age_seconds'] if item['age_seconds'] is not None else -1.0,
                -item['task_index'],
                item['task_key'],
            ),
        )

    stale_detected = bool(
        best_candidate is not None
        and best_candidate['age_seconds'] is not None
        and best_candidate['age_seconds'] > threshold_seconds
    )

    policy_summary = f'in_progress tasks older than {threshold_minutes} minutes must be investigated or escalated.'

    if stale_detected:
        recommended_next_action = (
            f'Treat this as a stale-execution incident under the {threshold_minutes}-minute investigation rule: '
            'check the live executor, confirm whether the task is still running, and either record the terminal result or re-dispatch the bounded slice.'
        )
    elif best_candidate is not None:
        recommended_next_action = (
            f'Continue monitoring on the next watchdog interval; the live task is still below the {threshold_minutes}-minute stale-investigation threshold.'
        )
    else:
        recommended_next_action = 'No live in_progress task was found; continue monitoring on the next watchdog interval.'

    if best_candidate is not None:
        best_candidate = dict(best_candidate)
        best_candidate['recommended_next_action'] = recommended_next_action

    return {
        'stale_detected': stale_detected,
        'threshold_minutes': threshold_minutes,
        'threshold_seconds': threshold_seconds,
        'policy_summary': policy_summary,
        'task_key': best_candidate['task_key'] if best_candidate else None,
        'executor': best_candidate['executor'] if best_candidate else None,
        'started_at': best_candidate['started_at'] if best_candidate else None,
        'age_seconds': best_candidate['age_seconds'] if best_candidate else None,
        'age': best_candidate['age'] if best_candidate else None,
        'recommended_next_action': recommended_next_action,
        'inspection_source': best_candidate['source'] if best_candidate else None,
        'task_index': best_candidate['task_index'] if best_candidate else None,
        'task_status': best_candidate['task_status'] if best_candidate else None,
        'observed_in_progress_candidates': len(candidates),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Detect stale in_progress execution tasks using the 30-minute investigation rule.')
    parser.add_argument('--threshold-minutes', type=int, default=DEFAULT_THRESHOLD_MINUTES, help='Stale-investigation threshold in minutes (default: 30).')
    parser.add_argument('--active-execution-path', type=Path, default=ACTIVE_EXECUTION_PATH, help='Path to active_execution.json.')
    parser.add_argument('--queue-path', type=Path, default=QUEUE_PATH, help='Path to execution_queue.json.')
    args = parser.parse_args()

    active_execution = load_json(args.active_execution_path, {})
    queue = load_json(args.queue_path, {})
    result = detect_stale_execution(
        active_execution=active_execution if isinstance(active_execution, dict) else None,
        queue=queue if isinstance(queue, dict) else None,
        threshold_minutes=args.threshold_minutes,
    )
    result['active_execution_path'] = str(args.active_execution_path)
    result['queue_path'] = str(args.queue_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
