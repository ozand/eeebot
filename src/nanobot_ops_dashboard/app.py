from __future__ import annotations

import json
from datetime import datetime, timezone
from wsgiref.util import setup_testing_defaults
from urllib.parse import parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .collector import collect_once
from .config import DashboardConfig
from .storage import count_collections, count_events, fetch_events, fetch_latest_collections


def _env(cfg: DashboardConfig) -> Environment:
    templates = cfg.project_root / 'src' / 'nanobot_ops_dashboard' / 'templates'
    return Environment(
        loader=FileSystemLoader(str(templates)),
        autoescape=select_autoescape(['html', 'xml']),
    )


def _json_loads_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else []
    except Exception:
        return []



def _json_loads_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}



def _decorate_rows(rows):
    decorated = []
    for row in rows:
        item = dict(row)
        item['detail'] = _json_loads_dict(item.get('detail_json'))
        decorated.append(item)
    return decorated



def _row_timestamp(row) -> str:
    detail = row.get('detail') or {}
    return row.get('collected_at') or detail.get('finished_at') or detail.get('started_at') or ''



def _sort_rows_desc(rows):
    return sorted(rows, key=lambda row: _row_timestamp(row), reverse=True)



def _status_kind(status: str | None) -> str:
    normalized = (status or 'unknown').strip().upper()
    if normalized in {'PASS', 'ACCEPT', 'APPROVED', 'OK', 'SUCCESS'}:
        return 'pass'
    if normalized in {'BLOCK', 'FAIL', 'ERROR', 'REJECT', 'DECLINE', 'DENY'}:
        return 'block'
    if normalized in {'UNKNOWN', 'PENDING', 'REVIEW', 'NONE', 'IN_PROGRESS'}:
        return 'unknown'
    return 'unknown' if not status else 'neutral'



def _status_label(status: str | None) -> str:
    return (status or 'unknown').strip() or 'unknown'



def _origin_label(detail: dict | None) -> str:
    if not isinstance(detail, dict):
        return 'unknown'
    origin = detail.get('origin')
    if not isinstance(origin, dict):
        return 'unknown'
    channel = origin.get('channel')
    chat_id = origin.get('chat_id')
    if channel and chat_id:
        return f'{channel}:{chat_id}'
    return channel or chat_id or 'unknown'



def _filter_rows(rows, source: str | None, status: str | None, origin: str | None = None):
    result = rows
    if source:
        result = [row for row in result if row.get('source') == source]
    if status:
        result = [row for row in result if row.get('status') == status]
    if origin:
        result = [row for row in result if _origin_label(row.get('detail')) == origin]
    return result



def _compute_status_streak(rows, status_name: str) -> int:
    streak = 0
    for row in rows:
        if (row.get('status') or 'unknown') == status_name:
            streak += 1
        else:
            break
    return streak



def _latest_status_timestamp(rows, status_name: str) -> str | None:
    for row in rows:
        if (row.get('status') or 'unknown') == status_name:
            return row.get('collected_at')
    return None



def _top_goals(rows, limit: int = 5) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        goal = row.get('title') or 'unknown'
        counts[goal] = counts.get(goal, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{'goal': goal, 'count': count} for goal, count in ordered]



def _top_block_reasons(rows, limit: int = 5) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        if (row.get('status') or 'unknown') != 'BLOCK':
            continue
        detail = row.get('detail') or {}
        reason = detail.get('failure_class') or detail.get('blocked_next_step') or 'unknown'
        counts[reason] = counts.get(reason, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{'reason': reason, 'count': count} for reason, count in ordered]



def _artifact_history(rows, limit: int = 10) -> list[dict]:
    items = []
    for row in rows:
        detail = row.get('detail') or {}
        for artifact in detail.get('artifact_paths') or []:
            items.append({
                'collected_at': row.get('collected_at'),
                'source': row.get('source'),
                'title': row.get('title'),
                'artifact': artifact,
                'status': row.get('status'),
            })
    return items[:limit]


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except Exception:
        return None


def _age_text(value: str | None, now: datetime | None = None) -> str:
    ts = _parse_timestamp(value)
    if ts is None:
        return 'unknown'
    now = now or datetime.now(timezone.utc)
    delta = now - ts
    if delta.total_seconds() < 0:
        return '0s ago'
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f'{seconds}s ago'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes}m ago'
    hours = minutes // 60
    if hours < 24:
        return f'{hours}h ago'
    days = hours // 24
    return f'{days}d ago'


def _sum_observations(groups: list[dict]) -> int:
    return sum(int(group.get('observed_count') or 0) for group in groups)


def _repeat_observations(groups: list[dict]) -> int:
    return max(_sum_observations(groups) - len(groups), 0)


def _latest_cycle_timestamp(rows) -> str | None:
    if not rows:
        return None
    return rows[0].get('collected_at')


