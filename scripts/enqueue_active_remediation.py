#!/usr/bin/env python3
from __future__ import annotations
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard')
ANALYZER = ROOT / 'scripts' / 'analyze_active_remediation.py'
QUEUE_PATH = ROOT / 'control' / 'execution_queue.json'


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def run_analyzer() -> dict:
    proc = subprocess.run(['python3', str(ANALYZER)], capture_output=True, text=True, check=True, timeout=60)
    return json.loads(proc.stdout)


def main() -> None:
    analysis = run_analyzer()
    diagnosis = analysis.get('diagnosis')
    queue = load_json(QUEUE_PATH, {'tasks': []})
    tasks = queue.get('tasks') if isinstance(queue, dict) else []
    if not isinstance(tasks, list):
        tasks = []

    active_goal = analysis.get('active_goal')
    if isinstance(active_goal, dict):
        active_goal_value = active_goal.get('goal_id') or active_goal.get('text')
    else:
        active_goal_value = active_goal

    remediation = {
        'created_at': now_utc(),
        'status': 'queued',
        'source': 'hermes-autonomy-controller',
        'diagnosis': diagnosis,
        'severity': analysis.get('severity'),
        'active_goal': active_goal_value,
        'report_source': analysis.get('report_source'),
        'failure_class': analysis.get('failure_class'),
        'blocked_next_step': analysis.get('blocked_next_step'),
        'remediation_class': analysis.get('remediation_class'),
        'recommended_remediation_action': analysis.get('recommended_remediation_action'),
        'operator_summary': analysis.get('operator_summary'),
    }

    dedupe_key = '|'.join([
        remediation.get('diagnosis') or '',
        remediation.get('active_goal') or '',
        remediation.get('report_source') or '',
        remediation.get('failure_class') or '',
        remediation.get('remediation_class') or '',
    ])
    remediation['dedupe_key'] = dedupe_key

    if diagnosis != 'stagnating_on_quality_blocker':
        output = {'enqueued': False, 'reason': 'diagnosis_not_actionable', 'analysis': analysis, 'queue_size': len(tasks)}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    existing = next((t for t in tasks if t.get('dedupe_key') == dedupe_key and t.get('status') in {'queued', 'in_progress'}), None)
    if existing:
        output = {'enqueued': False, 'reason': 'duplicate_open_task', 'existing_task': existing, 'queue_size': len(tasks)}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    tasks.insert(0, remediation)
    queue = {'tasks': tasks}
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding='utf-8')
    output = {'enqueued': True, 'task': remediation, 'queue_size': len(tasks)}
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
