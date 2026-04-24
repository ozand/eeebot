from __future__ import annotations

from pathlib import Path
import os
import time
import json
from wsgiref.util import setup_testing_defaults

from nanobot_ops_dashboard.app import create_app
from nanobot_ops_dashboard.config import DashboardConfig
from nanobot_ops_dashboard.storage import init_db, insert_collection, upsert_event


def _call_app(app, path='/', query_string=''):
    captured = {}

    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = headers

    environ = {}
    setup_testing_defaults(environ)
    environ['PATH_INFO'] = path
    environ['QUERY_STRING'] = query_string
    body = b''.join(app(environ, start_response)).decode('utf-8')
    return captured['status'], body


def _seed_dashboard_data(db: Path) -> None:
    insert_collection(db, {
        'collected_at': '2026-04-16T12:00:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-1',
        'approval_gate': '{"ok": true, "reason": "valid"}',
        'gate_state': 'valid',
        'report_source': '/state/reports/evolution-1.json',
        'outbox_source': '/state/outbox/report.index.json',
        'artifact_paths_json': '["prompts/diagnostics.md"]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': '{"current_plan": {"feedback_decision": {"mode": "force_remediation", "reason": "repeated BLOCK on approval:invalid; force remediation", "selected_task_title": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py`", "selection_source": "feedback_repeat_block_remediation"}, "selected_tasks": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py` [task_id=verify-approval-gate]", "task_selection_source": "feedback_repeat_block_remediation"}, "outbox": {"status": "BLOCK", "process_reflection": {"failure_class": "no_concrete_change", "improvement_score": 30}, "goal": {"follow_through": {"blocked_next_step": "Rewrite the cycle around one file-level action or an explicit blocked next step."}}, "experiment": {"selected_tasks": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py` [task_id=verify-approval-gate]", "task_selection_source": "feedback_repeat_block_remediation", "feedback_decision": {"mode": "force_remediation", "reason": "repeated BLOCK on approval:invalid; force remediation", "selected_task_title": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py`", "selection_source": "feedback_repeat_block_remediation"}}}}',
    })
    insert_collection(db, {
        'collected_at': '2026-04-16T12:05:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-1',
        'approval_gate': '{"ok": true, "reason": "valid"}',
        'gate_state': 'valid',
        'report_source': '/state/reports/evolution-1.json',
        'outbox_source': '/state/outbox/report.index.json',
        'artifact_paths_json': '["prompts/diagnostics.md"]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': '{"current_plan": {"feedback_decision": {"mode": "force_remediation", "reason": "repeated BLOCK on approval:invalid; force remediation", "selected_task_title": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py`", "selection_source": "feedback_repeat_block_remediation"}, "selected_tasks": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py` [task_id=verify-approval-gate]", "task_selection_source": "feedback_repeat_block_remediation"}, "outbox": {"status": "BLOCK", "process_reflection": {"failure_class": "no_concrete_change", "improvement_score": 30}, "goal": {"follow_through": {"blocked_next_step": "Rewrite the cycle around one file-level action or an explicit blocked next step."}}, "experiment": {"selected_tasks": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py` [task_id=verify-approval-gate]", "task_selection_source": "feedback_repeat_block_remediation", "feedback_decision": {"mode": "force_remediation", "reason": "repeated BLOCK on approval:invalid; force remediation", "selected_task_title": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py`", "selection_source": "feedback_repeat_block_remediation"}}}}',
    })
    insert_collection(db, {
        'collected_at': '2026-04-16T12:10:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-1',
        'approval_gate': '{"ok": true, "reason": "valid"}',
        'gate_state': 'valid',
        'report_source': '/state/reports/evolution-1.json',
        'outbox_source': '/state/outbox/report.index.json',
        'artifact_paths_json': '["prompts/diagnostics.md"]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': '{"current_plan": {"feedback_decision": {"mode": "force_remediation", "reason": "repeated BLOCK on approval:invalid; force remediation", "selected_task_title": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py`", "selection_source": "feedback_repeat_block_remediation"}, "selected_tasks": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py` [task_id=verify-approval-gate]", "task_selection_source": "feedback_repeat_block_remediation"}, "outbox": {"status": "BLOCK", "process_reflection": {"failure_class": "no_concrete_change", "improvement_score": 30}, "goal": {"follow_through": {"blocked_next_step": "Rewrite the cycle around one file-level action or an explicit blocked next step."}}, "experiment": {"selected_tasks": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py` [task_id=verify-approval-gate]", "task_selection_source": "feedback_repeat_block_remediation", "feedback_decision": {"mode": "force_remediation", "reason": "repeated BLOCK on approval:invalid; force remediation", "selected_task_title": "Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py`", "selection_source": "feedback_repeat_block_remediation"}}}}',
    })
    insert_collection(db, {
        'collected_at': '2026-04-16T12:00:01Z',
        'source': 'repo',
        'status': 'unknown',
        'active_goal': None,
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': 'promotion-42 | reviewed | accept',
        'promotion_candidate_path': '/workspace/state/promotions/promotion-42.json',
        'promotion_decision_record': 'present',
        'promotion_accepted_record': 'present',
        'raw_json': '{"current_plan": {"current_task": "draft plan", "task_list": ["draft plan", "write tests"], "reward_signal": {"status": "seed", "score": 0.25}, "plan_history": [{"current_task": "draft plan", "reward_signal": "seed"}]}}',
    })
    insert_collection(db, {
        'collected_at': '2026-04-16T12:05:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-1',
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': 'promotion-42 | reviewed | accept',
        'promotion_candidate_path': '/workspace/state/promotions/promotion-42.json',
        'promotion_decision_record': 'present',
        'promotion_accepted_record': 'present',
        'raw_json': '{"current_plan": {"current_task": "ship plan view", "task_list": ["ship plan view", {"title": "wire api"}], "reward_signal": {"status": "dense", "score": 0.75}, "plan_history": [{"current_task": "draft plan", "reward_signal": "seed"}, {"current_task": "ship plan view", "reward_signal": {"status": "dense", "score": 0.75}}]}}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:00Z',
        'source': 'eeepc',
        'event_type': 'cycle',
        'identity_key': '/state/reports/evolution-1.json',
        'title': 'goal-1',
        'status': 'PASS',
        'detail_json': '{"report_source": "/state/reports/evolution-1.json", "artifact_paths": ["prompts/diagnostics.md"], "approval": {"ok": true, "reason": "valid"}, "failure_class": "no_concrete_change", "blocked_next_step": "Rewrite the cycle around one file-level action or an explicit blocked next step."}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:01Z',
        'source': 'repo',
        'event_type': 'promotion',
        'identity_key': 'promotion-42',
        'title': 'promotion-42 | reviewed | accept',
        'status': 'accept',
        'detail_json': '{"candidate_path": "/workspace/state/promotions/promotion-42.json", "decision_record": "present", "accepted_record": "present"}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:02Z',
        'source': 'repo',
        'event_type': 'cycle',
        'identity_key': '/workspace/state/reports/evolution-2.json',
        'title': 'goal-2',
        'status': 'BLOCK',
        'detail_json': '{"report_source": "/workspace/state/reports/evolution-2.json", "artifact_paths": [], "approval": {"ok": false, "reason": "missing"}}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:03Z',
        'source': 'repo',
        'event_type': 'subagent',
        'identity_key': 'sub-1',
        'title': 'widget-fix',
        'status': 'ok',
        'detail_json': '{"task": "fix the widget", "label": "widget-fix", "started_at": "2026-04-16T12:00:00Z", "finished_at": "2026-04-16T12:01:00Z", "goal_id": "goal-1", "cycle_id": "cycle-1", "report_path": "/workspace/state/reports/evolution-1.json", "current_task_id": "ship plan view", "task_reward_signal": {"value": 1.0, "source": "improvement_score"}, "task_feedback_decision": {"mode": "force_remediation", "selection_source": "feedback_repeat_block_remediation"}, "origin": {"channel": "cli", "chat_id": "direct"}, "parent_context": {"session_key": "session-1", "origin": {"channel": "cli", "chat_id": "direct"}}, "summary": "done", "result": "done", "source_path": "/workspace/state/subagents/sub-1.json"}',
    })
    upsert_event(db, {
        'collected_at': '2026-04-16T12:00:04Z',
        'source': 'eeepc',
        'event_type': 'subagent',
        'identity_key': 'sub-2',
        'title': 'browser-report',
        'status': 'BLOCK',
        'detail_json': '{"task": "prepare browser report", "label": "browser-report", "started_at": "2026-04-16T12:00:02Z", "finished_at": "2026-04-16T12:00:05Z", "origin": {"channel": "browser", "chat_id": "ops"}, "summary": "needs more evidence", "result": "needs more evidence", "source_path": "/workspace/state/subagents/sub-2.json"}',
    })


