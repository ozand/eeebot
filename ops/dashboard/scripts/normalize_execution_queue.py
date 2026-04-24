#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'
SCRIPT_NAME = 'normalize_execution_queue.py'

ACTIVE_STATUSES = {'queued', 'in_progress', 'requested_execution', 'handed_off'}


def now_utc() -> str:
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


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def task_freshness(task: dict[str, Any]) -> tuple[int, str]:
    timestamps = []
    for key in ('created_at', 'dispatched_at', 'execution_requested_at', 'executor_handoff_at', 'delegated_executor_started_at'):
        parsed = parse_timestamp(task.get(key))
        if parsed is not None:
            timestamps.append(parsed)
    if timestamps:
        return (int(max(timestamps).timestamp() * 1_000_000), task.get('status') or '')
    return (0, task.get('status') or '')


def normalize_tasks(tasks: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    passthrough: list[tuple[int, dict[str, Any]]]= []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        dedupe_key = task.get('dedupe_key')
        if isinstance(dedupe_key, str) and dedupe_key.strip():
            grouped.setdefault(dedupe_key, []).append((index, task))
        else:
            passthrough.append((index, task))

    canonical: list[tuple[int, dict[str, Any]]] = []
    removed: list[dict[str, Any]] = []

    for dedupe_key, entries in grouped.items():
        kept_index, kept_task = max(entries, key=lambda item: (task_freshness(item[1]), item[0]))
        canonical.append((kept_index, kept_task))
        for index, task in entries:
            if index == kept_index:
                continue
            removed.append(
                {
                    'dedupe_key': dedupe_key,
                    'index': index,
                    'status': task.get('status'),
                    'created_at': task.get('created_at'),
                    'dispatched_at': task.get('dispatched_at'),
                    'execution_requested_at': task.get('execution_requested_at'),
                    'executor_handoff_at': task.get('executor_handoff_at'),
                }
            )

    ordered = sorted(canonical + passthrough, key=lambda item: (task_freshness(item[1]), item[0]), reverse=True)
    return [task for _, task in ordered], removed


def main() -> None:
    queue = load_json(QUEUE_PATH, {'tasks': []})
    tasks = queue.get('tasks') if isinstance(queue, dict) else []
    if not isinstance(tasks, list):
        tasks = []

    normalized_tasks, removed = normalize_tasks(tasks)
    normalized_queue = {'tasks': normalized_tasks, 'normalized_at': now_utc(), 'normalized_by': SCRIPT_NAME}
    if removed:
        normalized_queue['superseded_tasks'] = removed
    atomic_write_json(QUEUE_PATH, normalized_queue)

    print(
        json.dumps(
            {
                'normalized': True,
                'queue_path': str(QUEUE_PATH),
                'task_count_before': len(tasks),
                'task_count_after': len(normalized_tasks),
                'superseded_count': len(removed),
                'normalized_at': normalized_queue['normalized_at'],
            },
            ensure_ascii=False,
        )
    )


if __name__ == '__main__':
    main()
