from __future__ import annotations

from pathlib import Path
from wsgiref.util import setup_testing_defaults

from nanobot_ops_dashboard.app import create_app
from nanobot_ops_dashboard.config import DashboardConfig
from nanobot_ops_dashboard.storage import init_db


def _call_app(app, path='/'):
    captured = {}
    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = headers
    environ = {}
    setup_testing_defaults(environ)
    environ['PATH_INFO'] = path
    body = b''.join(app(environ, start_response)).decode('utf-8')
    return captured['status'], body


def test_app_overview_renders(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    cfg = DashboardConfig(
        project_root=Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard'),
        db_path=db,
        nanobot_repo_root=tmp_path / 'nanobot',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=Path('/tmp/fake'),
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )
    app = create_app(cfg)
    status, body = _call_app(app, '/')
    assert status.startswith('200')
    assert 'Nanobot Ops Dashboard' in body
