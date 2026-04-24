#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

DB = Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard/data/dashboard.sqlite3')


def _safe_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nested_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _goal_payload(raw: dict[str, Any]) -> dict[str, Any]:
    outbox = _nested_dict(raw.get('outbox'))
    goal = _nested_dict(outbox.get('goal'))
    goal_context = _nested_dict(outbox.get('goal_context'))

    return {
        'goal_id': goal.get('goal_id') or goal_context.get('goal_id') or raw.get('active_goal'),
        'text': goal.get('text') or goal_context.get('goal') or raw.get('active_goal'),
        'priority': goal_context.get('priority'),
        'status': goal_context.get('status'),
        'selection': _nested_dict(goal.get('selection')) or None,
    }


def _latest_pass_timestamp(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "select collected_at from collections where source='eeepc' and status='PASS' order by collected_at desc limit 1"
    ).fetchone()
    return row['collected_at'] if row else None


def _extract_failure_class(raw: dict[str, Any]) -> str | None:
    outbox = _nested_dict(raw.get('outbox'))
    goal = _nested_dict(outbox.get('goal'))
    goal_context = _nested_dict(outbox.get('goal_context'))
    last_result = _nested_dict(goal_context.get('last_result'))
    candidates = [
        raw.get('failure_class'),
        _nested_dict(raw.get('process_reflection')).get('failure_class'),
        _nested_dict(outbox.get('process_reflection')).get('failure_class'),
        _nested_dict(goal.get('process_reflection')).get('failure_class'),
        _nested_dict(goal_context.get('process_reflection')).get('failure_class'),
        _nested_dict(last_result.get('process_reflection')).get('failure_class'),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def _extract_blocked_next_step(raw: dict[str, Any]) -> str | None:
    outbox = _nested_dict(raw.get('outbox'))
    goal = _nested_dict(outbox.get('goal'))
    goal_context = _nested_dict(outbox.get('goal_context'))
    last_result = _nested_dict(goal_context.get('last_result'))
    follow_through = _nested_dict(goal.get('follow_through'))
    candidates = [
        raw.get('blocked_next_step'),
        follow_through.get('blocked_next_step'),
        last_result.get('blocked_next_step'),
        _nested_dict(goal_context.get('process_reflection')).get('next'),
        _nested_dict(outbox.get('process_reflection')).get('next'),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def _remediation_for(failure_class: str | None, flags: dict[str, bool], latest: dict[str, Any]) -> tuple[str, str]:
    if failure_class == 'promotion_execute_denied':
        return (
            'approval_refresh',
            'Refresh the approval gate state, then rerun the bounded apply check so the next cycle can proceed with an allowed lane.',
        )
    if failure_class == 'no_concrete_change' or (flags['all_last6_block'] and flags['same_goal_last6']):
        return (
            'planner_hardening',
            'Tighten the next-cycle planner so it must emit exactly one file-level action plus one verification command and an explicit blocked-next-step fallback.',
        )
    if not latest.get('report_source') or not latest.get('active_goal'):
        return (
            'observability_gap',
            'Repair the dashboard capture path so the next snapshot includes the active goal, report source, and blocker metadata.',
        )
    return (
        'stale_goal_reprioritization',
        'Replace the current active goal with a smaller verifiable slice, then bound the follow-up to one concrete change.',
    )


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    eeepc = list(
        conn.execute(
            "select collected_at,status,active_goal,gate_state,report_source,raw_json from collections where source='eeepc' order by collected_at desc limit 24"
        )
    )
    cycles = list(
        conn.execute(
            "select collected_at,title,status,detail_json from events where source='eeepc' and event_type='cycle' order by collected_at desc limit 24"
        )
    )

    latest_row = dict(eeepc[0]) if eeepc else {}
    latest_raw = _safe_json(latest_row.get('raw_json'))
    latest_goal = _goal_payload(latest_raw)
    latest_failure_class = _extract_failure_class(latest_raw)
    latest_blocked_next_step = _extract_blocked_next_step(latest_raw)

    status_counts = Counter(row['status'] or 'unknown' for row in eeepc)
    report_counts = Counter(row['report_source'] or 'unknown' for row in eeepc)
    failure_counts = Counter()
    for row in cycles:
        detail = _safe_json(row['detail_json'])
        failure = _extract_failure_class(detail)
        if failure:
            failure_counts[failure] += 1

    last6 = eeepc[:6]
    flags = {
        'all_last6_block': bool(last6) and all(row['status'] == 'BLOCK' for row in last6),
        'same_report_source_last6': bool(last6) and len({row['report_source'] for row in last6}) == 1,
        'same_goal_last6': bool(last6) and len({row['active_goal'] for row in last6}) == 1,
        'repeated_failure_class': bool(failure_counts),
        'latest_gate_valid': bool(latest_row and latest_row.get('gate_state') == 'valid'),
    }

    pass_timestamp = _latest_pass_timestamp(conn)
    latest_pass_block = 'unknown'
    if pass_timestamp and latest_row.get('collected_at'):
        latest_pass_block = 'stale' if latest_row['collected_at'] >= pass_timestamp else 'fresh'

    diagnosis = 'healthy'
    if flags['all_last6_block'] and flags['same_report_source_last6'] and flags['same_goal_last6']:
        diagnosis = 'stagnating_on_quality_blocker'
    elif flags['all_last6_block'] or flags['same_goal_last6'] or flags['same_report_source_last6']:
        diagnosis = 'partial_stagnation'

    severity = 'low'
    if diagnosis == 'stagnating_on_quality_blocker' and latest_failure_class == 'no_concrete_change' and flags['latest_gate_valid']:
        severity = 'critical'
    elif diagnosis == 'stagnating_on_quality_blocker':
        severity = 'high'
    elif diagnosis == 'partial_stagnation':
        severity = 'medium'

    remediation_class, recommended_action = _remediation_for(latest_failure_class, flags, latest_row)

    if diagnosis == 'healthy':
        operator_summary = 'Dashboard does not show a current stagnation incident; continue scheduled monitoring.'
    else:
        operator_summary = (
            f"{diagnosis} at severity {severity}: the active goal is stuck behind {latest_failure_class or 'an unresolved blocker'} "
            f"with repeated BLOCK results."
        )

    result = {
        'diagnosis': diagnosis,
        'severity': severity,
        'active_goal': latest_goal,
        'report_source': latest_row.get('report_source'),
        'failure_class': latest_failure_class,
        'blocked_next_step': latest_blocked_next_step,
        'recommended_remediation_action': recommended_action,
        'remediation_class': remediation_class,
        'operator_summary': operator_summary,
        'evidence': {
            'db_path': str(DB),
            'collections_considered': len(eeepc),
            'cycles_considered': len(cycles),
            'status_counts': dict(status_counts),
            'report_source_counts': dict(report_counts),
            'failure_class_counts': dict(failure_counts),
            'stagnation_flags': flags,
            'latest_collection_at': latest_row.get('collected_at'),
            'latest_pass_timestamp': pass_timestamp,
            'latest_pass_recency': latest_pass_block,
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