def _eeepc_observation_groups(rows, limit: int = 10) -> list[dict]:
    groups: list[dict] = []
    for row in rows:
        item = dict(row)
        report_source = item.get('report_source') or 'unknown'
        collected_at = item.get('collected_at') or ''
        if groups and groups[-1]['report_source'] == report_source:
            group = groups[-1]
            group['observed_count'] += 1
            group['earliest_observed_at'] = collected_at or group['earliest_observed_at']
        else:
            groups.append({
                'report_source': report_source,
                'latest_observed_at': collected_at,
                'earliest_observed_at': collected_at,
                'observed_count': 1,
                'status': item.get('status') or 'unknown',
                'active_goal': item.get('active_goal'),
            })
    for group in groups:
        latest = _parse_timestamp(group.get('latest_observed_at'))
        earliest = _parse_timestamp(group.get('earliest_observed_at'))
        if latest and earliest and group.get('observed_count', 0) > 1:
            span_minutes = (latest - earliest).total_seconds() / 60
            group['observed_span_minutes'] = round(span_minutes, 1)
            group['approx_cadence_minutes'] = round(span_minutes / (group['observed_count'] - 1), 1)
        else:
            group['observed_span_minutes'] = 0.0 if group.get('observed_count') == 1 else None
            group['approx_cadence_minutes'] = None
    return groups[:limit]



