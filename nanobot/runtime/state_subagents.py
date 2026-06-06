from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any

def _safe_read_json(path: Path | None) -> Any:
    if not path:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def _subagent_rollup_snapshot(
    *,
    state_root: Path,
    current_task_id: str | None = None,
    current_task_title: str | None = None,
    stale_after_seconds: int = 3600,
) -> dict[str, Any] | None:
    subagents_dir = state_root / 'subagents'
    request_dir = subagents_dir / 'requests'
    result_dir = subagents_dir / 'results'

    completed_statuses = {'ok', 'error', 'cancelled', 'canceled', 'completed', 'complete', 'done', 'pass'}
    queued_statuses = {'queued', 'pending'}

    telemetry_records: list[dict[str, Any]] = []
    terminal_telemetry_results: dict[str, dict[str, Any]] = {}
    if subagents_dir.exists():
        telemetry_paths = sorted(
            [path for path in subagents_dir.glob('*.json') if path.is_file()],
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        for path in telemetry_paths:
            payload = _safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            task_id = payload.get('subagent_id') or payload.get('task_id') or payload.get('id')
            request_id = payload.get('request_id') or payload.get('id')
            semantic_task_id = payload.get('semantic_task_id') or task_id
            verification_task_id = payload.get('verification_task_id') or request_id
            status = str(payload.get('status') or 'unknown')
            telemetry_record = {
                'path': str(path),
                'task_id': task_id,
                'semantic_task_id': semantic_task_id,
                'request_id': request_id,
                'verification_task_id': verification_task_id,
                'verification_role': payload.get('verification_role'),
                'status': status,
                'summary': payload.get('summary') or payload.get('result'),
                'started_at': payload.get('started_at'),
                'finished_at': payload.get('finished_at'),
                'origin': payload.get('origin'),
                'runtime_state_source': payload.get('runtime_state_source'),
            }
            telemetry_records.append(telemetry_record)
            if task_id and status.lower() in completed_statuses:
                terminal_result = {
                    'path': str(path),
                    'task_id': task_id,
                    'semantic_task_id': semantic_task_id,
                    'request_id': request_id,
                    'verification_task_id': verification_task_id,
                    'verification_role': payload.get('verification_role'),
                    'task_title': payload.get('title') or payload.get('summary') or task_id,
                    'cycle_id': payload.get('cycle_id') or payload.get('cycleId'),
                    'status': status,
                    'summary': payload.get('summary') or payload.get('result'),
                    'age_seconds': max(0, int(time.time() - path.stat().st_mtime)),
                    'materialized_from': 'telemetry',
                }
                if request_id:
                    terminal_telemetry_results.setdefault(str(request_id), terminal_result)
                terminal_telemetry_results.setdefault(str(task_id), terminal_result)

    request_records: list[dict[str, Any]] = []
    if request_dir.exists():
        request_paths = sorted(
            [path for path in request_dir.glob('*.json') if path.is_file()],
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        for path in request_paths:
            payload = _safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            task_id = payload.get('task_id') or payload.get('taskId')
            request_id = payload.get('request_id') or payload.get('id')
            semantic_task_id = payload.get('semantic_task_id') or task_id
            verification_task_id = payload.get('verification_task_id') or request_id
            original_status = str(payload.get('request_status') or payload.get('status') or 'queued')
            materialized_result = terminal_telemetry_results.get(str(request_id)) if request_id else None
            if materialized_result is None and not request_id:
                materialized_result = terminal_telemetry_results.get(str(task_id)) if task_id else None
            effective_status = 'completed' if materialized_result else original_status
            age_seconds = max(0, int(time.time() - path.stat().st_mtime))
            request_records.append({
                'path': str(path),
                'task_id': task_id,
                'semantic_task_id': semantic_task_id,
                'request_id': request_id,
                'verification_task_id': verification_task_id,
                'verification_role': payload.get('verification_role'),
                'task_title': payload.get('task_title') or payload.get('title') or payload.get('summary'),
                'cycle_id': payload.get('cycle_id') or payload.get('cycleId'),
                'status': effective_status,
                'request_status': original_status,
                'age_seconds': age_seconds,
                'source_artifact': payload.get('source_artifact'),
                'feedback_decision': payload.get('feedback_decision'),
                'materialized_result_path': materialized_result.get('path') if isinstance(materialized_result, dict) else None,
                'materialized_result_status': materialized_result.get('status') if isinstance(materialized_result, dict) else None,
            })

    result_records: list[dict[str, Any]] = []
    results_by_request_path: dict[str, dict[str, Any]] = {}
    results_by_request_id: dict[str, dict[str, Any]] = {}
    results_by_cycle_id: dict[str, dict[str, Any]] = {}
    results_by_task_id: dict[str, dict[str, Any]] = {}
    if result_dir.exists():
        result_paths = sorted(
            [path for path in result_dir.glob('*.json') if path.is_file()],
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        for path in result_paths:
            payload = _safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            status = str(payload.get('status') or payload.get('result_status') or 'completed')
            result = {
                'path': str(path),
                'request_path': payload.get('request_path'),
                'request_id': payload.get('request_id') or payload.get('id'),
                'semantic_task_id': payload.get('semantic_task_id') or payload.get('task_id') or payload.get('taskId') or payload.get('subagent_id'),
                'verification_task_id': payload.get('verification_task_id') or payload.get('request_id') or payload.get('id'),
                'verification_role': payload.get('verification_role'),
                'task_id': payload.get('task_id') or payload.get('taskId') or payload.get('subagent_id'),
                'task_title': payload.get('task_title') or payload.get('title') or payload.get('summary'),
                'cycle_id': payload.get('cycle_id') or payload.get('cycleId'),
                'status': status,
                'summary': payload.get('summary') or payload.get('result'),
                'key_learnings': payload.get('key_learnings') if isinstance(payload.get('key_learnings'), list) else [],
                'learning_classification': payload.get('learning_classification'),
                'age_seconds': max(0, int(time.time() - path.stat().st_mtime)),
            }
            result_records.append(result)
            if result.get('request_path'):
                results_by_request_path.setdefault(str(result['request_path']), result)
            if result.get('request_id'):
                results_by_request_id.setdefault(str(result['request_id']), result)
            if result.get('cycle_id'):
                results_by_cycle_id.setdefault(str(result['cycle_id']), result)
            if result.get('task_id'):
                results_by_task_id.setdefault(str(result['task_id']), result)
    for result_key, result in terminal_telemetry_results.items():
        if not any(record.get('path') == result.get('path') for record in result_records):
            result_records.append(result)
        results_by_task_id.setdefault(str(result_key), result)
    for request in request_records:
        task_id = request.get('task_id')
        request_id = request.get('request_id')
        cycle_id = request.get('cycle_id')
        materialized_result = (
            (results_by_request_id.get(str(request_id)) if request_id else None)
            or results_by_request_path.get(str(request.get('path')))
            or (results_by_cycle_id.get(str(cycle_id)) if cycle_id else None)
            or (results_by_task_id.get(str(task_id)) if task_id and not request_id else None)
        )
        if isinstance(materialized_result, dict):
            request['materialized_result_path'] = materialized_result.get('path')
            request['materialized_result_status'] = materialized_result.get('status')
            request['status'] = str(materialized_result.get('status') or 'completed').lower()
    result_records = sorted(result_records, key=lambda record: record.get('age_seconds') or 0)

    if not telemetry_records and not request_records and not result_records:
        return None

    completed_task_ids = {str(record['task_id']) for record in result_records if record.get('task_id')}
    blocked_result_count = sum(1 for record in result_records if str(record.get('status') or '').lower() in {'blocked', 'terminal_blocked'})

    queued_count = sum(1 for record in request_records if record['status'] in queued_statuses)
    queued_count += sum(
        1
        for record in telemetry_records
        if record['status'] in {'running', 'queued', 'pending', 'in_progress', 'dispatching'}
        and str(record.get('task_id')) not in completed_task_ids
    )
    completed_count = len(result_records)
    nonblocked_result_count = max(0, completed_count - blocked_result_count)
    stale_count = sum(
        1
        for record in request_records
        if record['request_status'] in queued_statuses
        and not record.get('materialized_result_path')
        and record['age_seconds'] >= stale_after_seconds
    )

    blocked_results_dominant = bool(blocked_result_count and blocked_result_count > nonblocked_result_count)
    if blocked_results_dominant:
        rollup_state = 'blocked' if nonblocked_result_count == 0 else 'degraded'
        rollup_reason = 'blocked_results_dominant'
    elif completed_count and (queued_count or stale_count):
        rollup_state = 'mixed'
        rollup_reason = 'mixed_requests_and_results'
    elif stale_count:
        rollup_state = 'stale'
        rollup_reason = 'stale_requests_present'
    elif queued_count:
        rollup_state = 'queued'
        rollup_reason = 'queued_requests_present'
    elif completed_count:
        rollup_state = 'completed'
        rollup_reason = 'completed_results_only'
    else:
        rollup_state = 'missing'
        rollup_reason = 'no_subagent_activity'

    def _match_record(records: list[dict[str, Any]], task_id: str | None) -> dict[str, Any] | None:
        if not task_id:
            return None
        for record in records:
            if record.get('task_id') == task_id:
                return record
        return None

    preferred_task_id = current_task_id
    request_match = _match_record(request_records, preferred_task_id) if preferred_task_id else None
    telemetry_match = _match_record(telemetry_records, preferred_task_id) if preferred_task_id else None
    result_match = None
    request_match_id = (request_match or {}).get('request_id')
    if request_match_id:
        for record in result_records:
            if record.get('request_id') == request_match_id:
                result_match = record
                break
    elif preferred_task_id:
        result_match = _match_record(result_records, preferred_task_id)

    linkage_source = 'task_plan' if preferred_task_id else None
    if preferred_task_id is None:
        for source_name, record in (
            ('request', request_records[0] if request_records else None),
            ('telemetry', telemetry_records[0] if telemetry_records else None),
            ('result', result_records[0] if result_records else None),
        ):
            if record is not None:
                preferred_task_id = record.get('task_id') or preferred_task_id
                linkage_source = source_name
                if source_name == 'request':
                    request_match = record
                elif source_name == 'telemetry':
                    telemetry_match = record
                else:
                    result_match = record
                break

    active_task_linkage = {
        'task_id': preferred_task_id,
        'semantic_task_id': (request_match or {}).get('semantic_task_id') or (result_match or {}).get('semantic_task_id') or (telemetry_match or {}).get('semantic_task_id') or preferred_task_id,
        'request_id': (request_match or {}).get('request_id') or (result_match or {}).get('request_id') or (telemetry_match or {}).get('request_id'),
        'verification_task_id': (request_match or {}).get('verification_task_id') or (result_match or {}).get('verification_task_id') or (telemetry_match or {}).get('verification_task_id'),
        'verification_role': (request_match or {}).get('verification_role') or (result_match or {}).get('verification_role') or (telemetry_match or {}).get('verification_role'),
        'title': current_task_title
        or (request_match or {}).get('task_title')
        or (telemetry_match or {}).get('summary')
        or (result_match or {}).get('task_title')
        or preferred_task_id,
        'request_path': (request_match or {}).get('path'),
        'result_path': (result_match or {}).get('path'),
        'telemetry_path': (telemetry_match or {}).get('path'),
        'request_status': (request_match or {}).get('status'),
        'result_status': (result_match or {}).get('status'),
        'telemetry_status': (telemetry_match or {}).get('status'),
        'source': linkage_source,
    }

    return {
        'schema_version': 'subagent-rollup-v1',
        'enabled': True,
        'state': rollup_state,
        'reason': rollup_reason,
        'count_total': queued_count + completed_count + stale_count,
        'count_done': completed_count,
        'count_queued': queued_count,
        'count_completed': completed_count,
        'count_stale': stale_count,
        'queued_request_count': queued_count,
        'completed_result_count': completed_count,
        'blocked_result_count': blocked_result_count,
        'stale_request_count': stale_count,
        'telemetry_count': len(telemetry_records),
        'request_count': len(request_records),
        'result_count': len(result_records),
        'active_task_id': preferred_task_id,
        'active_task_title': active_task_linkage.get('title'),
        'active_task_linkage': active_task_linkage,
        'latest_request': request_records[0] if request_records else None,
        'latest_result': result_records[0] if result_records else None,
        'latest_telemetry': telemetry_records[0] if telemetry_records else None,
    }

