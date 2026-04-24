#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_status_snapshot as snapshot

FEED_PATH = ROOT / 'control' / 'status_feed.jsonl'


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _project_summary(project: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': project.get('id'),
        'status': project.get('status'),
        'current_stage': project.get('current_stage'),
        'goal': project.get('goal'),
    }


def _live_task_summary(live_task: dict[str, Any] | None) -> dict[str, Any] | str:
    if live_task is None:
        return 'no_live_executor'
    return {
        'task_key': live_task.get('task_key'),
        'queue_status': live_task.get('queue_status'),
        'execution_state': live_task.get('execution_state'),
        'active_goal': live_task.get('active_goal'),
        'diagnosis': live_task.get('diagnosis'),
        'stale_execution_detected': bool(live_task.get('stale_execution_detected')),
    }


def build_status_feed_entry(updated_at: str | None = None) -> dict[str, Any]:
    snapshot_at = updated_at or now_utc()
    active_projects = snapshot.load(snapshot.ACTIVE_PROJECTS, {'projects': []})
    queue = snapshot.load(snapshot.QUEUE, {'tasks': []})
    active_execution = snapshot.build_active_execution(queue, snapshot_at)

    project_items = active_projects.get('projects', []) if isinstance(active_projects, dict) else []
    if not isinstance(project_items, list):
        project_items = []

    feed_entry = {
        'timestamp': snapshot_at,
        'active_projects_summary': [
            _project_summary(project)
            for project in project_items
            if isinstance(project, dict)
        ],
        'live_execution_exists': bool(active_execution.get('has_actually_executing_task')),
        'current_live_task_or_state': _live_task_summary(active_execution.get('live_task')),
        'status_snapshot_updated_at': active_execution.get('updated_at'),
        'status_snapshot_summary': active_execution.get('summary'),
    }
    return feed_entry


def append_status_feed(feed_path: Path | None = None, updated_at: str | None = None) -> dict[str, Any]:
    feed_entry = build_status_feed_entry(updated_at=updated_at)
    resolved_path = feed_path or FEED_PATH
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(feed_entry, ensure_ascii=False, separators=(',', ':')) + '\n'
    with resolved_path.open('a', encoding='utf-8') as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return {
        'status_feed_path': str(resolved_path),
        'status_feed_entry': feed_entry,
    }


def main() -> None:
    result = append_status_feed()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
