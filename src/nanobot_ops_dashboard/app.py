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

        analytics = {
            'total_snapshots': len(repo_rows) + len(eeepc_rows),
            'source_breakdown': {
                'repo': len(repo_rows),
                'eeepc': len(eeepc_rows),
            },
            'cycle_status_breakdown': {},
        }
        for row in cycles:
            status_value = row.get('status') or 'unknown'
            analytics['cycle_status_breakdown'][status_value] = analytics['cycle_status_breakdown'].get(status_value, 0) + 1

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
