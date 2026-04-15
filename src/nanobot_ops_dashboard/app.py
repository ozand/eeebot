from __future__ import annotations

import json
from wsgiref.util import setup_testing_defaults
from urllib.parse import parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .collector import collect_once
from .config import DashboardConfig
from .storage import fetch_events, fetch_latest_collections


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



def _filter_rows(rows, source: str | None, status: str | None):
    result = rows
    if source:
        result = [row for row in result if row.get('source') == source]
    if status:
        result = [row for row in result if row.get('status') == status]
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



def create_app(cfg: DashboardConfig):
    env = _env(cfg)

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
        cycle_source = query.get('source', [None])[0]
        cycle_status = query.get('status', [None])[0]
        promotion_source = query.get('source', [None])[0]
        promotion_status = query.get('status', [None])[0]
        cycles = _filter_rows(
            _decorate_rows(fetch_events(cfg.db_path, 'eeepc', 'cycle', limit=100) + fetch_events(cfg.db_path, 'repo', 'cycle', limit=100)),
            cycle_source,
            cycle_status,
        )
        promotions = _filter_rows(
            _decorate_rows(fetch_events(cfg.db_path, 'repo', 'promotion', limit=100)),
            promotion_source,
            promotion_status,
        )

        repo_latest = repo_rows[0] if repo_rows else None
        eeepc_latest = eeepc_rows[0] if eeepc_rows else None
        latest_collected = None
        for row in [eeepc_latest, repo_latest]:
            if row and (latest_collected is None or row['collected_at'] > latest_collected):
                latest_collected = row['collected_at']

        eeepc_raw = _json_loads_dict(eeepc_latest['raw_json']) if eeepc_latest else {}
        eeepc_outbox = eeepc_raw.get('outbox') if isinstance(eeepc_raw.get('outbox'), dict) else {}
        eeepc_reflection = eeepc_outbox.get('process_reflection') if isinstance(eeepc_outbox.get('process_reflection'), dict) else {}
        eeepc_follow = (eeepc_outbox.get('goal') or {}).get('follow_through') if isinstance(eeepc_outbox.get('goal'), dict) else {}
        current_blocker = {
            'failure_class': eeepc_reflection.get('failure_class'),
            'improvement_score': eeepc_reflection.get('improvement_score'),
            'blocked_next_step': (eeepc_follow or {}).get('blocked_next_step'),
        }

        analytics = {
            'total_snapshots': len(repo_rows) + len(eeepc_rows),
            'source_breakdown': {
                'repo': len(repo_rows),
                'eeepc': len(eeepc_rows),
            },
            'cycle_status_breakdown': {},
            'cycle_failure_breakdown': {},
            'current_pass_streak': _compute_status_streak(cycles, 'PASS'),
            'current_block_streak': _compute_status_streak(cycles, 'BLOCK'),
            'latest_pass_at': _latest_status_timestamp(cycles, 'PASS'),
            'latest_block_at': _latest_status_timestamp(cycles, 'BLOCK'),
            'top_goals': _top_goals(cycles),
            'top_block_reasons': _top_block_reasons(cycles),
            'artifact_history': _artifact_history(cycles),
        }
        for row in cycles:
            status_value = row.get('status') or 'unknown'
            analytics['cycle_status_breakdown'][status_value] = analytics['cycle_status_breakdown'].get(status_value, 0) + 1
            failure_class = (row.get('detail') or {}).get('failure_class')
            if failure_class:
                analytics['cycle_failure_breakdown'][failure_class] = analytics['cycle_failure_breakdown'].get(failure_class, 0) + 1

        context = {
            'repo_latest': repo_latest,
            'eeepc_latest': eeepc_latest,
            'repo_rows': repo_rows,
            'eeepc_rows': eeepc_rows,
            'cycles': cycles,
            'promotions': promotions,
            'subagents_available': False,
            'latest_collected': latest_collected,
            'snapshot_count': len(repo_rows) + len(eeepc_rows),
            'eeepc_artifacts': _json_loads_list(eeepc_latest['artifact_paths_json']) if eeepc_latest else [],
            'repo_artifacts': _json_loads_list(repo_latest['artifact_paths_json']) if repo_latest else [],
            'analytics': analytics,
            'current_blocker': current_blocker,
            'request_source': query.get('source', [''])[0],
            'request_status': query.get('status', [''])[0],
            'recent_snapshots': sorted([dict(r) for r in (repo_rows[:5] + eeepc_rows[:5])], key=lambda x: x['collected_at'], reverse=True)[:10],
            'recent_cycles': cycles[:10],
        }

        if path == '/api/summary':
            payload = {
                'latest_collected': latest_collected,
                'snapshot_count': len(repo_rows) + len(eeepc_rows),
                'cycle_count': len(cycles),
                'promotion_count': len(promotions),
                'repo_latest': dict(repo_latest) if repo_latest else None,
                'eeepc_latest': dict(eeepc_latest) if eeepc_latest else None,
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