def create_app(cfg: DashboardConfig):
    env = _env(cfg)
    env.globals['status_kind'] = _status_kind
    env.globals['status_label'] = _status_label

    def app(environ, start_response):
        setup_testing_defaults(environ)
        path = environ.get('PATH_INFO', '/')
        query = parse_qs(environ.get('QUERY_STRING', ''))

        if path == '/collect':
            result = collect_once(cfg)
            body = json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        repo_rows = fetch_latest_collections(cfg.db_path, 'repo', limit=50)
        eeepc_rows = fetch_latest_collections(cfg.db_path, 'eeepc', limit=50)
        eeepc_observation_groups = _eeepc_observation_groups(eeepc_rows)
        eeepc_latest_observation = eeepc_observation_groups[0] if eeepc_observation_groups else None
        cycle_source = query.get('source', [None])[0]
        cycle_status = query.get('status', [None])[0]
        promotion_source = query.get('source', [None])[0]
        promotion_status = query.get('status', [None])[0]
        cycles = _filter_rows(
            _decorate_rows(fetch_events(cfg.db_path, 'eeepc', 'cycle', limit=100) + fetch_events(cfg.db_path, 'repo', 'cycle', limit=100)),
            cycle_source,
            cycle_status,
        )
        eeepc_cycle_events = [row for row in cycles if row.get('source') == 'eeepc']
        promotions = _filter_rows(
            _decorate_rows(fetch_events(cfg.db_path, 'repo', 'promotion', limit=100)),
            promotion_source,
            promotion_status,
        )
        all_subagent_events = _sort_rows_desc(
            _decorate_rows(
                fetch_events(cfg.db_path, 'repo', 'subagent', limit=100) +
                fetch_events(cfg.db_path, 'eeepc', 'subagent', limit=100)
            )
        )

        repo_latest = repo_rows[0] if repo_rows else None
        eeepc_latest = eeepc_rows[0] if eeepc_rows else None
        latest_collected = None
        for row in [eeepc_latest, repo_latest]:
            if row and (latest_collected is None or row['collected_at'] > latest_collected):
                latest_collected = row['collected_at']

        now = datetime.now(timezone.utc)
        loaded_snapshot_count = len(repo_rows) + len(eeepc_rows)
        total_snapshot_count = count_collections(cfg.db_path)
        source_breakdown = {
            'repo': count_collections(cfg.db_path, 'repo'),
            'eeepc': count_collections(cfg.db_path, 'eeepc'),
        }
        loaded_cycle_count = len(cycles)
        total_cycle_count = count_events(cfg.db_path, event_type='cycle')
        eeepc_observation_total = _sum_observations(eeepc_observation_groups)
        eeepc_observation_repeat_count = _repeat_observations(eeepc_observation_groups)
        eeepc_latest_age = _age_text(eeepc_latest['collected_at'] if eeepc_latest else None, now)
        repo_latest_age = _age_text(repo_latest['collected_at'] if repo_latest else None, now)
        latest_collector_success_age = _age_text(latest_collected, now)
        latest_pass_at = _latest_status_timestamp(cycles, 'PASS')
        latest_block_at = _latest_status_timestamp(cycles, 'BLOCK')

        eeepc_raw = _json_loads_dict(eeepc_latest['raw_json']) if eeepc_latest else {}
        eeepc_outbox = eeepc_raw.get('outbox') if isinstance(eeepc_raw.get('outbox'), dict) else {}
        eeepc_reflection = eeepc_outbox.get('process_reflection') if isinstance(eeepc_outbox.get('process_reflection'), dict) else {}
        eeepc_follow = (eeepc_outbox.get('goal') or {}).get('follow_through') if isinstance(eeepc_outbox.get('goal'), dict) else {}
        eeepc_reachability = eeepc_raw.get('reachability') if isinstance(eeepc_raw.get('reachability'), dict) else {}
        eeepc_reachability_at = eeepc_reachability.get('collected_at') if eeepc_reachability else None
        eeepc_reachability_age = _age_text(eeepc_reachability_at, now)
        current_blocker = {
            'kind': 'block' if (eeepc_reachability and not eeepc_reachability.get('reachable')) or eeepc_reflection.get('failure_class') or eeepc_follow.get('blocked_next_step') else 'unknown',
            'source': 'reachability watchdog' if eeepc_reachability and not eeepc_reachability.get('reachable') else 'outbox reflection',
            'failure_class': 'control_plane_unreachable' if eeepc_reachability and not eeepc_reachability.get('reachable') else eeepc_reflection.get('failure_class'),
            'improvement_score': eeepc_reflection.get('improvement_score'),
            'blocked_next_step': eeepc_reachability.get('recommended_next_action') if eeepc_reachability and not eeepc_reachability.get('reachable') else (eeepc_follow or {}).get('blocked_next_step'),
            'error': eeepc_reachability.get('error') if eeepc_reachability and not eeepc_reachability.get('reachable') else None,
            'reachable': eeepc_reachability.get('reachable') if eeepc_reachability else None,
        }

        analytics = {
            'total_snapshots': total_snapshot_count,
            'loaded_snapshot_window': loaded_snapshot_count,
            'source_breakdown': source_breakdown,
            'loaded_source_breakdown': {
                'repo': len(repo_rows),
                'eeepc': len(eeepc_rows),
            },
            'cycle_status_breakdown': {},
            'cycle_failure_breakdown': {},
            'current_pass_streak': _compute_status_streak(cycles, 'PASS'),
            'current_block_streak': _compute_status_streak(cycles, 'BLOCK'),
            'latest_pass_at': latest_pass_at,
            'latest_pass_age': _age_text(latest_pass_at, now),
            'latest_block_at': latest_block_at,
            'latest_block_age': _age_text(latest_block_at, now),
            'top_goals': _top_goals(cycles),
            'top_block_reasons': _top_block_reasons(cycles),
            'artifact_history': _artifact_history(cycles),
            'eeepc_unique_cycle_reports': len(eeepc_cycle_events),
            'eeepc_observation_groups': eeepc_observation_groups,
            'eeepc_observation_total': eeepc_observation_total,
            'eeepc_observation_repeat_count': eeepc_observation_repeat_count,
            'eeepc_observation_group_count': len(eeepc_observation_groups),
            'recent_unique_cycle_reports': [
                {
                    'collected_at': row.get('collected_at'),
                    'source': row.get('source'),
                    'status': row.get('status'),
                    'title': row.get('title'),
                }
                for row in eeepc_cycle_events[:10]
            ],
            'recent_cycle_timeline': [
                {
                    'collected_at': row.get('collected_at'),
                    'source': row.get('source'),
                    'status': row.get('status'),
                    'title': row.get('title'),
                }
                for row in cycles[:10]
            ],
            'recent_goal_transitions': [
                {
                    'collected_at': row.get('collected_at'),
                    'source': row.get('source'),
                    'goal': row.get('title'),
                    'status': row.get('status'),
                }
                for row in cycles[:10]
            ],
            'loaded_cycle_window': loaded_cycle_count,
            'total_cycle_events': total_cycle_count,
        }
        for row in cycles:
            status_value = row.get('status') or 'unknown'
            analytics['cycle_status_breakdown'][status_value] = analytics['cycle_status_breakdown'].get(status_value, 0) + 1
            failure_class = (row.get('detail') or {}).get('failure_class')
            if failure_class:
                analytics['cycle_failure_breakdown'][failure_class] = analytics['cycle_failure_breakdown'].get(failure_class, 0) + 1

        request_source = query.get('source', [''])[0]
        request_status = query.get('status', [''])[0]
        request_origin = query.get('origin', [''])[0]
        subagent_events = _filter_rows(all_subagent_events, request_source, request_status, request_origin)
        subagent_sources = sorted({row.get('source') for row in all_subagent_events if row.get('source')})
        subagent_origins = sorted({_origin_label(row.get('detail')) for row in all_subagent_events if _origin_label(row.get('detail')) != 'unknown'})
        subagent_statuses = sorted({row.get('status') or 'unknown' for row in all_subagent_events})
        subagent_total = len(all_subagent_events)

        context = {
            'repo_latest': repo_latest,
            'eeepc_latest': eeepc_latest,
            'repo_rows': repo_rows,
            'eeepc_rows': eeepc_rows,
            'cycles': cycles,
            'promotions': promotions,
            'subagent_events': subagent_events,
            'subagents_available': bool(all_subagent_events),
            'latest_collected': latest_collected,
            'latest_collected_age': _age_text(latest_collected, now),
            'latest_collector_success_age': latest_collector_success_age,
            'snapshot_count': total_snapshot_count,
            'loaded_snapshot_count': loaded_snapshot_count,
            'snapshot_window_count': loaded_snapshot_count,
            'total_snapshot_count': total_snapshot_count,
            'eeepc_latest_age': eeepc_latest_age,
            'repo_latest_age': repo_latest_age,
            'eeepc_reachability_age': eeepc_reachability_age,
            'eeepc_reachability_collected_at': eeepc_reachability_at,
            'latest_pass_at': latest_pass_at,
            'latest_pass_age': analytics['latest_pass_age'],
            'latest_block_at': latest_block_at,
            'latest_block_age': analytics['latest_block_age'],
            'eeepc_artifacts': _json_loads_list(eeepc_latest['artifact_paths_json']) if eeepc_latest else [],
            'repo_artifacts': _json_loads_list(repo_latest['artifact_paths_json']) if repo_latest else [],
            'analytics': analytics,
            'current_blocker': current_blocker,
            'eeepc_reachability': eeepc_reachability,
            'request_source': request_source,
            'request_status': request_status,
            'request_origin': request_origin,
            'subagent_sources': subagent_sources,
            'subagent_origins': subagent_origins,
            'subagent_statuses': subagent_statuses,
            'subagent_total': subagent_total,
            'eeepc_observation_groups': eeepc_observation_groups,
            'eeepc_latest_observation': eeepc_latest_observation,
            'eeepc_unique_cycle_reports': len(eeepc_cycle_events),
            'recent_snapshots': sorted([dict(r) for r in (repo_rows[:5] + eeepc_rows[:5])], key=lambda x: x['collected_at'], reverse=True)[:10],
            'recent_cycles': cycles[:10],
        }

        if path == '/api/summary':
            payload = {
                'latest_collected': latest_collected,
                'snapshot_count': total_snapshot_count,
                'loaded_snapshot_count': loaded_snapshot_count,
                'snapshot_window_count': loaded_snapshot_count,
                'total_snapshot_count': total_snapshot_count,
                'cycle_count': loaded_cycle_count,
                'loaded_cycle_count': loaded_cycle_count,
                'total_cycle_events': total_cycle_count,
                'latest_collector_success_age': latest_collector_success_age,
                'latest_collected_age': _age_text(latest_collected, now),
                'eeepc_latest_age': eeepc_latest_age,
                'repo_latest_age': repo_latest_age,
                'eeepc_reachability_age': eeepc_reachability_age,
                'eeepc_reachability_collected_at': eeepc_reachability_at,
                'latest_pass_at': latest_pass_at,
                'latest_pass_age': analytics['latest_pass_age'],
                'latest_block_at': latest_block_at,
                'latest_block_age': analytics['latest_block_age'],
                'eeepc_unique_cycle_reports': len(eeepc_cycle_events),
                'eeepc_observation_groups': eeepc_observation_groups,
                'eeepc_observation_total': eeepc_observation_total,
                'eeepc_observation_repeat_count': eeepc_observation_repeat_count,
                'promotion_count': len(promotions),
                'repo_latest': dict(repo_latest) if repo_latest else None,
                'eeepc_latest': dict(eeepc_latest) if eeepc_latest else None,
                'eeepc_latest_observation': eeepc_latest_observation,
                'eeepc_reachability': eeepc_reachability,
                'current_blocker': current_blocker,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/cycles':
            body = json.dumps({'items': cycles}, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/promotions':
            body = json.dumps({'items': promotions}, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/approvals':
            payload = {'items': [dict(r) for r in (eeepc_rows + repo_rows)]}
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/deployments':
            payload = {
                'eeepc_latest': dict(eeepc_latest) if eeepc_latest else None,
                'repo_latest': dict(repo_latest) if repo_latest else None,
                'eeepc_latest_observation': eeepc_latest_observation,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/analytics':
            body = json.dumps({'analytics': analytics, 'current_blocker': current_blocker}, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/cycles':
            template = env.get_template('cycles.html')
        elif path == '/promotions':
            template = env.get_template('promotions.html')
        elif path == '/approvals':
            template = env.get_template('approvals.html')
        elif path == '/deployments':
            template = env.get_template('deployments.html')
        elif path == '/analytics':
            template = env.get_template('analytics.html')
        elif path == '/subagents':
            template = env.get_template('subagents.html')
        else:
            template = env.get_template('index.html')

        body = template.render(**context).encode('utf-8')
        start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
        return [body]

    return app
