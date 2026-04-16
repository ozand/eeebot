from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DashboardConfig
from .reachability import probe_eeepc_reachability
from .storage import insert_collection, upsert_event


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _safe_json_load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def _latest_json_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    matches = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return matches[0] if matches else None


def _build_ssh_command(cfg: DashboardConfig, remote_command: str) -> list[str]:
    if cfg.eeepc_sudo_password:
        remote_command = f"printf '%s\\n' '{cfg.eeepc_sudo_password}' | sudo -S -p '' {remote_command}"
    return [
        'ssh', '-F', '/home/ozand/.ssh/config', '-i', str(cfg.eeepc_ssh_key), '-o', 'IdentitiesOnly=yes',
        cfg.eeepc_ssh_host,
        remote_command,
    ]


def _truncate_text(value: str | None, limit: int = 240) -> str | None:
    if value is None:
        return None
    compact = ' '.join(str(value).split())
    return compact if len(compact) <= limit else compact[: limit - 1] + '…'


def _collection_error(source: str, stage: str, exc: Exception) -> dict[str, Any]:
    detail: dict[str, Any] = {
        'source': source,
        'stage': stage,
        'message': _truncate_text(str(exc)) or exc.__class__.__name__,
        'error_type': exc.__class__.__name__,
    }
    returncode = getattr(exc, 'returncode', None)
    if returncode is not None:
        detail['returncode'] = returncode
    stderr = _truncate_text(getattr(exc, 'stderr', None))
    stdout = _truncate_text(getattr(exc, 'output', None))
    if stderr:
        detail['stderr'] = stderr
    if stdout and stdout != stderr:
        detail['stdout'] = stdout
    return detail


