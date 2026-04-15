from __future__ import annotations

import json
from pathlib import Path

from nanobot_ops_dashboard.collector import (
    _build_ssh_command,
    _normalize_eeepc_payloads,
    _normalize_repo_state,
    collect_once,
    run_poll_loop,
)
from nanobot_ops_dashboard.config import DashboardConfig
from nanobot_ops_dashboard.storage import fetch_events, init_db


def test_normalize_repo_state_handles_missing_workspace_state(tmp_path: Path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    result = _normalize_repo_state(repo)
    assert result['source'] == 'repo'
    assert result['status'] == 'unknown'
    assert result['events'] == []


def test_build_ssh_command_uses_sudo_password_when_present(tmp_path: Path):
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'db.sqlite3',
        nanobot_repo_root=tmp_path / 'repo',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'id_ed25519',
        eeepc_state_root='/state',
        eeepc_sudo_password='secret',
    )
    cmd = _build_ssh_command(cfg, 'cat /state/outbox/report.index.json')
    joined = ' '.join(cmd)
    assert 'ssh' in cmd[0]
    assert "printf '%s\\n' 'secret' | sudo -S -p '' cat /state/outbox/report.index.json" in joined


def test_normalize_eeepc_payloads_extracts_goal_status_and_artifacts(tmp_path: Path):
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'db.sqlite3',
        nanobot_repo_root=tmp_path / 'repo',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'id_ed25519',
        eeepc_state_root='/state',
    )
    outbox = {
        'status': 'PASS',
        'source': '/state/reports/evolution-1.json',
        'goal': {
            'goal_id': 'goal-1',
            'follow_through': {'artifact_paths': ['prompts/diagnostics.md']},
        },
        'capability_gate': {'approval': {'ok': True, 'reason': 'valid'}},
    }
    goals = {'active_goal_id': 'goal-1'}
    result = _normalize_eeepc_payloads(cfg, outbox, goals)
    assert result['status'] == 'PASS'
    assert result['active_goal'] == 'goal-1'
    assert result['gate_state'] == 'valid'
    assert result['artifact_paths'] == ['prompts/diagnostics.md']
    assert result['events'][0]['identity_key'] == '/state/reports/evolution-1.json'
    assert result['events'][0]['detail']['failure_class'] is None
    assert result['events'][0]['detail']['blocked_next_step'] is None


class _StopPolling(Exception):
    pass


def test_run_poll_loop_collects_requested_iterations(tmp_path: Path, monkeypatch):
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'db.sqlite3',
        nanobot_repo_root=tmp_path / 'repo',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'id_ed25519',
        eeepc_state_root='/state',
        poll_interval_seconds=1,
    )
    calls = []

    monkeypatch.setattr('nanobot_ops_dashboard.collector.collect_once', lambda _cfg: calls.append('x') or {'ok': True})
    monkeypatch.setattr('nanobot_ops_dashboard.collector.time.sleep', lambda _seconds: None)

    run_poll_loop(cfg, iterations=3)

    assert calls == ['x', 'x', 'x']


def test_normalize_repo_state_loads_subagent_telemetry(tmp_path: Path):
    repo = tmp_path / 'repo'
    telemetry_dir = repo / 'workspace' / 'state' / 'subagents'
    telemetry_dir.mkdir(parents=True)
    (telemetry_dir / 'sub-1.json').write_text(
        json.dumps(
            {
                'subagent_id': 'sub-1',
                'task': 'fix the widget',
                'label': 'widget-fix',
                'started_at': '2026-04-16T12:00:00Z',
                'finished_at': '2026-04-16T12:01:00Z',
                'status': 'ok',
                'summary': 'done',
                'result': 'done',
                'origin': {'channel': 'cli', 'chat_id': 'direct'},
                'parent_context': {'session_key': 'session-1', 'origin': {'channel': 'cli', 'chat_id': 'direct'}},
                'workspace': str(repo / 'workspace'),
            }
        ),
        encoding='utf-8',
    )

    result = _normalize_repo_state(repo)
    subagent_events = [event for event in result['events'] if event['event_type'] == 'subagent']
    assert len(subagent_events) == 1
    event = subagent_events[0]
    assert event['identity_key'] == 'sub-1'
    assert event['title'] == 'widget-fix'
    assert event['status'] == 'ok'
    assert event['detail']['task'] == 'fix the widget'
    assert event['detail']['started_at'] == '2026-04-16T12:00:00Z'
    assert event['detail']['finished_at'] == '2026-04-16T12:01:00Z'
    assert event['detail']['origin']['channel'] == 'cli'
    assert event['detail']['parent_context']['session_key'] == 'session-1'


def test_collect_once_persists_subagent_telemetry(tmp_path: Path):
    repo = tmp_path / 'repo'
    workspace_state = repo / 'workspace' / 'state' / 'subagents'
    workspace_state.mkdir(parents=True)
    (workspace_state / 'sub-2.json').write_text(
        json.dumps(
            {
                'subagent_id': 'sub-2',
                'task': 'collect docs',
                'label': 'docs',
                'started_at': '2026-04-16T12:10:00Z',
                'finished_at': '2026-04-16T12:11:00Z',
                'status': 'ok',
                'summary': 'docs collected',
                'result': 'docs collected',
                'origin': {'channel': 'cli', 'chat_id': 'direct'},
                'parent_context': {'session_key': 'session-2', 'origin': {'channel': 'cli', 'chat_id': 'direct'}},
                'workspace': str(repo / 'workspace'),
            }
        ),
        encoding='utf-8',
    )

    db = tmp_path / 'db.sqlite3'
    init_db(db)
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=db,
        nanobot_repo_root=repo,
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'id_ed25519',
        eeepc_state_root='/state',
    )

    collect_once(cfg)

    events = fetch_events(db, 'repo', 'subagent', limit=10)
    assert len(events) == 1
    row = events[0]
    detail = json.loads(row['detail_json'])
    assert row['identity_key'] == 'sub-2'
    assert row['status'] == 'ok'
    assert detail['task'] == 'collect docs'
    assert detail['source_path'].endswith('workspace/state/subagents/sub-2.json')
