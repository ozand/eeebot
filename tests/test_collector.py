from __future__ import annotations

import json
from pathlib import Path

from nanobot_ops_dashboard.collector import (
    _build_ssh_command,
    _normalize_eeepc_payloads,
    _normalize_repo_state,
    run_poll_loop,
)
from nanobot_ops_dashboard.config import DashboardConfig


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
