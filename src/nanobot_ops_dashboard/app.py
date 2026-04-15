from __future__ import annotations

import json
from pathlib import Path
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
        cycles = fetch_events(cfg.db_path, 'eeepc', 'cycle', limit=100) + fetch_events(cfg.db_path, 'repo', 'cycle', limit=100)
        promotions = fetch_events(cfg.db_path, 'repo', 'promotion', limit=100)

        context = {
            'repo_latest': repo_rows[0] if repo_rows else None,
            'eeepc_latest': eeepc_rows[0] if eeepc_rows else None,
            'repo_rows': repo_rows,
            'eeepc_rows': eeepc_rows,
            'cycles': cycles,
            'promotions': promotions,
            'subagents_available': False,
        }

        if path == '/cycles':
            template = env.get_template('cycles.html')
        elif path == '/promotions':
            template = env.get_template('promotions.html')
        elif path == '/approvals':
            template = env.get_template('approvals.html')
        elif path == '/deployments':
            template = env.get_template('deployments.html')
        elif path == '/subagents':
            template = env.get_template('subagents.html')
        else:
            template = env.get_template('index.html')

        body = template.render(**context).encode('utf-8')
        start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
        return [body]

    return app