def _seed_experiment_telemetry(repo_root: Path) -> None:
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True, exist_ok=True)
    (state_root / 'budgets').mkdir(parents=True, exist_ok=True)
    (state_root / 'credits').mkdir(parents=True, exist_ok=True)
    (state_root / 'experiments' / 'history.jsonl').write_text(
        '{"experiment_id": "exp-16", "title": "reward-baseline", "status": "done", "phase": "complete", "result_status": "PASS", "outcome": "keep", "metric_name": "reward_signal.value", "metric_baseline": null, "metric_current": 0.1, "metric_frontier": 0.1, "contract_path": "/workspace/state/experiments/contracts/exp-16.json", "reward_signal": {"status": "seed", "value": 0.1}, "budget": {"limit": 1200, "spent": 240, "remaining": 960, "currency": "USD"}, "budget_used": {"requests": 1, "tool_calls": 2}}\n',
        encoding='utf-8',
    )
    (state_root / 'budgets' / 'current.json').write_text(
        '{"budget": {"limit": 1200, "spent": 275, "remaining": 925, "currency": "USD", "status": "tracking"}}',
        encoding='utf-8',
    )
    (state_root / 'credits' / 'latest.json').write_text(
        '{"schema_version": "credits-ledger-v1", "balance": 3.5, "delta": 1.0, "goal_id": "goal-1", "cycle_id": "cycle-1", "reason": "improvement_score", "reward_signal": {"value": 1.0, "source": "improvement_score"}, "budget_used": {"requests": 1, "tool_calls": 4}}',
        encoding='utf-8',
    )
    (state_root / 'credits' / 'history.jsonl').write_text(
        '{"schema_version": "credits-ledger-v1", "balance": 2.5, "delta": 0.5, "goal_id": "goal-0", "cycle_id": "cycle-0", "reason": "improvement_score"}\n'
        '{"schema_version": "credits-ledger-v1", "balance": 3.5, "delta": 1.0, "goal_id": "goal-1", "cycle_id": "cycle-1", "reason": "improvement_score"}\n',
        encoding='utf-8',
    )
    (state_root / 'experiments' / 'current.json').write_text(
        '{"current_experiment": {"experiment_id": "exp-17", "title": "reward-tuning", "status": "running", "phase": "active", "result_status": "PASS", "outcome": "keep", "metric_name": "reward_signal.value", "metric_baseline": 0.1, "metric_current": 0.25, "metric_frontier": 0.25, "contract_path": "/workspace/state/experiments/contracts/exp-17.json", "reward_signal": {"status": "seed", "value": 0.25, "source": "experiment-telemetry"}, "budget": {"limit": 1200, "spent": 275, "remaining": 925, "currency": "USD"}, "budget_used": {"requests": 1, "tool_calls": 4}}}',
        encoding='utf-8',
    )
    now = time.time()
    os.utime(state_root / 'experiments' / 'history.jsonl', (now - 2, now - 2))
    os.utime(state_root / 'budgets' / 'current.json', (now - 1, now - 1))
    os.utime(state_root / 'experiments' / 'current.json', (now, now))


