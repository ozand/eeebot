from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nanobot_ops_dashboard.collector import (
    _build_ssh_command,
    _load_subagent_telemetry,
    _normalize_eeepc_payloads,
    _normalize_eeepc_state,
    _normalize_repo_state,
    collect_once,
    run_poll_loop,
)
from nanobot_ops_dashboard.config import DashboardConfig
from nanobot_ops_dashboard.reachability import probe_eeepc_reachability
from nanobot_ops_dashboard.storage import fetch_events, fetch_latest_collections, init_db


def test_normalize_repo_state_handles_missing_workspace_state(tmp_path: Path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    result = _normalize_repo_state(repo)
    assert result['source'] == 'repo'
    assert result['status'] == 'unknown'
    assert result['events'] == []


def test_normalize_repo_state_loads_hypothesis_backlog_snapshot(tmp_path: Path):
    repo = tmp_path / 'repo'
    backlog_dir = repo / 'workspace' / 'state' / 'hypotheses'
    backlog_dir.mkdir(parents=True)
    (backlog_dir / 'backlog.json').write_text(
        json.dumps(
            {
                'schema_version': 1,
                'selected_hypothesis_id': 'hyp-2',
                'selected_hypothesis_title': 'Ship dashboard visibility',
                'entries': [
                    {
                        'hypothesis_id': 'hyp-2',
                        'title': 'Ship dashboard visibility',
                        'bounded_priority_score': 0.93,
                        'selection_status': 'selected',
                        'execution_spec': {
                            'goal': 'Expose the backlog live',
                            'task': 'Add operator dashboard routes',
                            'acceptance': 'Operator can see selected hypothesis and top-ranked backlog entries',
                            'budget': {'limit': 3, 'spent': 1, 'remaining': 2, 'currency': 'points'},
                        },
                    }
                ],
            }
        ),
        encoding='utf-8',
    )

    result = _normalize_repo_state(repo)
    backlog = result['raw'].get('hypothesis_backlog')
    assert backlog is not None
    assert backlog['path'].endswith('workspace/state/hypotheses/backlog.json')
    assert backlog['entry_count'] == 1
    assert backlog['selected_hypothesis_id'] == 'hyp-2'
    assert backlog['selected_hypothesis_title'] == 'Ship dashboard visibility'


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


def test_probe_eeepc_reachability_writes_control_artifact_and_reports_unreachable(tmp_path: Path, monkeypatch):
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'db.sqlite3',
        nanobot_repo_root=tmp_path / 'repo',
        eeepc_ssh_host='192.168.1.44',
        eeepc_ssh_key=tmp_path / 'id_ed25519',
        eeepc_state_root='/state',
    )

    def fake_run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            returncode=255,
            cmd=['ssh'],
            stderr='ssh: connect to host 192.168.1.44 port 22: No route to host\n',
        )

    monkeypatch.setattr('nanobot_ops_dashboard.reachability.subprocess.run', fake_run)

    result = probe_eeepc_reachability(cfg)
    assert result['reachable'] is False
    assert result['ssh_host'] == '192.168.1.44'
    assert result['target'] == '192.168.1.44'
    assert result['error'].endswith('No route to host')
    assert result['recommended_next_action'].startswith('Treat as a control-plane incident')
    assert (tmp_path / 'control' / 'eeepc_reachability.json').exists()


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


