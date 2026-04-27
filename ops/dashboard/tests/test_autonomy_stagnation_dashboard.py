from __future__ import annotations

import json
import os
import time
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from nanobot_ops_dashboard.app import create_app
from nanobot_ops_dashboard.config import DashboardConfig
from nanobot_ops_dashboard.storage import init_db, insert_collection, upsert_event


def _call_json(app, path: str) -> dict:
    captured = {}
    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = headers
    environ = {}
    setup_testing_defaults(environ)
    environ['PATH_INFO'] = path
    environ['QUERY_STRING'] = ''
    body = b''.join(app(environ, start_response)).decode('utf-8')
    assert captured['status'].startswith('200'), body
    return json.loads(body)


def _seed_pass_cycle(db: Path, idx: int, task_id: str = 'subagent-verify-materialized-improvement') -> None:
    stamp = f'2026-04-24T12:{idx:02d}:00Z'
    raw = {
        'current_plan': {
            'current_task_id': task_id,
            'current_task': task_id,
            'feedback_decision': {'mode': 'handoff_to_next_candidate', 'selected_task_id': task_id},
            'task_selection_source': 'feedback_review_to_execution',
            'selected_tasks': f'{task_id} [task_id={task_id}]',
        },
        'outbox': {'status': 'PASS'},
    }
    insert_collection(db, {
        'collected_at': stamp,
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': f'/workspace/state/reports/evolution-{idx}.json',
        'outbox_source': '/workspace/state/outbox/latest.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps(raw),
    })
    upsert_event(db, {
        'collected_at': stamp,
        'source': 'repo',
        'event_type': 'cycle',
        'identity_key': f'cycle-{idx}',
        'title': task_id,
        'status': 'PASS',
        'detail_json': json.dumps({'current_task_id': task_id}),
    })


def _write_control_plane_summary(project_root: Path, *, material_progress: dict | None = None, task_plan: dict | None = None) -> None:
    summary: dict[str, object] = {}
    if material_progress is not None:
        summary['material_progress'] = material_progress
    if task_plan is not None:
        summary['task_plan'] = task_plan
    path = project_root / 'workspace' / 'state' / 'control_plane' / 'current_summary.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary), encoding='utf-8')


def _seed_hypothesis_backlog(repo_root: Path, *, selected_id: str, selected_title: str, selected_score: int = 100) -> Path:
    backlog = repo_root / 'workspace' / 'state' / 'hypotheses' / 'backlog.json'
    backlog.parent.mkdir(parents=True, exist_ok=True)
    backlog.write_text(json.dumps({
        'schema_version': 'hypotheses-backlog-v1',
        'model': 'HADI',
        'selected_hypothesis_id': selected_id,
        'selected_hypothesis_title': selected_title,
        'selected_hypothesis_score': selected_score,
        'entries': [
            {
                'hypothesis_id': f'hypothesis-{selected_id}',
                'title': selected_title,
                'bounded_priority_score': selected_score,
                'selection_status': 'selected',
                'execution_spec': {
                    'goal': 'goal-bootstrap',
                    'task': selected_title,
                    'acceptance': 'surface current stagnation evidence for the selected hypothesis',
                    'budget': {'requests': 1, 'tool_calls': 2, 'subagents': 0},
                },
                'wsjf': {'score': 24.0},
                'hadi': {
                    'hypothesis': selected_title,
                    'action': 'inspect current stagnation evidence and expose it in the dashboard APIs',
                },
            },
            {
                'hypothesis_id': 'hypothesis-record-reward',
                'title': 'Record cycle reward',
                'bounded_priority_score': 70,
                'selection_status': 'backlog',
                'execution_spec': {
                    'goal': 'goal-bootstrap',
                    'task': 'Record cycle reward',
                    'acceptance': 'complete the current lane and persist the reward evidence',
                    'budget': {'requests': 1, 'tool_calls': 2, 'subagents': 0},
                },
                'wsjf': {'score': 14.0},
                'hadi': {
                    'hypothesis': 'Record cycle reward',
                    'action': 'publish durable evidence for the finished lane',
                },
            },
        ],
    }), encoding='utf-8')
    return backlog


