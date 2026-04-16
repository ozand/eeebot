#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
ACTIVE = ROOT / 'control' / 'active_projects.json'
QUEUE = ROOT / 'control' / 'execution_queue.json'


def load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def main() -> None:
    active = load(ACTIVE, {'projects': []})
    queue = load(QUEUE, {'tasks': []})
    tasks = queue.get('tasks') if isinstance(queue, dict) else []
    if not isinstance(tasks, list):
        tasks = []
    queue_summary = {
        'total': len(tasks),
        'queued': sum(1 for t in tasks if t.get('status') == 'queued'),
        'in_progress': sum(1 for t in tasks if t.get('status') == 'in_progress'),
        'requested_execution': sum(1 for t in tasks if t.get('status') == 'requested_execution'),
        'handed_off': sum(1 for t in tasks if t.get('status') == 'handed_off'),
    }
    print(json.dumps({
        'active_projects': active.get('projects', []),
        'queue_summary': queue_summary,
        'first_task': tasks[0] if tasks else None,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