def test_normalize_eeepc_state_falls_back_to_goals_when_report_index_is_permission_denied(tmp_path: Path, monkeypatch):
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'db.sqlite3',
        nanobot_repo_root=tmp_path / 'repo',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'id_ed25519',
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )

    def fake_probe(_cfg):
        return {'reachable': True}

    def fake_load_ssh_json(_cfg, remote_path: str):
        if remote_path.endswith('/outbox/report.index.json'):
            return None, {
                'source': 'eeepc',
                'stage': f'ssh:{remote_path}',
                'message': 'Permission denied',
                'error_type': 'CalledProcessError',
                'returncode': 1,
            }
        if remote_path.endswith('/goals/registry.json'):
            return {'active_goal_id': 'goal-1'}, None
        if remote_path.endswith('/goals/current.json'):
            return {
                'schema_version': 2,
                'current_task_id': 'task-7',
                'current_task': 'ship plan view',
                'task_counts': {'open': 1},
                'task_list': ['ship plan view', 'wire api'],
                'reward_signal': {'status': 'dense', 'score': 0.75},
                'plan_history': [{'current_task': 'draft plan', 'reward_signal': 'seed'}],
            }, None
        if remote_path.endswith('/goals/active.json'):
            return None, None
        if remote_path.endswith('/goals/history/cycle-1.json'):
            return {'current_task': 'draft plan', 'reward_signal': 'seed'}, None
        raise AssertionError(remote_path)

    monkeypatch.setattr('nanobot_ops_dashboard.collector.probe_eeepc_reachability', fake_probe)
    monkeypatch.setattr('nanobot_ops_dashboard.collector._load_ssh_json', fake_load_ssh_json)
    monkeypatch.setattr('nanobot_ops_dashboard.collector._run_ssh_lines', lambda *_args, **_kwargs: [f'{cfg.eeepc_state_root}/goals/history/cycle-1.json'])
    monkeypatch.setattr(
        'nanobot_ops_dashboard.collector._load_ssh_subagent_telemetry',
        lambda _cfg, _state_root: [
            {
                'subagent_id': 'eeepc-sub-9',
                'task': 'canonical authority-root proof',
                'label': 'authority-root-proof',
                'started_at': '2026-04-21T11:07:20Z',
                'finished_at': '2026-04-21T11:08:20Z',
                'status': 'ok',
                'summary': 'live authority-root telemetry visible',
                'result': 'ok',
                'goal_id': 'goal-1',
                'cycle_id': 'cycle-1',
                'report_path': '/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-1.json',
                'origin': {'channel': 'cli', 'chat_id': 'direct'},
                'parent_context': {'session_key': 'session-eeepc', 'origin': {'channel': 'cli', 'chat_id': 'direct'}},
                'workspace': '/var/lib/eeepc-agent/self-evolving-agent/state',
                '_source_path': '/var/lib/eeepc-agent/self-evolving-agent/state/subagents/latest.json',
            }
        ],
    )

    result = _normalize_eeepc_state(cfg)
    assert result['collection_status'] == 'ok'
    assert result['collection_error'] is None
    assert result['active_goal'] == 'goal-1'
    assert result['current_task'] == 'ship plan view'
    assert result['task_list'] == ['ship plan view', 'wire api']
    assert result['reward_signal']['score'] == 0.75
    assert result['plan_history'][0]['current_task'] == 'draft plan'
    assert result['outbox_source'] == '/var/lib/eeepc-agent/self-evolving-agent/state/goals/current.json'
    assert result['raw']['source_errors']['outbox']['message'] == 'Permission denied'
    assert result['raw']['current_plan']['current_task_id'] == 'task-7'
    subagent_events = [event for event in result['events'] if event['event_type'] == 'subagent']
    assert len(subagent_events) == 1
    assert subagent_events[0]['identity_key'] == 'eeepc-sub-9'
    assert subagent_events[0]['detail']['source_path'].endswith('state/subagents/latest.json')



def test_normalize_eeepc_state_falls_back_to_latest_readable_report_when_authority_indexes_are_locked(tmp_path: Path, monkeypatch):
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=tmp_path / 'db.sqlite3',
        nanobot_repo_root=tmp_path / 'repo',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'id_ed25519',
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )
    latest_report = f'{cfg.eeepc_state_root}/reports/evolution-20260426T172155Z-cycle-18a55ab2ec15.json'

    def fake_probe(_cfg):
        return {'reachable': True}

    def fake_load_ssh_json(_cfg, remote_path: str):
        if remote_path.endswith('/outbox/report.index.json') or remote_path.endswith('/goals/registry.json'):
            return None, {
                'source': 'eeepc',
                'stage': f'ssh:{remote_path}',
                'message': 'Permission denied',
                'error_type': 'CalledProcessError',
                'returncode': 1,
            }
        if remote_path.endswith('/goals/current.json') or remote_path.endswith('/goals/active.json'):
            return None, None
        if remote_path == latest_report:
            return {
                'cycle_id': 'cycle-18a55ab2ec15',
                'cycle_started_utc': '2026-04-26T17:21:55.333883Z',
                'cycle_ended_utc': '2026-04-26T17:21:57.760362Z',
                'result_status': 'PASS',
                'goal_id': 'goal-bootstrap',
                'approval_gate': {'state': 'fresh', 'source': f'{cfg.eeepc_state_root}/approvals/apply.ok'},
                'selected_tasks': 'Record cycle reward [task_id=record-reward]',
                'task_selection_source': 'recorded_current_task',
                'feedback_decision': None,
                'summary': 'Self-evolving cycle PASS — goal=goal-bootstrap — evidence=evidence-d76541e1ddd0',
            }, None
        raise AssertionError(remote_path)

    def fake_run_ssh_lines(_cfg, command: str):
        if '/reports/evolution-*.json' in command:
            return [latest_report]
        return []

    monkeypatch.setattr('nanobot_ops_dashboard.collector.probe_eeepc_reachability', fake_probe)
    monkeypatch.setattr('nanobot_ops_dashboard.collector._load_ssh_json', fake_load_ssh_json)
    monkeypatch.setattr('nanobot_ops_dashboard.collector._run_ssh_lines', fake_run_ssh_lines)
    monkeypatch.setattr('nanobot_ops_dashboard.collector._load_ssh_subagent_telemetry', lambda _cfg, _state_root: [])

    result = _normalize_eeepc_state(cfg)

    assert result['collection_status'] == 'ok'
    assert result['status'] == 'PASS'
    assert result['active_goal'] == 'goal-bootstrap'
    assert result['report_source'] == latest_report
    assert result['outbox_source'] == latest_report
    assert result['raw']['outbox']['feedback_decision'] is None
    assert result['raw']['outbox']['selected_tasks'] == 'Record cycle reward [task_id=record-reward]'
    assert result['raw']['outbox']['task_selection_source'] == 'recorded_current_task'
    assert result['raw']['source_errors']['outbox']['message'] == 'Permission denied'
    assert result['raw']['source_errors']['goals']['message'] == 'Permission denied'