def _seed_selected_hypothesis_cycle(
    db: Path,
    idx: int,
    task_id: str,
    *,
    outcome: str = 'discard',
    summary_only: bool = False,
    repo_root: Path | None = None,
) -> None:
    stamp = f'2026-04-24T13:{idx:02d}:00Z'
    raw = {
        'current_plan': {
            'current_task_id': task_id,
            'current_task': 'Analyze the last failed self-evolution candidate before retrying mutation',
            'selected_tasks': 'Analyze the last failed self-evolution candidate [task_id=analyze-last-failed-candidate]',
            'task_selection_source': 'generated_from_failure_learning',
            'feedback_decision': {
                'mode': 'handoff_to_next_candidate',
                'selected_task_id': task_id,
                'selected_task_title': 'Analyze the last failed self-evolution candidate before retrying mutation',
                'selection_source': 'generated_from_failure_learning',
            },
            'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0, 'elapsed_seconds': 0},
            'experiment': {
                'outcome': outcome,
                'revert_status': 'skipped_no_material_change',
                'revert_required': True,
            },
        },
        'outbox': {'status': 'PASS'},
    }
    report_source = f'/workspace/state/reports/evolution-{idx}.json'
    if summary_only:
        assert repo_root is not None
        report_path = repo_root / 'workspace' / 'state' / 'reports' / f'evolution-{idx}.json'
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({
            'current_plan': {
                'current_task_id': task_id,
                'current_task': 'Analyze the last failed self-evolution candidate before retrying mutation',
                'selected_hypothesis_id': task_id,
                'hypothesis_id': task_id,
                'feedback_decision': {
                    'mode': 'handoff_to_next_candidate',
                    'selected_task_id': task_id,
                    'selected_task_title': 'Analyze the last failed self-evolution candidate before retrying mutation',
                    'selection_source': 'generated_from_failure_learning',
                },
                'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0, 'elapsed_seconds': 0},
                'experiment': {
                    'outcome': outcome,
                    'revert_status': 'skipped_no_material_change',
                    'revert_required': True,
                },
            },
            'experiment': {
                'outcome': outcome,
                'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0, 'elapsed_seconds': 0},
            },
            'feedback_decision': {
                'mode': 'handoff_to_next_candidate',
                'selected_task_id': task_id,
                'selected_task_title': 'Analyze the last failed self-evolution candidate before retrying mutation',
                'selection_source': 'generated_from_failure_learning',
            },
        }), encoding='utf-8')
    insert_collection(db, {
        'collected_at': stamp,
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': report_source,
        'outbox_source': '/workspace/state/outbox/latest.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps(raw),
    })
    detail: dict[str, object]
    if summary_only:
        detail = {
            'report_source': report_source,
            'artifact_paths': [f'/workspace/state/reports/evolution-{idx}.json'],
        }
    else:
        detail = {
            'current_task_id': task_id,
            'selected_hypothesis_id': task_id,
            'outcome': outcome,
            'budget_used': {'requests': 1, 'tool_calls': 2, 'subagents': 0, 'elapsed_seconds': 0},
        }
    upsert_event(db, {
        'collected_at': stamp,
        'source': 'repo',
        'event_type': 'cycle',
        'identity_key': f'cycle-stagnant-{idx}',
        'title': 'summary-only cycle' if summary_only else task_id,
        'status': 'PASS',
        'detail_json': json.dumps(detail),
    })


