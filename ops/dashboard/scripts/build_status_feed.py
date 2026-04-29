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


def _project_summary(project: dict[str, Any], *, fallback_status: str | None = None) -> dict[str, Any]:
    status = project.get('status')
    if status == 'in_progress' and fallback_status in {'waiting_for_dispatch', 'stale_blocked'}:
        status = fallback_status
    return {
        'id': project.get('id'),
        'status': status,
        'current_stage': project.get('current_stage'),
        'goal': project.get('goal'),
    }


def _incident_summary(active_execution: dict[str, Any]) -> dict[str, Any]:
    incident = active_execution.get('stale_execution_incident_task') if isinstance(active_execution.get('stale_execution_incident_task'), dict) else {}
    summary = active_execution.get('summary') if isinstance(active_execution.get('summary'), dict) else {}
    return {
        'source': incident.get('source'),
        'report_source': incident.get('report_source'),
        'incident_path': incident.get('stale_execution_incident_path'),
        'next_action_path': incident.get('stale_execution_next_action_path'),
        'next_action': incident.get('stale_execution_next_action_summary') or incident.get('stale_execution_recommended_next_action') or 'redispatch_stale_execution',
        'stale_execution_incidents': summary.get('stale_execution_incidents', 0),
    }


def _live_task_summary(active_execution: dict[str, Any]) -> dict[str, Any] | str:
    live_task = active_execution.get('live_task') if isinstance(active_execution, dict) else None
    if live_task is None:
        summary = active_execution.get('summary') if isinstance(active_execution, dict) and isinstance(active_execution.get('summary'), dict) else {}
        stale_execution_detected = bool(active_execution.get('stale_execution_detected')) if isinstance(active_execution, dict) else False
        needs_redispatch = int(summary.get('needs_redispatch') or 0) > 0
        if stale_execution_detected or needs_redispatch:
            return {
                'execution_state': 'needs_redispatch' if needs_redispatch else 'no_live_executor',
                'status': 'stale_blocked' if stale_execution_detected else 'no_live_executor',
                'queue_status': 'stale_blocked' if stale_execution_detected else 'no_live_executor',
                'stale_execution_detected': stale_execution_detected,
                'needs_redispatch': summary.get('needs_redispatch', 0),
                **_incident_summary(active_execution),
            }
        return {
            'execution_state': 'waiting_for_dispatch',
            'status': 'waiting_for_dispatch',
            'queue_status': 'no_live_executor',
            'stale_execution_detected': False,
            'needs_redispatch': summary.get('needs_redispatch', 0),
            'next_action': 'dispatch_or_enqueue_bounded_task',
        }
    return {
        'task_key': live_task.get('task_key'),
        'queue_status': live_task.get('queue_status'),
        'execution_state': live_task.get('execution_state'),
        'active_goal': live_task.get('active_goal'),
        'diagnosis': live_task.get('diagnosis'),
        'stale_execution_detected': bool(live_task.get('stale_execution_detected')),
        'stale_execution_age_seconds': live_task.get('stale_execution_age_seconds'),
        'stale_execution_age': live_task.get('stale_execution_age'),
        'stale_execution_recommended_next_action': live_task.get('stale_execution_recommended_next_action'),
        'stale_execution_threshold_minutes': live_task.get('stale_execution_threshold_minutes'),
    }


def build_status_feed_entry(updated_at: str | None = None) -> dict[str, Any]:
    snapshot_at = updated_at or now_utc()
    active_projects = snapshot.load(snapshot.ACTIVE_PROJECTS, {'projects': []})
    queue = snapshot.load(snapshot.QUEUE, {'tasks': []})
    active_execution = snapshot.build_active_execution(queue, snapshot_at)

    project_items = active_projects.get('projects', []) if isinstance(active_projects, dict) else []
    if not isinstance(project_items, list):
        project_items = []

    fallback_project_status = None
    if not active_execution.get('has_actually_executing_task'):
        fallback_project_status = 'stale_blocked' if active_execution.get('stale_execution_detected') else 'waiting_for_dispatch'

    feed_entry = {
        'timestamp': snapshot_at,
        'active_projects_summary': [
            _project_summary(project, fallback_status=fallback_project_status)
            for project in project_items
            if isinstance(project, dict)
        ],
        'live_execution_exists': bool(active_execution.get('has_actually_executing_task')),
        'current_live_task_or_state': _live_task_summary(active_execution),
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