def test_collect_once_persists_plan_fields(tmp_path: Path, monkeypatch):
    db = tmp_path / 'db.sqlite3'
    init_db(db)
    cfg = DashboardConfig(
        project_root=tmp_path,
        db_path=db,
        nanobot_repo_root=tmp_path / 'repo',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=tmp_path / 'id_ed25519',
        eeepc_state_root='/state',
    )

    monkeypatch.setattr(
        'nanobot_ops_dashboard.collector._normalize_repo_state',
        lambda _repo_root, **_kwargs: {
            'source': 'repo',
            'status': 'PASS',
            'active_goal': 'goal-1',
            'current_task': 'ship plan view',
            'task_list': ['ship plan view', {'title': 'write tests'}],
            'reward_signal': {'status': 'dense', 'score': 0.75},
            'plan_history': [{'current_task': 'draft plan', 'reward_signal': 'seed'}],
            'approval_gate': None,
            'gate_state': None,
            'report_source': None,
            'outbox_source': None,
            'artifact_paths': [],
            'promotion_summary': None,
            'promotion_candidate_path': None,
            'promotion_decision_record': None,
            'promotion_accepted_record': None,
            'events': [],
            'raw': {'plan': {'current_task': 'ship plan view'}},
            'collection_status': 'ok',
            'collection_error': None,
        },
    )
    monkeypatch.setattr(
        'nanobot_ops_dashboard.collector._normalize_eeepc_state',
        lambda _cfg: {
            'source': 'eeepc',
            'status': 'BLOCK',
            'active_goal': None,
            'current_task': None,
            'task_list': [],
            'reward_signal': None,
            'plan_history': [],
            'approval_gate': None,
            'gate_state': None,
            'report_source': None,
            'outbox_source': None,
            'artifact_paths': [],
            'promotion_summary': None,
            'promotion_candidate_path': None,
            'promotion_decision_record': None,
            'promotion_accepted_record': None,
            'events': [],
            'reachability': {'reachable': True},
            'raw': {'outbox': {}, 'goals': {}, 'reachability': {'reachable': True}},
            'collection_status': 'ok',
            'collection_error': None,
        },
    )

    collect_once(cfg)

    repo_rows = fetch_latest_collections(db, 'repo', limit=1)
    assert len(repo_rows) == 1
    row = repo_rows[0]
    assert row['current_task'] == 'ship plan view'
    assert json.loads(row['task_list_json'])[1]['title'] == 'write tests'
    assert json.loads(row['reward_signal'])['score'] == 0.75
    assert json.loads(row['plan_history_json'])[0]['current_task'] == 'draft plan'


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


def test_load_subagent_telemetry_is_bounded(tmp_path: Path):
    state_root = tmp_path / 'state'
    telemetry_dir = state_root / 'subagents'
    telemetry_dir.mkdir(parents=True)
    for index in range(5):
        path = telemetry_dir / f'sub-{index}.json'
        path.write_text(json.dumps({'subagent_id': f'sub-{index}', 'status': 'ok'}), encoding='utf-8')

    records = _load_subagent_telemetry(state_root, max_records=2)

    assert len(records) == 2


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
                'goal_id': 'goal-1',
                'cycle_id': 'cycle-1',
                'report_path': '/workspace/state/reports/evolution-1.json',
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
                'goal_id': 'goal-2',
                'cycle_id': 'cycle-2',
                'report_path': '/workspace/state/reports/evolution-2.json',
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
    assert detail['goal_id'] == 'goal-2'
    assert detail['cycle_id'] == 'cycle-2'
    assert detail['report_path'] == '/workspace/state/reports/evolution-2.json'
    assert detail['source_path'].endswith('workspace/state/subagents/sub-2.json')