def _load_ssh_json(cfg: DashboardConfig, remote_path: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    cmd = _build_ssh_command(cfg, f"cat {remote_path}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        return json.loads(proc.stdout), None
    except Exception as exc:
        return None, _collection_error('eeepc', f'ssh:{remote_path}', exc)


def _run_ssh_lines(cfg: DashboardConfig, command: str) -> list[str]:
    cmd = _build_ssh_command(cfg, command)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        return [line for line in proc.stdout.splitlines() if line.strip()]
    except Exception:
        return []

def _normalize_repo_state(repo_root: Path) -> dict[str, Any]:
    workspace = repo_root / 'workspace'
    state_root = workspace / 'state'
    try:
        if not state_root.exists():
            git_head = None
            try:
                proc = subprocess.run(
                    ['git', '-C', str(repo_root), 'rev-parse', '--short', 'HEAD'],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                git_head = proc.stdout.strip() or None
            except Exception:
                git_head = None
            events = []
            if git_head:
                events.append({
                    'event_type': 'deployment',
                    'identity_key': git_head,
                    'title': f'repo HEAD {git_head}',
                    'status': 'present',
                    'detail': {'repo_root': str(repo_root)},
                })
            return {
                'source': 'repo',
                'status': 'unknown',
                'active_goal': None,
                'approval_gate': None,
                'gate_state': None,
                'current_task': None,
                'task_list': [],
                'reward_signal': None,
                'plan_history': [],
                'report_source': None,
                'outbox_source': None,
                'artifact_paths': [],
                'promotion_summary': None,
                'promotion_candidate_path': None,
                'promotion_decision_record': None,
                'promotion_accepted_record': None,
                'events': events,
                'raw': {'repo_root': str(repo_root), 'git_head': git_head},
                'collection_status': 'ok',
                'collection_error': None,
            }
        try:
            from nanobot.runtime.state import load_runtime_state
            runtime = load_runtime_state(workspace)
        except Exception:
            runtime = _load_local_runtime_state(workspace)
        return {
            'source': 'repo',
            'status': runtime.get('runtime_status') or 'unknown',
            'active_goal': runtime.get('active_goal'),
            'approval_gate': json.dumps(runtime.get('approval_gate')) if runtime.get('approval_gate') is not None else None,
            'gate_state': runtime.get('approval_gate_state'),
            'report_source': runtime.get('report_path'),
            'outbox_source': runtime.get('outbox_path'),
            'artifact_paths': runtime.get('artifact_paths') or [],
            'promotion_summary': runtime.get('promotion_summary'),
            'promotion_candidate_path': runtime.get('promotion_candidate_path'),
            'promotion_decision_record': runtime.get('promotion_decision_record'),
            'promotion_accepted_record': runtime.get('promotion_accepted_record'),
            'current_task': runtime.get('current_task'),
            'task_list': runtime.get('task_list') or [],
            'reward_signal': runtime.get('reward_signal'),
            'plan_history': runtime.get('plan_history') or [],
            'events': _repo_events(runtime) + _subagent_events(state_root),
            'raw': runtime,
            'collection_status': 'ok',
            'collection_error': None,
        }
    except Exception as exc:
        return {
            'source': 'repo',
            'status': 'error',
            'active_goal': None,
            'approval_gate': None,
            'gate_state': None,
            'current_task': None,
            'task_list': [],
            'reward_signal': None,
            'plan_history': [],
            'report_source': None,
            'outbox_source': None,
            'artifact_paths': [],
            'promotion_summary': None,
            'promotion_candidate_path': None,
            'promotion_decision_record': None,
            'promotion_accepted_record': None,
            'events': [],
            'raw': {'repo_root': str(repo_root)},
            'collection_status': 'error',
            'collection_error': _collection_error('repo', 'runtime-state', exc),
        }


def _repo_events(runtime: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    if runtime.get('report_path'):
        events.append({
            'event_type': 'cycle',
            'identity_key': runtime.get('report_path'),
            'title': runtime.get('active_goal') or 'unknown goal',
            'status': runtime.get('runtime_status') or 'unknown',
            'detail': {
                'report_source': runtime.get('report_path'),
                'artifact_paths': runtime.get('artifact_paths') or [],
                'promotion_summary': runtime.get('promotion_summary'),
            },
        })
    if runtime.get('promotion_candidate_id'):
        events.append({
            'event_type': 'promotion',
            'identity_key': runtime.get('promotion_candidate_id'),
            'title': runtime.get('promotion_summary') or runtime.get('promotion_candidate_id'),
            'status': runtime.get('decision') or runtime.get('review_status') or 'unknown',
            'detail': {
                'candidate_path': runtime.get('promotion_candidate_path'),
                'decision_record': runtime.get('promotion_decision_record'),
                'accepted_record': runtime.get('promotion_accepted_record'),
            },
        })
    return events


def _load_subagent_telemetry(state_root: Path) -> list[dict[str, Any]]:
    telemetry_dir = state_root / 'subagents'
    if not telemetry_dir.exists():
        return []

    records: dict[str, dict[str, Any]] = {}

    def _consume(record: dict[str, Any], source_path: Path) -> None:
        subagent_id = record.get('subagent_id') or record.get('id') or record.get('task_id')
        if not subagent_id:
            return
        payload = dict(record)
        payload['_source_path'] = str(source_path)
        payload['_source_mtime'] = source_path.stat().st_mtime if source_path.exists() else 0
        records[str(subagent_id)] = payload

    for path in sorted(telemetry_dir.glob('*.json')):
        data = _safe_json_load(path)
        if isinstance(data, dict):
            _consume(data, path)

    for path in sorted(telemetry_dir.glob('*.jsonl')):
        try:
            with path.open('r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if isinstance(data, dict):
                        _consume(data, path)
        except Exception:
            continue

    return sorted(
        records.values(),
        key=lambda item: (
            item.get('finished_at') or '',
            item.get('started_at') or '',
            item.get('_source_mtime') or 0,
            item.get('subagent_id') or '',
        ),
        reverse=True,
    )


def _subagent_events(state_root: Path) -> list[dict[str, Any]]:
    events = []
    for record in _load_subagent_telemetry(state_root):
        subagent_id = record.get('subagent_id') or record.get('id') or record.get('task_id')
        if not subagent_id:
            continue
        title = record.get('label') or record.get('task') or str(subagent_id)
        events.append({
            'event_type': 'subagent',
            'identity_key': str(subagent_id),
            'title': title,
            'status': record.get('status') or 'unknown',
            'detail': {
                'task': record.get('task'),
                'label': record.get('label'),
                'started_at': record.get('started_at'),
                'finished_at': record.get('finished_at'),
                'goal_id': record.get('goal_id'),
                'cycle_id': record.get('cycle_id'),
                'report_path': record.get('report_path'),
                'origin': record.get('origin'),
                'parent_context': record.get('parent_context'),
                'summary': record.get('summary'),
                'result': record.get('result'),
                'workspace': record.get('workspace'),
                'source_path': record.get('_source_path'),
            },
        })
    return events


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _extract_plan_state(*payloads: dict[str, Any] | None) -> dict[str, Any]:
    def _candidate_payloads() -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            nested_plan = payload.get('plan') if isinstance(payload.get('plan'), dict) else None
            task_plan = payload.get('task_plan') if isinstance(payload.get('task_plan'), dict) else None
            if isinstance(nested_plan, dict):
                candidates.append(nested_plan)
            if isinstance(task_plan, dict):
                candidates.append(task_plan)
            candidates.append(payload)
        return candidates

    candidates = _candidate_payloads()

    def _pick(keys: tuple[str, ...]) -> Any:
        for candidate in candidates:
            for key in keys:
                value = candidate.get(key)
                if _has_value(value):
                    return value
        return None

    def _pick_list(keys: tuple[str, ...]) -> list[Any]:
        value = _pick(keys)
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if _has_value(value):
            return [value]
        return []

    return {
        'current_task': _pick(('current_task', 'currentTask', 'task', 'task_name', 'taskName', 'current_task_name', 'currentTaskName')),
        'task_list': _pick_list(('task_list', 'taskList', 'tasks', 'task_queue', 'taskQueue')),
        'reward_signal': _pick(('reward_signal', 'rewardSignal', 'reward', 'reward_score', 'rewardScore')),
        'plan_history': _pick_list(('plan_history', 'planHistory', 'recent_plan_history', 'recentPlanHistory', 'history')),
    }


def _load_local_runtime_state(workspace: Path) -> dict[str, Any]:
    state_root = workspace / 'state'
    reports_dir = state_root / 'reports'
    goals_dir = state_root / 'goals'
    outbox_dir = state_root / 'outbox'
    promotions_dir = state_root / 'promotions'
    plans_dir = state_root / 'plans'

    latest_report = _latest_json_file(reports_dir, 'evolution-*.json') or _latest_json_file(reports_dir, '*.json')
    latest_goal = _latest_json_file(goals_dir, '*.json')
    latest_outbox = _latest_json_file(outbox_dir, 'latest.json') or _latest_json_file(outbox_dir, '*.json')
    latest_promotion = _latest_json_file(promotions_dir, 'latest.json') or _latest_json_file(promotions_dir, '*.json')
    latest_plan = _latest_json_file(plans_dir, 'latest.json') or _latest_json_file(plans_dir, '*.json')

    report_data = _safe_json_load(latest_report)
    goal_data = _safe_json_load(latest_goal)
    outbox_data = _safe_json_load(latest_outbox)
    promotion_data = _safe_json_load(latest_promotion)
    plan_data = _safe_json_load(latest_plan)

    active_goal = None
    if isinstance(goal_data, dict):
        active_goal = (
            goal_data.get('active_goal')
            or goal_data.get('activeGoal')
            or goal_data.get('active_goal_id')
            or goal_data.get('activeGoalId')
            or goal_data.get('goal_id')
            or goal_data.get('goalId')
        )

    approval_gate = None
    gate_state = None
    if isinstance(outbox_data, dict):
        approval_gate = outbox_data.get('approval_gate') or outbox_data.get('approvalGate')
        if approval_gate is None:
            capability_gate = outbox_data.get('capability_gate') if isinstance(outbox_data.get('capability_gate'), dict) else None
            if isinstance(capability_gate, dict):
                approval_gate = capability_gate.get('approval') if isinstance(capability_gate.get('approval'), dict) else None
        if isinstance(approval_gate, dict):
            gate_state = (
                approval_gate.get('state')
                or approval_gate.get('status')
                or approval_gate.get('reason')
                or ('ok' if approval_gate.get('ok') else None)
            )
        elif approval_gate:
            gate_state = str(approval_gate)

    status = None
    if isinstance(report_data, dict):
        result_obj = report_data.get('result') if isinstance(report_data.get('result'), dict) else None
        status = (
            report_data.get('result_status')
            or report_data.get('resultStatus')
            or (result_obj.get('status') if isinstance(result_obj, dict) else None)
        )
        if not active_goal:
            active_goal = report_data.get('goal_id') or report_data.get('goalId')
    if status is None and isinstance(outbox_data, dict):
        status = outbox_data.get('status')

    artifact_paths = []
    if isinstance(report_data, dict):
        follow_through = report_data.get('follow_through') if isinstance(report_data.get('follow_through'), dict) else None
        if isinstance(follow_through, dict):
            artifact_paths = follow_through.get('artifact_paths') or follow_through.get('artifactPaths') or []

    promotion_summary = None
    promotion_candidate_path = None
    promotion_decision_record = None
    promotion_accepted_record = None
    promotion_candidate_id = None
    review_status = None
    decision = None
    decision_reason = None
    if isinstance(promotion_data, dict):
        promotion_candidate_id = promotion_data.get('promotion_candidate_id') or promotion_data.get('promotionCandidateId')
        review_status = promotion_data.get('review_status') or promotion_data.get('reviewStatus')
        decision = promotion_data.get('decision')
        decision_reason = promotion_data.get('decision_reason') or promotion_data.get('decisionReason')
        promotion_candidate_path = promotion_data.get('candidate_path') or promotion_data.get('candidatePath')

    plan_state = _extract_plan_state(plan_data, report_data, goal_data, outbox_data, promotion_data)

    if promotion_candidate_id or review_status or decision:
        promotion_summary = ' | '.join(
            str(value)
            for value in [
                promotion_candidate_id or 'unknown',
                review_status or 'unknown',
                decision or 'unknown',
            ]
        )

    return {
        'runtime_status': status,
        'active_goal': active_goal,
        'approval_gate': approval_gate,
        'approval_gate_state': gate_state,
        'approval_gate_ttl_minutes': None,
        'report_path': str(latest_report) if latest_report else None,
        'goal_path': str(latest_goal) if latest_goal else None,
        'outbox_path': str(latest_outbox) if latest_outbox else None,
        'promotion_summary': promotion_summary,
        'promotion_candidate_path': promotion_candidate_path,
        'promotion_decision_record': promotion_decision_record,
        'promotion_accepted_record': promotion_accepted_record,
        'promotion_candidate_id': promotion_candidate_id,
        'review_status': review_status,
        'decision': decision,
        'decision_reason': decision_reason,
        'artifact_paths': artifact_paths,
        'current_task': plan_state.get('current_task'),
        'task_list': plan_state.get('task_list') or [],
        'reward_signal': plan_state.get('reward_signal'),
        'plan_history': plan_state.get('plan_history') or [],
        'promotion_path': str(latest_promotion) if latest_promotion else None,
        'subagent_rollup': None,
        'raw': {
            'report': report_data,
            'goal': goal_data,
            'outbox': outbox_data,
            'promotion': promotion_data,
            'plan': plan_data,
        },
    }


def _normalize_eeepc_payloads(
    cfg: DashboardConfig,
    outbox: dict[str, Any],
    goals: dict[str, Any],
    reachability: dict[str, Any] | None = None,
    collection_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active_goal = (outbox.get('goal') or {}).get('goal_id') or goals.get('active_goal_id')
    approval = ((outbox.get('capability_gate') or {}).get('approval')) if isinstance(outbox.get('capability_gate'), dict) else None
    artifact_paths = (((outbox.get('goal') or {}).get('follow_through') or {}).get('artifact_paths')) or []
    process_reflection = outbox.get('process_reflection') if isinstance(outbox.get('process_reflection'), dict) else {}
    blocked_next_step = (((outbox.get('goal') or {}).get('follow_through') or {}).get('blocked_next_step')) or None
    events = []
    source_report = outbox.get('source')
    if source_report:
        events.append({
            'event_type': 'cycle',
            'identity_key': source_report,
            'title': active_goal or 'unknown goal',
            'status': outbox.get('status') or 'unknown',
            'detail': {
                'report_source': source_report,
                'artifact_paths': artifact_paths,
                'approval': approval,
                'failure_class': process_reflection.get('failure_class'),
                'blocked_next_step': blocked_next_step,
                'improvement_score': process_reflection.get('improvement_score'),
            },
        })
    return {
        'source': 'eeepc',
        'status': outbox.get('status') or 'unknown',
        'active_goal': active_goal,
        'approval_gate': json.dumps(approval) if approval is not None else None,
        'gate_state': (approval or {}).get('reason') if isinstance(approval, dict) else None,
        'report_source': source_report,
        'outbox_source': f"{cfg.eeepc_state_root}/outbox/report.index.json",
        'artifact_paths': artifact_paths,
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'events': events,
        'reachability': reachability,
        'raw': {'outbox': outbox, 'goals': goals, 'reachability': reachability},
        'collection_status': 'error' if collection_error else 'ok',
        'collection_error': collection_error,
    }



def _normalize_eeepc_state(cfg: DashboardConfig) -> dict[str, Any]:
    state_root = cfg.eeepc_state_root
    reachability = probe_eeepc_reachability(cfg)
    if not reachability.get('reachable'):
        collection_error = {
            'source': 'eeepc',
            'stage': 'reachability',
            'message': reachability.get('error') or 'eeepc SSH probe failed',
            'error_type': 'ReachabilityProbeError',
            'returncode': reachability.get('returncode'),
            'recommended_next_action': reachability.get('recommended_next_action'),
        }
        return {
            'source': 'eeepc',
            'status': 'BLOCK',
            'active_goal': None,
            'approval_gate': None,
            'gate_state': None,
            'current_task': None,
            'task_list': [],
            'reward_signal': None,
            'plan_history': [],
            'report_source': None,
            'outbox_source': f"{cfg.eeepc_state_root}/outbox/report.index.json",
            'artifact_paths': [],
            'promotion_summary': None,
            'promotion_candidate_path': None,
            'promotion_decision_record': None,
            'promotion_accepted_record': None,
            'events': [],
            'reachability': reachability,
            'raw': {'outbox': {}, 'goals': {}, 'reachability': reachability},
            'collection_status': 'error',
            'collection_error': collection_error,
        }
    outbox, outbox_error = _load_ssh_json(cfg, f"{state_root}/outbox/report.index.json")
    goals, goals_error = _load_ssh_json(cfg, f"{state_root}/goals/registry.json")
    collection_error = outbox_error or goals_error
    if collection_error:
        return _normalize_eeepc_payloads(cfg, outbox or {}, goals or {}, reachability, collection_error)
    return _normalize_eeepc_payloads(cfg, outbox or {}, goals or {}, reachability, None)


def _persist(cfg: DashboardConfig, normalized: dict[str, Any]) -> None:
    collected_at = _utc_now()
    insert_collection(cfg.db_path, {
        'collected_at': collected_at,
        'source': normalized['source'],
        'status': normalized.get('status'),
        'active_goal': normalized.get('active_goal'),
        'current_task': normalized.get('current_task'),
        'task_list_json': json.dumps(normalized.get('task_list') or []),
        'reward_signal': json.dumps(normalized.get('reward_signal'), ensure_ascii=False) if isinstance(normalized.get('reward_signal'), (dict, list)) else normalized.get('reward_signal'),
        'plan_history_json': json.dumps(normalized.get('plan_history') or [], ensure_ascii=False),
        'approval_gate': normalized.get('approval_gate'),
        'gate_state': normalized.get('gate_state'),
        'report_source': normalized.get('report_source'),
        'outbox_source': normalized.get('outbox_source'),
        'artifact_paths_json': json.dumps(normalized.get('artifact_paths') or []),
        'promotion_summary': normalized.get('promotion_summary'),
        'promotion_candidate_path': normalized.get('promotion_candidate_path'),
        'promotion_decision_record': normalized.get('promotion_decision_record'),
        'promotion_accepted_record': normalized.get('promotion_accepted_record'),
        'raw_json': json.dumps(normalized.get('raw') or {}, ensure_ascii=False),
    })
    for event in normalized.get('events', []):
        upsert_event(cfg.db_path, {
            'collected_at': collected_at,
            'source': normalized['source'],
            'event_type': event['event_type'],
            'identity_key': event['identity_key'],
            'title': event.get('title'),
            'status': event.get('status'),
            'detail_json': json.dumps(event.get('detail') or {}, ensure_ascii=False),
        })


def collect_once(cfg: DashboardConfig) -> dict[str, Any]:
    repo = _normalize_repo_state(cfg.nanobot_repo_root)
    eeepc = _normalize_eeepc_state(cfg)
    _persist(cfg, repo)
    _persist(cfg, eeepc)
    return {
        'repo_status': repo.get('status'),
        'repo_goal': repo.get('active_goal'),
        'repo_collection_status': repo.get('collection_status'),
        'repo_error': repo.get('collection_error'),
        'eeepc_status': eeepc.get('status'),
        'eeepc_goal': eeepc.get('active_goal'),
        'eeepc_collection_status': eeepc.get('collection_status'),
        'eeepc_error': eeepc.get('collection_error'),
        'eeepc_reachability': eeepc.get('reachability'),
        'collection_status': {
            'repo': repo.get('collection_status'),
            'eeepc': eeepc.get('collection_status'),
        },
    }



def run_poll_loop(cfg: DashboardConfig, iterations: int | None = None) -> None:
    count = 0
    while True:
        collect_once(cfg)
        count += 1
        if iterations is not None and count >= iterations:
            return
        time.sleep(cfg.poll_interval_seconds)