def test_api_subagents_returns_json_with_stale_queued_request(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    req_dir = state_root / 'subagents' / 'requests'
    req_dir.mkdir(parents=True)
    req = req_dir / 'request-cycle-old.json'
    req.write_text(json.dumps({'schema_version': 'subagent-request-v1', 'request_status': 'queued', 'task_id': 'subagent-verify-materialized-improvement'}), encoding='utf-8')
    old = time.time() - 3 * 3600
    os.utime(req, (old, old))
    upsert_event(db, {
        'collected_at': '2026-04-24T12:00:00Z',
        'source': 'repo',
        'event_type': 'subagent',
        'identity_key': 'subagent-old',
        'title': 'subagent verify',
        'status': 'queued',
        'detail_json': json.dumps({'task_id': 'subagent-verify-materialized-improvement'}),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    payload = _call_json(create_app(cfg), '/api/subagents')

    assert payload['summary']['total_events'] == 1
    assert payload['summary']['stale_request_count'] == 1
    assert payload['requests'][0]['status'] == 'stale'
    assert payload['requests'][0]['task_id'] == 'subagent-verify-materialized-improvement'


def test_dashboard_autonomy_verdict_flags_pass_but_stagnant(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True)
    (state_root / 'credits').mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    for i in range(10):
        _seed_pass_cycle(db, i)
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({'outcome': 'discard', 'revert_status': 'skipped_no_material_change'}), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({'delta': 0.0, 'reward_gate': {'status': 'suppressed'}}), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({'status': 'terminal_noop'}), encoding='utf-8')
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    analytics = _call_json(app, '/api/analytics')['analytics']
    system = _call_json(app, '/api/system')

    assert analytics['current_streak']['status'] == 'PASS'
    assert analytics['autonomy_verdict']['state'] == 'stagnant'
    assert 'same_task_streak' in analytics['autonomy_verdict']['reasons']
    assert 'discarded_experiment' in analytics['autonomy_verdict']['reasons']
    assert system['autonomy_verdict']['state'] == 'stagnant'


def test_dashboard_api_surfaces_shared_autonomy_verdict_and_material_progress(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True)
    (state_root / 'credits').mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    for i in range(10):
        _seed_pass_cycle(db, i)
    _write_control_plane_summary(
        project_root,
        material_progress={
            'schema_version': 'material-progress-v1',
            'state': 'proven',
            'available': True,
            'healthy_autonomy_allowed': True,
            'proof_count': 1,
            'proofs': ['workspace/state/material/proof.md'],
            'qualifying_proofs': ['workspace/state/material/proof.md'],
            'blocking_reason': None,
        },
        task_plan={
            'current_task_id': 'subagent-verify-materialized-improvement',
            'current_task': 'subagent-verify-materialized-improvement',
        },
    )
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({'outcome': 'discard', 'revert_status': 'skipped_no_material_change'}), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({'delta': 0.0, 'reward_gate': {'status': 'suppressed'}}), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({'status': 'terminal_noop'}), encoding='utf-8')
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    analytics = _call_json(app, '/api/analytics')['analytics']
    system = _call_json(app, '/api/system')
    plan = _call_json(app, '/api/plan')
    experiments = _call_json(app, '/api/experiments')

    assert analytics['autonomy_verdict']['state'] == 'stagnant'
    assert system['autonomy_verdict'] == analytics['autonomy_verdict']
    assert system['control_plane']['autonomy_verdict'] == analytics['autonomy_verdict']
    assert system['control_plane']['material_progress']['state'] == 'proven'
    assert system['material_progress']['state'] == 'proven'
    assert plan['material_progress']['state'] == 'proven'
    assert plan['material_progress']['available'] is True
    assert experiments['material_progress']['state'] == 'proven'
    assert experiments['material_progress']['available'] is True


def test_dashboard_api_surfaces_selected_hypothesis_diagnostics_and_hypothesis_dynamics(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True)
    (state_root / 'credits').mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    (state_root / 'control_plane').mkdir(parents=True)
    _seed_hypothesis_backlog(
        repo_root,
        selected_id='analyze-last-failed-candidate',
        selected_title='Analyze the last failed self-evolution candidate before retrying mutation',
        selected_score=100,
    )
    _write_control_plane_summary(
        project_root,
        task_plan={
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task': 'Analyze the last failed self-evolution candidate before retrying mutation',
        },
    )
    for idx in range(5):
        _seed_selected_hypothesis_cycle(db, idx, 'analyze-last-failed-candidate')
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({
        'outcome': 'discard',
        'revert_required': True,
        'revert_status': 'skipped_no_material_change',
    }), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({
        'delta': 0.0,
        'reward_gate': {
            'status': 'suppressed',
            'reason': 'discarded_experiment_unresolved_revert',
        },
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({
        'status': 'terminal_noop',
        'selfevo_issue': {
            'number': 61,
            'url': 'https://github.com/ozand/eeebot-self-evolving/issues/61',
            'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
        },
        'pr': {
            'number': 62,
            'url': 'https://github.com/ozand/eeebot-self-evolving/pull/62',
            'title': 'Terminal self-evolution lane closure',
        },
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'current_state.json').write_text(json.dumps({
        'selfevo_issue': {
            'number': 61,
            'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
            'url': 'https://github.com/ozand/eeebot-self-evolving/issues/61',
        },
        'last_pr': {
            'number': 62,
            'title': 'Terminal self-evolution lane closure',
            'url': 'https://github.com/ozand/eeebot-self-evolving/pull/62',
        },
    }), encoding='utf-8')
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    hypotheses = _call_json(app, '/api/hypotheses')
    system = _call_json(app, '/api/system')

    diagnostics = hypotheses['selected_hypothesis_diagnostics']
    assert diagnostics['selected_hypothesis_id'] == 'analyze-last-failed-candidate'
    assert diagnostics['run_count'] == 5
    assert diagnostics['run_streak'] == 5
    assert diagnostics['last_24h']['total_runs'] == 5
    assert diagnostics['last_24h']['discard_count'] == 5
    assert diagnostics['last_24h']['budget_used_sum']['requests'] == 5
    assert diagnostics['last_24h']['budget_used_sum']['tool_calls'] == 10
    assert diagnostics['last_24h']['reward_gate']['status'] == 'suppressed'
    assert diagnostics['last_24h']['reward_gate']['reason'] == 'discarded_experiment_unresolved_revert'
    assert diagnostics['terminal_selfevo_issue']['number'] == 61
    assert diagnostics['terminal_selfevo_pr']['number'] == 62

    hypothesis_dynamics = system['hypothesis_dynamics']
    assert hypothesis_dynamics['state'] == 'stagnant'
    assert hypothesis_dynamics['selected_hypothesis_id'] == 'analyze-last-failed-candidate'
    assert hypothesis_dynamics['run_count'] == 5
    assert hypothesis_dynamics['run_streak'] == 5
    assert hypothesis_dynamics['last_24h']['discard_count'] == 5
    assert hypothesis_dynamics['last_24h']['reward_gate']['reason'] == 'discarded_experiment_unresolved_revert'
    assert hypothesis_dynamics['terminal_selfevo_issue']['number'] == 61
    assert hypothesis_dynamics['terminal_selfevo_pr']['number'] == 62
    assert 'hypothesis_dynamics_stagnant' in system['autonomy_verdict']['reasons']
    assert system['autonomy_verdict']['state'] == 'stagnant'


def test_dashboard_api_marks_selected_hypothesis_stagnant_when_non_selected_cycle_interrupts_streak(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True)
    (state_root / 'credits').mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    (state_root / 'control_plane').mkdir(parents=True)
    _seed_hypothesis_backlog(
        repo_root,
        selected_id='analyze-last-failed-candidate',
        selected_title='Analyze the last failed self-evolution candidate before retrying mutation',
        selected_score=100,
    )
    _write_control_plane_summary(
        project_root,
        task_plan={
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task': 'Analyze the last failed self-evolution candidate before retrying mutation',
        },
    )
    for idx in range(5):
        _seed_selected_hypothesis_cycle(db, idx, 'analyze-last-failed-candidate')
    interrupt_stamp = '2026-04-24T14:00:00Z'
    insert_collection(db, {
        'collected_at': interrupt_stamp,
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': '/workspace/state/reports/evolution-interrupt.json',
        'outbox_source': '/workspace/state/outbox/latest.json',
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({
            'current_plan': {
                'current_task_id': 'newer-unrelated-cycle',
                'current_task': 'Close out an unrelated maintenance pass',
                'feedback_decision': {
                    'mode': 'handoff_to_next_candidate',
                    'selected_task_id': 'newer-unrelated-cycle',
                },
                'task_selection_source': 'feedback_review_to_execution',
                'selected_tasks': 'Close out an unrelated maintenance pass [task_id=newer-unrelated-cycle]',
            },
            'outbox': {'status': 'PASS'},
        }),
    })
    upsert_event(db, {
        'collected_at': interrupt_stamp,
        'source': 'repo',
        'event_type': 'cycle',
        'identity_key': 'cycle-interrupting-non-selected',
        'title': 'newer-unrelated-cycle',
        'status': 'PASS',
        'detail_json': json.dumps({'current_task_id': 'newer-unrelated-cycle'}),
    })
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({
        'outcome': 'discard',
        'revert_required': True,
        'revert_status': 'skipped_no_material_change',
    }), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({
        'delta': 0.0,
        'reward_gate': {
            'status': 'suppressed',
            'reason': 'discarded_experiment_unresolved_revert',
        },
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({
        'status': 'terminal_noop',
        'selfevo_issue': {
            'number': 61,
            'url': 'https://github.com/ozand/eeebot-self-evolving/issues/61',
            'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
        },
        'pr': {
            'number': 62,
            'url': 'https://github.com/ozand/eeebot-self-evolving/pull/62',
            'title': 'Terminal self-evolution lane closure',
        },
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'current_state.json').write_text(json.dumps({
        'selfevo_issue': {
            'number': 61,
            'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
            'url': 'https://github.com/ozand/eeebot-self-evolving/issues/61',
        },
        'last_pr': {
            'number': 62,
            'title': 'Terminal self-evolution lane closure',
            'url': 'https://github.com/ozand/eeebot-self-evolving/pull/62',
        },
    }), encoding='utf-8')
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    hypotheses = _call_json(app, '/api/hypotheses')
    system = _call_json(app, '/api/system')

    diagnostics = hypotheses['selected_hypothesis_diagnostics']
    assert diagnostics['selected_hypothesis_id'] == 'analyze-last-failed-candidate'
    assert diagnostics['run_count'] == 5
    assert diagnostics['run_streak'] == 0
    assert diagnostics['state'] == 'stagnant'
    assert 'selected_hypothesis_repetition' in diagnostics['reasons']
    assert 'discard_only_selected_hypothesis' in diagnostics['reasons']
    assert diagnostics['last_24h']['discard_count'] == 5
    assert diagnostics['last_24h']['reward_gate']['status'] == 'suppressed'
    assert diagnostics['terminal_selfevo_issue']['number'] == 61
    assert diagnostics['terminal_selfevo_pr']['number'] == 62

    hypothesis_dynamics = system['hypothesis_dynamics']
    assert hypothesis_dynamics['state'] == 'stagnant'
    assert hypothesis_dynamics['selected_hypothesis_id'] == 'analyze-last-failed-candidate'
    assert hypothesis_dynamics['run_count'] == 5
    assert hypothesis_dynamics['run_streak'] == 0
    assert hypothesis_dynamics['last_24h']['discard_count'] == 5
    assert hypothesis_dynamics['last_24h']['reward_gate']['reason'] == 'discarded_experiment_unresolved_revert'
    assert hypothesis_dynamics['terminal_selfevo_issue']['number'] == 61
    assert hypothesis_dynamics['terminal_selfevo_pr']['number'] == 62
    assert 'hypothesis_dynamics_stagnant' in system['autonomy_verdict']['reasons']
    assert system['autonomy_verdict']['state'] == 'stagnant'


def test_dashboard_api_hydrates_summary_only_cycle_detail_from_report_source(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True)
    (state_root / 'credits').mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    (state_root / 'control_plane').mkdir(parents=True)
    _seed_hypothesis_backlog(
        repo_root,
        selected_id='analyze-last-failed-candidate',
        selected_title='Analyze the last failed self-evolution candidate before retrying mutation',
        selected_score=100,
    )
    _write_control_plane_summary(
        project_root,
        task_plan={
            'current_task_id': 'analyze-last-failed-candidate',
            'current_task': 'Analyze the last failed self-evolution candidate before retrying mutation',
        },
    )
    for idx in range(5):
        _seed_selected_hypothesis_cycle(db, idx, 'analyze-last-failed-candidate', summary_only=True, repo_root=repo_root)
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({
        'outcome': 'discard',
        'revert_required': True,
        'revert_status': 'skipped_no_material_change',
    }), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({
        'delta': 0.0,
        'reward_gate': {
            'status': 'suppressed',
            'reason': 'discarded_experiment_unresolved_revert',
        },
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({
        'status': 'terminal_noop',
        'selfevo_issue': {
            'number': 61,
            'url': 'https://github.com/ozand/eeebot-self-evolving/issues/61',
            'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
        },
        'pr': {
            'number': 62,
            'url': 'https://github.com/ozand/eeebot-self-evolving/pull/62',
            'title': 'Terminal self-evolution lane closure',
        },
    }), encoding='utf-8')
    (state_root / 'self_evolution' / 'current_state.json').write_text(json.dumps({
        'selfevo_issue': {
            'number': 61,
            'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
            'url': 'https://github.com/ozand/eeebot-self-evolving/issues/61',
        },
        'last_pr': {
            'number': 62,
            'title': 'Terminal self-evolution lane closure',
            'url': 'https://github.com/ozand/eeebot-self-evolving/pull/62',
        },
    }), encoding='utf-8')
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    hypotheses = _call_json(app, '/api/hypotheses')
    system = _call_json(app, '/api/system')

    diagnostics = hypotheses['selected_hypothesis_diagnostics']
    assert diagnostics['selected_hypothesis_id'] == 'analyze-last-failed-candidate'
    assert diagnostics['run_count'] == 5
    assert diagnostics['run_streak'] == 5
    assert diagnostics['selected_hypothesis_experiment_outcome'] == 'discard'
    assert diagnostics['selected_hypothesis_feedback_decision']['mode'] == 'handoff_to_next_candidate'
    assert diagnostics['last_24h']['total_runs'] == 5
    assert diagnostics['last_24h']['discard_count'] == 5
    assert diagnostics['last_24h']['budget_used_sum']['requests'] == 5
    assert diagnostics['last_24h']['budget_used_sum']['tool_calls'] == 10
    assert diagnostics['last_24h']['reward_gate']['status'] == 'suppressed'
    assert diagnostics['last_24h']['reward_gate']['reason'] == 'discarded_experiment_unresolved_revert'
    assert diagnostics['terminal_selfevo_issue']['number'] == 61
    assert diagnostics['terminal_selfevo_pr']['number'] == 62

    hypothesis_dynamics = system['hypothesis_dynamics']
    assert hypothesis_dynamics['state'] == 'stagnant'
    assert hypothesis_dynamics['selected_hypothesis_id'] == 'analyze-last-failed-candidate'
    assert hypothesis_dynamics['run_count'] == 5
    assert hypothesis_dynamics['run_streak'] == 5
    assert hypothesis_dynamics['last_24h']['discard_count'] == 5
    assert hypothesis_dynamics['last_24h']['reward_gate']['reason'] == 'discarded_experiment_unresolved_revert'
    assert hypothesis_dynamics['terminal_selfevo_issue']['number'] == 61
    assert hypothesis_dynamics['terminal_selfevo_pr']['number'] == 62
    assert 'hypothesis_dynamics_stagnant' in system['autonomy_verdict']['reasons']
    assert system['autonomy_verdict']['state'] == 'stagnant'


def test_dashboard_api_surfaces_unavailable_material_progress_when_missing(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    system = _call_json(app, '/api/system')
    plan = _call_json(app, '/api/plan')
    experiments = _call_json(app, '/api/experiments')

    for payload in (system, plan, experiments):
        material_progress = payload['material_progress']
        assert material_progress['state'] == 'unavailable'
        assert material_progress['available'] is False
        assert material_progress['reason'] == 'material_progress_unavailable'


def test_system_api_reports_legacy_reward_loop_parity(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    insert_collection(db, {
        'collected_at': '2026-04-24T12:00:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({'current_plan': {'current_task_id': 'subagent-verify-materialized-improvement', 'feedback_decision': {'mode': 'handoff_to_next_candidate'}}}),
    })
    insert_collection(db, {
        'collected_at': '2026-04-24T12:00:01Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({'current_plan': {'selected_tasks': 'Record cycle reward [task_id=record-reward]', 'task_selection_source': 'recorded_current_task', 'feedback_decision': None}}),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    system = _call_json(create_app(cfg), '/api/system')

    assert system['runtime_parity']['state'] == 'legacy_reward_loop'
    assert 'live_feedback_decision_missing' in system['runtime_parity']['reasons']
    assert 'current_task_drift' not in system['runtime_parity']['reasons']
    assert 'legacy_live_reward_loop_current_task' in system['runtime_parity']['reasons']
    assert 'live_hadi_artifacts_missing' in system['runtime_parity']['reasons']
    assert system['runtime_parity']['local_current_task_id'] == 'subagent-verify-materialized-improvement'
    assert system['runtime_parity']['live_current_task_id'] == 'record-reward'
    assert system['runtime_parity']['canonical_current_task_id'] == 'subagent-verify-materialized-improvement'


def test_api_subagents_materializes_terminal_telemetry_for_queued_request(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    req_dir = state_root / 'subagents' / 'requests'
    req_dir.mkdir(parents=True)
    req = req_dir / 'request-old.json'
    req.write_text(json.dumps({'request_status': 'queued', 'task_id': 'inspect-pass-streak'}), encoding='utf-8')
    old = time.time() - 3 * 3600
    os.utime(req, (old, old))
    terminal_result = state_root / 'subagents' / 'terminal-result.json'
    terminal_result.write_text(json.dumps({'status': 'done', 'task_id': 'inspect-pass-streak', 'summary': 'bounded review completed'}), encoding='utf-8')
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    payload = _call_json(create_app(cfg), '/api/subagents')

    assert payload['summary']['state'] == 'completed'
    assert payload['summary']['stale_request_count'] == 0
    assert payload['summary']['result_count'] == 1
    assert payload['subagent_rollup']['latest_request']['materialized_result_path'].endswith('terminal-result.json')


def test_runtime_parity_is_shared_by_system_control_plane_and_analytics(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    insert_collection(db, {
        'collected_at': '2026-04-24T12:00:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({'current_plan': {'current_task_id': 'inspect-pass-streak', 'feedback_decision': {'mode': 'continue_active_lane'}}}),
    })
    insert_collection(db, {
        'collected_at': '2026-04-24T12:00:01Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({'current_plan': {'selected_tasks': 'Record cycle reward [task_id=record-reward]', 'task_selection_source': 'recorded_current_task', 'feedback_decision': None}}),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')
    app = create_app(cfg)

    system = _call_json(app, '/api/system')
    analytics = _call_json(app, '/api/analytics')['analytics']

    assert system['runtime_parity']['state'] == 'legacy_reward_loop'
    assert system['control_plane']['runtime_parity'] == system['runtime_parity']
    assert analytics['runtime_parity'] == system['runtime_parity']
    assert analytics['autonomy_verdict']['state'] == 'stagnant'
    assert 'runtime_parity_blocked' in analytics['autonomy_verdict']['reasons']

def test_autonomy_verdict_treats_stale_blockers_as_historical_after_material_progress_and_aligned_runtime(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True)
    (state_root / 'credits').mkdir(parents=True)
    (state_root / 'hypotheses').mkdir(parents=True)
    (state_root / 'control_plane').mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    (state_root / 'hypotheses' / 'backlog.json').write_text(json.dumps([]), encoding='utf-8')
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({'task_plan': {'current_task_id': 'record-reward'}}), encoding='utf-8')
    (state_root / 'self_evolution' / 'current_state.json').write_text(json.dumps({'state': 'running'}), encoding='utf-8')
    for i in range(10):
        _seed_pass_cycle(db, i)
    _write_control_plane_summary(
        project_root,
        material_progress={
            'schema_version': 'material-progress-v1',
            'state': 'proven',
            'available': True,
            'healthy_autonomy_allowed': True,
            'proof_count': 3,
            'proofs': ['merged_selfevo_pr_closure', 'consumed_subagent_result', 'promotion_or_evidence_artifact'],
            'qualifying_proofs': ['merged_selfevo_pr_closure', 'consumed_subagent_result', 'promotion_or_evidence_artifact'],
            'blocking_reason': None,
        },
        task_plan={
            'current_task_id': 'record-reward',
            'current_task': 'record-reward',
        },
    )
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({'outcome': 'discard', 'revert_status': 'skipped_no_material_change'}), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({'delta': 0.0, 'reward_gate': {'status': 'suppressed'}}), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({'status': 'terminal_noop'}), encoding='utf-8')
    for source in ('repo', 'eeepc'):
        feedback_decision = {'mode': 'continue_active_lane'} if source == 'repo' else None
        insert_collection(db, {
            'collected_at': '2026-04-24T12:59:00Z',
            'source': source,
            'status': 'PASS',
            'active_goal': 'goal-bootstrap',
            'approval_gate': None,
            'gate_state': None,
            'report_source': None,
            'outbox_source': None,
            'artifact_paths_json': '[]',
            'promotion_summary': None,
            'promotion_candidate_path': None,
            'promotion_decision_record': None,
            'promotion_accepted_record': None,
            'raw_json': json.dumps({'current_plan': {'current_task_id': 'record-reward', 'selected_tasks': 'Record cycle reward [task_id=record-reward]', 'task_selection_source': 'recorded_current_task', 'feedback_decision': feedback_decision}}),
        })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    verdict = _call_json(create_app(cfg), '/api/system')['autonomy_verdict']

    assert verdict['state'] == 'healthy_progress'
    assert verdict['reasons'] == []
    assert 'same_task_streak' in verdict['historical_reasons']
    assert 'discarded_experiment' in verdict['historical_reasons']
    assert 'suppressed_reward' in verdict['historical_reasons']
    assert 'terminal_noop' in verdict['historical_reasons']
    assert 'runtime_parity_blocked' in verdict['historical_reasons']

def test_autonomy_verdict_keeps_runtime_parity_blocking_when_live_hadi_artifacts_missing_after_material_progress(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True)
    (state_root / 'credits').mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    for i in range(10):
        _seed_pass_cycle(db, i, task_id='record-reward')
    _write_control_plane_summary(
        project_root,
        material_progress={
            'schema_version': 'material-progress-v1',
            'state': 'proven',
            'available': True,
            'healthy_autonomy_allowed': True,
            'proof_count': 1,
            'proofs': ['merged_selfevo_pr_closure'],
            'qualifying_proofs': ['merged_selfevo_pr_closure'],
            'blocking_reason': None,
        },
        task_plan={'current_task_id': 'record-reward', 'current_task': 'record-reward'},
    )
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({'outcome': 'accepted'}), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({'delta': 1.0, 'reward_gate': {'status': 'accepted'}}), encoding='utf-8')
    for source in ('repo', 'eeepc'):
        insert_collection(db, {
            'collected_at': '2026-04-24T12:59:00Z',
            'source': source,
            'status': 'PASS',
            'active_goal': 'goal-bootstrap',
            'approval_gate': None,
            'gate_state': None,
            'report_source': None,
            'outbox_source': None,
            'artifact_paths_json': '[]',
            'promotion_summary': None,
            'promotion_candidate_path': None,
            'promotion_decision_record': None,
            'promotion_accepted_record': None,
            'raw_json': json.dumps({'current_plan': {'current_task_id': 'record-reward', 'current_task': 'record-reward', 'feedback_decision': {'mode': 'continue_active_lane'}}}),
        })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    system = _call_json(create_app(cfg), '/api/system')

    assert 'live_hadi_artifacts_missing' in system['runtime_parity']['reasons']
    assert system['autonomy_verdict']['state'] == 'stagnant'
    assert 'runtime_parity_blocked' in system['autonomy_verdict']['reasons']
    assert 'runtime_parity_blocked' not in system['autonomy_verdict']['historical_reasons']

def test_autonomy_verdict_downgrades_classified_legacy_reward_loop_after_canonical_task_authority(tmp_path: Path) -> None:
    project_root = tmp_path / 'dashboard'
    repo_root = tmp_path / 'nanobot'
    db = tmp_path / 'dashboard.sqlite3'
    init_db(db)
    state_root = repo_root / 'workspace' / 'state'
    (state_root / 'experiments').mkdir(parents=True)
    (state_root / 'credits').mkdir(parents=True)
    (state_root / 'hypotheses').mkdir(parents=True)
    (state_root / 'control_plane').mkdir(parents=True)
    (state_root / 'self_evolution' / 'runtime').mkdir(parents=True)
    (state_root / 'hypotheses' / 'backlog.json').write_text(json.dumps([]), encoding='utf-8')
    (state_root / 'control_plane' / 'current_summary.json').write_text(json.dumps({'task_plan': {'current_task_id': 'analyze-last-failed-candidate'}}), encoding='utf-8')
    (state_root / 'self_evolution' / 'current_state.json').write_text(json.dumps({'state': 'running'}), encoding='utf-8')
    for i in range(10):
        _seed_pass_cycle(db, i, task_id='analyze-last-failed-candidate')
    _write_control_plane_summary(
        project_root,
        material_progress={
            'schema_version': 'material-progress-v1',
            'state': 'proven',
            'available': True,
            'healthy_autonomy_allowed': True,
            'proof_count': 3,
            'proofs': ['merged_selfevo_pr_closure', 'consumed_subagent_result', 'promotion_or_evidence_artifact'],
            'qualifying_proofs': ['merged_selfevo_pr_closure', 'consumed_subagent_result', 'promotion_or_evidence_artifact'],
            'blocking_reason': None,
        },
        task_plan={'current_task_id': 'analyze-last-failed-candidate', 'current_task': 'Analyze the last failed self-evolution candidate'},
    )
    (state_root / 'experiments' / 'latest.json').write_text(json.dumps({'outcome': 'discard', 'revert_status': 'skipped_no_material_change'}), encoding='utf-8')
    (state_root / 'credits' / 'latest.json').write_text(json.dumps({'delta': 0.0, 'reward_gate': {'status': 'suppressed'}}), encoding='utf-8')
    (state_root / 'self_evolution' / 'runtime' / 'latest_noop.json').write_text(json.dumps({'status': 'terminal_noop'}), encoding='utf-8')
    insert_collection(db, {
        'collected_at': '2026-04-24T12:59:00Z',
        'source': 'repo',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({'current_plan': {'current_task_id': 'analyze-last-failed-candidate', 'current_task': 'Analyze the last failed self-evolution candidate', 'feedback_decision': {'mode': 'handoff_to_next_candidate'}}}),
    })
    insert_collection(db, {
        'collected_at': '2026-04-24T12:59:01Z',
        'source': 'eeepc',
        'status': 'PASS',
        'active_goal': 'goal-bootstrap',
        'approval_gate': None,
        'gate_state': None,
        'report_source': None,
        'outbox_source': None,
        'artifact_paths_json': '[]',
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'raw_json': json.dumps({'current_plan': {'current_task_id': 'record-reward', 'selected_tasks': 'Record cycle reward [task_id=record-reward]', 'task_selection_source': 'recorded_current_task', 'feedback_decision': None}}),
    })
    cfg = DashboardConfig(project_root=project_root, nanobot_repo_root=repo_root, db_path=db, eeepc_ssh_host='eeepc', eeepc_ssh_key=tmp_path / 'missing-key', eeepc_state_root='/state')

    system = _call_json(create_app(cfg), '/api/system')

    assert system['runtime_parity']['canonical_current_task_id'] == 'analyze-last-failed-candidate'
    assert system['runtime_parity']['reasons'] == ['live_feedback_decision_missing', 'legacy_live_reward_loop_current_task']
    assert system['autonomy_verdict']['state'] == 'healthy_progress'
    assert system['autonomy_verdict']['reasons'] == []
    assert 'runtime_parity_blocked' in system['autonomy_verdict']['historical_reasons']
