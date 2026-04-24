#!/usr/bin/env python3
from __future__ import annotations
import json
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard/data/dashboard.sqlite3')


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    eeepc = list(conn.execute(
        "select collected_at,status,active_goal,gate_state,report_source,raw_json from collections where source='eeepc' order by collected_at desc limit 24"
    ))
    cycles = list(conn.execute(
        "select collected_at,title,status,detail_json from events where source='eeepc' and event_type='cycle' order by collected_at desc limit 24"
    ))
    result = {
        'db_path': str(DB),
        'collections_considered': len(eeepc),
        'cycles_considered': len(cycles),
        'latest': None,
        'status_counts': {},
        'report_source_counts': {},
        'failure_class_counts': {},
        'stagnation_flags': {},
    }
    if eeepc:
        latest = dict(eeepc[0])
        raw = json.loads(latest.get('raw_json') or '{}') if latest.get('raw_json') else {}
        outbox = raw.get('outbox') if isinstance(raw.get('outbox'), dict) else {}
        process_reflection = outbox.get('process_reflection') if isinstance(outbox.get('process_reflection'), dict) else {}
        follow = (outbox.get('goal') or {}).get('follow_through') if isinstance(outbox.get('goal'), dict) else {}
        result['latest'] = {
            'collected_at': latest.get('collected_at'),
            'status': latest.get('status'),
            'active_goal': latest.get('active_goal'),
            'gate_state': latest.get('gate_state'),
            'report_source': latest.get('report_source'),
            'failure_class': process_reflection.get('failure_class'),
            'improvement_score': process_reflection.get('improvement_score'),
            'blocked_next_step': follow.get('blocked_next_step'),
        }
    status_counts = Counter(row['status'] or 'unknown' for row in eeepc)
    report_counts = Counter(row['report_source'] or 'unknown' for row in eeepc)
    failure_counts = Counter()
    for row in cycles:
        try:
            detail = json.loads(row['detail_json']) if row['detail_json'] else {}
        except Exception:
            detail = {}
        failure = detail.get('failure_class')
        if failure:
            failure_counts[failure] += 1
    result['status_counts'] = dict(status_counts)
    result['report_source_counts'] = dict(report_counts)
    result['failure_class_counts'] = dict(failure_counts)

    last6 = eeepc[:6]
    result['stagnation_flags'] = {
        'all_last6_block': bool(last6) and all((row['status'] == 'BLOCK') for row in last6),
        'same_report_last6': bool(last6) and len({row['report_source'] for row in last6}) == 1,
        'same_goal_last6': bool(last6) and len({row['active_goal'] for row in last6}) == 1,
        'repeated_failure_class': bool(failure_counts),
        'latest_gate_valid': bool(result['latest'] and result['latest'].get('gate_state') == 'valid'),
    }
    result['diagnosis'] = (
        'stagnating_on_quality_blocker' if result['stagnation_flags']['all_last6_block'] and result['stagnation_flags']['same_report_last6']
        else 'no_stagnation_detected'
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