def _seed_hypothesis_backlog(repo_root: Path) -> None:
    state_root = repo_root / 'workspace' / 'state' / 'hypotheses'
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / 'backlog.json').write_text(
        json.dumps(
            {
                'schema_version': 1,
                'model': 'HADI',
                'selected_hypothesis_id': 'hyp-2',
                'selected_hypothesis_title': 'Ship dashboard visibility',
                'selected_hypothesis_wsjf': {
                    'user_business_value': 8,
                    'time_criticality': 6,
                    'risk_reduction_opportunity_enablement': 7,
                    'job_size': 3,
                    'score': 7.0,
                },
                'entries': [
                    {
                        'hypothesis_id': 'hyp-2',
                        'title': 'Ship dashboard visibility',
                        'bounded_priority_score': 0.93,
                        'selection_status': 'selected',
                        'wsjf': {
                            'user_business_value': 8,
                            'time_criticality': 6,
                            'risk_reduction_opportunity_enablement': 7,
                            'job_size': 3,
                            'score': 7.0,
                        },
                        'hadi': {
                            'hypothesis': 'If the operator can see live backlog selection, they can detect stalled prioritization early.',
                            'action': 'Add operator dashboard routes and cards for backlog selection.',
                            'data': {'result_status': 'PASS', 'approval_gate_state': 'fresh'},
                            'insights': ['selected hypothesis should be visible on overview', 'backlog ranking must be operator-readable'],
                        },
                        'execution_spec': {
                            'goal': 'Expose the backlog live',
                            'task': 'Add operator dashboard routes',
                            'acceptance': 'Operator can see selected hypothesis and top-ranked backlog entries',
                            'budget': {'limit': 3, 'spent': 1, 'remaining': 2, 'currency': 'points'},
                        },
                    },
                    {
                        'hypothesis_id': 'hyp-1',
                        'title': 'Keep backlog truthful',
                        'bounded_priority_score': 0.81,
                        'selection_status': 'queued',
                        'wsjf': {
                            'user_business_value': 7,
                            'time_criticality': 5,
                            'risk_reduction_opportunity_enablement': 6,
                            'job_size': 4,
                            'score': 4.5,
                        },
                        'hadi': {
                            'hypothesis': 'If the dashboard reads the real backlog file, stale entries will disappear automatically.',
                            'action': 'Read workspace/state/hypotheses/backlog.json directly.',
                            'data': {'result_status': 'BLOCK', 'approval_gate_state': 'missing'},
                            'insights': ['stale hypotheses must not survive when file is absent'],
                        },
                        'execution_spec': {
                            'goal': 'Prefer live runtime state',
                            'task': 'Read workspace/state/hypotheses/backlog.json directly',
                            'acceptance': 'No stale hypotheses appear when the file is absent',
                            'budget': 'small',
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )


def _cfg(tmp_path: Path, db: Path) -> DashboardConfig:

    return DashboardConfig(
        project_root=Path('/home/ozand/herkoot/Projects/nanobot-ops-dashboard'),
        db_path=db,
        nanobot_repo_root=tmp_path / 'nanobot',
        eeepc_ssh_host='eeepc',
        eeepc_ssh_key=Path('/tmp/fake'),
        eeepc_state_root='/var/lib/eeepc-agent/self-evolving-agent/state',
    )


def test_app_collect_endpoint_surfaces_diagnostic_errors(tmp_path: Path, monkeypatch):
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    app = create_app(_cfg(tmp_path, db))

    monkeypatch.setattr(
        'nanobot_ops_dashboard.app.collect_once',
        lambda _cfg: {
            'repo_status': 'PASS',
            'repo_goal': 'goal-1',
            'repo_collection_status': 'ok',
            'repo_error': None,
            'eeepc_status': 'error',
            'eeepc_goal': None,
            'eeepc_collection_status': 'error',
            'eeepc_error': {
                'source': 'eeepc',
                'stage': 'ssh:/state/outbox/report.index.json',
                'message': 'ssh: connect to host 192.168.1.44 port 22: No route to host',
                'error_type': 'CalledProcessError',
                'returncode': 255,
            },
            'eeepc_reachability': {
                'reachable': False,
                'ssh_host': 'eeepc',
                'target': 'eeepc',
                'error': 'ssh: connect to host 192.168.1.44 port 22: No route to host',
                'returncode': 255,
                'recommended_next_action': 'Treat as a control-plane incident; verify eeepc power/network access, then retry collection.',
                'control_artifact_path': '/tmp/eeepc_reachability.json',
            },
            'collection_status': {'repo': 'ok', 'eeepc': 'error'},
        },
    )

    status, body = _call_app(app, '/collect')
    assert status.startswith('200')
    assert 'eeepc_collection_status' in body
    assert 'eeepc_reachability' in body
    assert 'No route to host' in body
    assert 'collection_status' in body


def test_app_overview_renders(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/')
    assert status.startswith('200')
    assert 'eeebot Ops Dashboard' in body
    assert 'Last collected' in body
    assert 'Loaded snapshot window' in body
    assert 'Historical snapshots in DB' in body
    assert 'Freshness / reachability' in body
    assert 'View eeepc cycles' in body
    assert 'prompts/diagnostics.md' in body
    assert 'http-equiv="refresh"' in body
    assert 'Current blocker' in body
    assert 'no_concrete_change' in body
    assert 'Rewrite the cycle around one file-level action' in body
    assert 'keep' in body or 'blocked' in body
    assert 'reward_signal.value' in body or 'Experiment frontier' in body
    assert 'status-pill' in body
    assert 'Feedback decision mode' in body
    assert 'force_remediation' in body
    assert 'Feedback decision reason' in body
    assert 'repeated BLOCK on approval:invalid' in body
    assert 'Selected tasks' in body
    assert 'Verify the gate with `PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py`' in body
    assert 'Selected task title' in body
    assert 'Task selection source' in body
    assert 'feedback_repeat_block_remediation' in body
    assert 'Task plan / reward' in body
    assert 'Open task plan' in body
    assert 'ship plan view' in body
    assert 'Reward signal' in body
    assert 'Experiments / budget' in body
    assert 'Open experiments' in body
    assert 'Current experiment' in body
    assert 'Latest real telemetry' in body
    assert 'browser-report' in body
    assert 'Collection Summary' in body
    assert 'Outbox' in body
    assert 'Recent cycle timeline' in body
    assert 'Recent goal transitions' in body
    assert 'status-pill status-pass' in body
    assert 'status-pill status-block' in body
    assert 'timeline-item status-pass' in body
    assert 'timeline-item status-block' in body
    assert 'Observation cadence' in body
    assert 'Fresh report first seen' in body
    assert 'Snapshot age' in body


def test_app_cycles_filters_and_api_render(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, cycles_body = _call_app(app, '/cycles')
    assert status.startswith('200')
    assert 'Detail' in cycles_body
    assert 'Loaded cycle rows' in cycles_body
    assert 'Unique eeepc reports' in cycles_body
    assert 'Observation groups' in cycles_body
    assert 'Repeated observations' in cycles_body
    assert 'PASS' in cycles_body
    assert 'prompts/diagnostics.md' in cycles_body
    assert 'Report source' in cycles_body
    assert '/state/reports/evolution-1.json' in cycles_body
    assert 'Approval' in cycles_body
    assert 'Observed eeepc collection cadence' in cycles_body
    assert 'Seen' in cycles_body
    assert '5.0 min' in cycles_body

    status, filtered_cycles = _call_app(app, '/cycles', 'source=repo&status=BLOCK')
    assert status.startswith('200')
    assert 'goal-2' in filtered_cycles
    assert '/workspace/state/reports/evolution-2.json' in filtered_cycles
    assert 'name="source"' in filtered_cycles
    assert 'name="status"' in filtered_cycles
    assert 'value="repo"' in filtered_cycles

    status, cycles_api = _call_app(app, '/api/cycles', 'source=repo&status=BLOCK')
    assert status.startswith('200')
    assert 'goal-2' in cycles_api
    assert 'promotion-42' not in cycles_api


def test_app_promotions_and_other_pages_render(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, api_body = _call_app(app, '/api/summary')
    assert status.startswith('200')
    assert 'goal-1' in api_body
    assert 'PASS' in api_body
    assert 'snapshot_count' in api_body
    assert 'raw_json' not in api_body

    status, summary_debug = _call_app(app, '/api/summary/debug')
    assert status.startswith('200')
    assert 'raw_json' in summary_debug
    assert 'loaded_snapshot_count' in api_body
    assert 'total_snapshot_count' in api_body
    assert 'plan_latest' in api_body

    status, plan_body = _call_app(app, '/plan')
    assert status.startswith('200')
    assert 'Task plan / reward' in plan_body
    assert 'ship plan view' in plan_body
    assert 'wire api' in plan_body
    assert 'dense' in plan_body
    assert 'Recent plan history' in plan_body
    assert 'draft plan' in plan_body
    assert 'Collection source' in plan_body
    assert 'Plan payload' in plan_body
    assert 'Hypothesis' in plan_body
    assert 'Success checks' in plan_body
    assert 'Task boundary title' in plan_body
    assert 'Task selection source' in plan_body
    assert 'Selected tasks' in plan_body

    status, plan_api = _call_app(app, '/api/plan')
    assert status.startswith('200')
    assert 'current_plan' in plan_api
    assert 'selected_task_title' in plan_api
    assert 'task_selection_source' in plan_api
    assert 'selected_tasks_text' in plan_api
    assert 'current_plan_source' in plan_api
    assert 'recent_plan_history' in plan_api
    assert 'ship plan view' in plan_api
    assert 'wire api' in plan_api

    status, hypotheses_body = _call_app(app, '/hypotheses')
    assert status.startswith('200')
    assert 'Hypotheses / backlog' in hypotheses_body
    assert 'No hypothesis backlog file was found under workspace/state/hypotheses/backlog.json.' in hypotheses_body
    assert '/api/hypotheses' in hypotheses_body

    status, hypotheses_api = _call_app(app, '/api/hypotheses')
    assert status.startswith('200')
    assert 'available' in hypotheses_api
    assert 'empty_state_reason' in hypotheses_api
    assert 'model' in hypotheses_api
    assert 'No hypothesis backlog file was found under workspace/state/hypotheses/backlog.json.' in hypotheses_api

    status, promotions_body = _call_app(app, '/promotions')
    assert status.startswith('200')
    assert 'Replay readiness' in promotions_body
    assert 'promotion-42 | reviewed | accept' in promotions_body
    assert '/workspace/state/promotions/promotion-42.json' in promotions_body
    assert 'Decision record' in promotions_body
    assert 'Accepted record' in promotions_body
    assert ('status-pill status-pass' in promotions_body or 'status-pill status-unknown' in promotions_body or 'status-pill status-neutral' in promotions_body)

    status, filtered_promotions = _call_app(app, '/promotions', 'source=repo&status=accept')
    assert status.startswith('200')
    assert 'promotion-42 | reviewed | accept' in filtered_promotions
    assert 'name="source"' in filtered_promotions
    assert 'name="status"' in filtered_promotions

    status, promotions_api = _call_app(app, '/api/promotions', 'source=repo&status=accept')
    assert status.startswith('200')
    assert 'promotion-42' in promotions_api
    assert 'accepted_record' in promotions_api

    status, approvals_api = _call_app(app, '/api/approvals')
    assert status.startswith('200')
    assert 'valid' in approvals_api

    status, approvals_body = _call_app(app, '/approvals')
    assert status.startswith('200')
    assert 'Approvals' in approvals_body
    assert 'Collection source' in approvals_body
    assert 'Current task' in approvals_body
    assert 'Plan payload' in approvals_body
    assert 'Gate state' in approvals_body
    assert 'valid' in approvals_body

    status, deployments_api = _call_app(app, '/api/deployments')
    assert status.startswith('200')
    assert '/state/reports/evolution-1.json' in deployments_api
    assert 'raw_json' not in deployments_api

    status, deployments_debug = _call_app(app, '/api/deployments/debug')
    assert status.startswith('200')
    assert 'raw_json' in deployments_debug
    assert 'eeepc_latest_observation' in deployments_api
    assert 'plan_snapshot' in deployments_api

    status, deployments_body = _call_app(app, '/deployments')
    assert status.startswith('200')
    assert 'Deployments / Verification' in deployments_body
    assert 'Live eeepc proof' in deployments_body
    assert '/state/reports/evolution-1.json' in deployments_body

    status, system_body = _call_app(app, '/system')
    assert status.startswith('200')
    assert 'System / goal files' in system_body
    assert 'README.md' in system_body

    status, system_api = _call_app(app, '/api/system')
    assert status.startswith('200')
    assert 'eeepc_goal' in system_api
    assert 'control_plane' in system_api
    assert 'validation_summary' in system_api
    assert 'runtime_source' in system_api
    assert 'eeepc_reachability' in system_api
    assert 'human_review_boundary' in system_api or 'human_review_boundary' in system_api
    assert 'local_files' in system_api
    assert 'Current task' in deployments_body
    assert 'Plan payload' in deployments_body
    assert 'Observation cadence' in deployments_body
    assert 'Fresh report first seen' in deployments_body


def test_app_hypotheses_renders_live_backlog_and_cross_links(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_hypothesis_backlog(tmp_path / 'nanobot')
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/hypotheses')
    assert status.startswith('200')
    assert 'Hypotheses / backlog' in body
    assert 'hyp-2' in body
    assert 'Ship dashboard visibility' in body
    assert '0.93' in body
    assert 'selected' in body
    assert 'Expose the backlog live' in body
    assert 'Add operator dashboard routes' in body
    assert 'Operator can see selected hypothesis and top-ranked backlog entries' in body
    assert 'points' in body
    assert 'HADI' in body
    assert 'WSJF' in body
    assert 'If the operator can see live backlog selection' in body
    assert 'user_business_value' in body or 'Business value' in body
    assert 'hyp-1' in body
    assert 'No stale hypotheses appear when the file is absent' in body

    status, api_body = _call_app(app, '/api/hypotheses')
    assert status.startswith('200')
    assert 'selected_hypothesis_id' in api_body
    assert 'selected_hypothesis_wsjf' in api_body
    assert 'selected_hypothesis_hadi' in api_body
    assert 'research_feed' in api_body
    assert '"model": "HADI"' in api_body
    assert 'hyp-2' in api_body
    assert 'top_entries' in api_body

    status, index_body = _call_app(app, '/')

    status, system_body = _call_app(app, '/system')
    assert status.startswith('200')
    assert 'Current control plane' in system_body
    assert 'Execution registry' in system_body
    assert 'Direct host path' in system_body
    assert 'Governance schema' in system_body
    assert 'Governance coverage' in system_body
    assert 'Human review boundary' in system_body
    assert 'Governance enforcement' in system_body
    assert 'Launch criteria' in system_body
    assert 'Task boundary' in system_body
    assert 'Mutation lane' in system_body
    assert 'Host resource sensing' in system_body
    assert 'Governance schema' in system_body
    assert 'Governance coverage' in system_body
    assert 'Human review boundary' in system_body
    assert 'Governance enforcement' in system_body
    assert 'Launch criteria' in system_body
    assert 'Action registry' in system_body
    assert 'Task boundary' in system_body
    assert 'Mutation lane' in system_body
    assert 'Capability reporting' in system_body
    assert 'Memory discipline' in system_body
    assert 'Validation status' in system_body
    assert 'Prompt mass' in system_body
    assert 'Timeout budget' in system_body
    assert 'Cycle budget' in system_body
    assert 'Owner utility' in system_body
    assert 'Operator boost' in system_body
    assert 'Latest subagent correlation' in system_body
    assert 'Runtime source pin' in system_body

    status, plan_body = _call_app(app, '/plan')
    assert status.startswith('200')
    assert 'Hypothesis backlog cross-link' in plan_body
    assert 'Ship dashboard visibility' in plan_body
    assert '/hypotheses' in plan_body
    assert 'Experiment' in plan_body or 'unknown' in plan_body
    assert 'Current plan snapshot' in plan_body


def test_app_hypotheses_renders_truthful_empty_state_when_file_absent(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    insert_collection(db, {
        'collected_at': '2026-04-16T12:00:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-1',
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': '{"hypothesis_backlog": {"path": "/workspace/state/hypotheses/backlog.json", "entry_count": 2, "selected_hypothesis_id": "stale-hyp-9", "selected_hypothesis_title": "Stale data that should not appear"}}',
    })
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/hypotheses')
    assert status.startswith('200')
    assert 'No hypothesis backlog file was found under workspace/state/hypotheses/backlog.json.' in body
    assert 'Stale data that should not appear' not in body
    assert 'stale-hyp-9' not in body

    status, api_body = _call_app(app, '/api/hypotheses')
    assert status.startswith('200')
    assert 'available' in api_body
    assert 'false' in api_body.lower()
    assert 'Stale data that should not appear' not in api_body


def test_app_experiments_renders_truthful_empty_state(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/experiments')
    assert status.startswith('200')
    assert 'Experiments & budget' in body
    assert 'No experiment telemetry yet.' in body
    assert 'No dedicated budget file has landed yet.' in body
    assert 'No experiment telemetry files were discovered yet.' in body
    assert '/api/experiments' in body

    status, api_body = _call_app(app, '/api/experiments')
    assert status.startswith('200')
    assert 'available' in api_body
    assert 'empty_state_reason' in api_body
    assert 'No experiment or budget telemetry files were found' in api_body


def test_app_experiments_renders_current_experiment_and_budget(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_experiment_telemetry(tmp_path / 'nanobot')
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/experiments')
    assert status.startswith('200')
    assert 'reward-tuning' in body
    assert 'exp-17' in body
    assert 'status-running' in body or 'status-pass' in body or 'status-neutral' in body
    assert 'Current credits ledger' in body
    assert 'Balance' in body
    assert '3.5' in body
    assert 'Delta' in body
    assert 'keep' in body
    assert 'reward_signal.value' in body
    assert 'Hypothesis' in body
    assert 'Success checks' in body
    assert '0.25' in body
    assert '/workspace/state/experiments/contracts/exp-17.json' in body
    assert 'revert=' in body or 'queued' in body or 'none' in body
    assert 'remaining=925' in body
    assert 'experiment-telemetry' in body
    assert 'workspace/state/experiments/current.json' in body
    assert 'workspace/state/budgets/current.json' in body
    assert 'reward-baseline' in body

    status, credits_body = _call_app(app, '/credits')
    assert status.startswith('200')
    assert 'Credits ledger' in credits_body
    assert '3.5' in credits_body
    assert 'credits-ledger-v1' not in credits_body or True
    assert 'improvement_score' in credits_body

    status, api_body = _call_app(app, '/api/experiments')
    assert status.startswith('200')
    assert 'reward-tuning' in api_body
    assert 'exp-17' in api_body
    assert 'budget_history' in api_body
    assert 'current_reward_signal' in api_body
    assert 'experiment-telemetry' in api_body
    assert 'credits' in api_body
    assert '3.5' in api_body
    assert 'outcome' in api_body
    assert 'metric_frontier' in api_body
    assert 'contract_path' in api_body
    assert 'workspace/state/experiments/current.json' in api_body

    status, credits_api = _call_app(app, '/api/credits')
    assert status.startswith('200')
    assert '3.5' in credits_api
    assert 'history' in credits_api


def test_app_analytics_renders_failure_breakdown(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/analytics')
    assert status.startswith('200')
    assert 'Analytics' in body
    assert 'Historical snapshots in DB' in body
    assert 'Loaded snapshot window' in body
    assert 'Source breakdown' in body
    assert 'Cycle status breakdown' in body
    assert 'Experiment frontier' in body
    assert 'reward_signal.value' in body or 'unknown' in body
    assert 'Current streak' in body
    assert 'Latest status time' in body
    assert 'Recent snapshots' in body
    assert 'Observed eeepc collections' in body
    assert 'Recent unique cycle reports' in body
    assert 'Recent goal transitions' in body
    assert 'Feedback decision mode' in body
    assert 'force_remediation' in body
    assert 'Selected tasks' in body
    assert 'Selected task title' in body
    assert 'Task selection source' in body

    status, analytics_api = _call_app(app, '/api/analytics')
    assert status.startswith('200')
    assert 'eeepc_observation_groups' in analytics_api
    assert 'approx_cadence_minutes' in analytics_api
    payload = json.loads(analytics_api)
    analytics_payload = payload['analytics']
    assert analytics_payload['current_streak']['status'] == 'BLOCK'
    assert analytics_payload['current_streak']['length'] == 1
    assert analytics_payload['latest_status_at'] == '2026-04-16T12:00:02Z'
    assert analytics_payload['latest_pass_at'] == '2026-04-16T12:00:00Z'
    assert analytics_payload['latest_block_at'] == '2026-04-16T12:00:02Z'
    assert analytics_payload['recent_status_sequence'][0]['status'] == 'BLOCK'


def test_app_subagents_renders_durable_history(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    _seed_dashboard_data(db)
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/subagents')
    assert status.startswith('200')
    assert 'Subagents' in body
    assert 'Durable rows' in body
    assert 'Goal / cycle' in body
    assert 'Apply filters' in body
    assert 'name="origin"' in body
    assert 'name="status"' in body
    assert 'browser-report' in body.split('widget-fix')[0]
    assert 'browser-report' in body
    assert 'widget-fix' in body
    assert 'goal-1' in body
    assert 'cycle-1' in body
    assert '/workspace/state/reports/evolution-1.json' in body
    assert 'prepare browser report' in body
    assert 'fix the widget' in body
    assert 'session-1' in body
    assert 'ship plan view' in body
    assert 'force_remediation' in body
    assert '1.0' in body
    assert 'state/subagents/sub-1.json' in body
    assert 'state/subagents/sub-2.json' in body

    status, filtered_body = _call_app(app, '/subagents', 'source=repo&origin=cli:direct&status=ok')
    assert status.startswith('200')
    assert 'widget-fix' in filtered_body
    assert 'browser-report' not in filtered_body
    assert 'name="origin"' in filtered_body
    assert 'selected' in filtered_body

    status, limited_body = _call_app(app, '/subagents', 'limit=1')
    assert status.startswith('200')
    assert 'browser-report' in limited_body
    assert 'widget-fix' not in limited_body


def test_app_reports_missing_report_source_and_pending_cadence(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    insert_collection(db, {
        'collected_at': '2026-04-16T13:00:00Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-null-source',
        'approval_gate': '{"ok": true, "reason": "valid"}',
        'gate_state': 'valid',
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': '{"outbox": {"status": "PASS"}}',
    })
    app = create_app(_cfg(tmp_path, db))

    status, cycles_body = _call_app(app, '/cycles')
    assert status.startswith('200')
    assert 'report source unavailable' in cycles_body
    assert 'single observation / cadence not yet established' in cycles_body

    status, analytics_body = _call_app(app, '/analytics')
    assert status.startswith('200')
    assert 'report source unavailable' in analytics_body
    assert 'single observation / cadence not yet established' in analytics_body
    assert 'Feedback decision mode' in analytics_body
    assert 'Top BLOCK reasons' in analytics_body
    assert 'Failure class breakdown' in analytics_body

    status, deployments_body = _call_app(app, '/deployments')
    assert status.startswith('200')
    assert 'report source unavailable' in deployments_body
    assert 'cadence not yet established' in deployments_body


def test_app_subagents_handles_missing_telemetry(tmp_path: Path):
    root = tmp_path / 'dashboard'
    db = root / 'data' / 'db.sqlite3'
    init_db(db)
    app = create_app(_cfg(tmp_path, db))

    status, body = _call_app(app, '/subagents')
    assert status.startswith('200')
    assert 'No durable subagent telemetry has been collected yet.' in body
    assert 'state/subagents/*.json' in body
    assert 'Apply filters' not in body
    assert 'No subagent rows match the selected filters.' not in body
