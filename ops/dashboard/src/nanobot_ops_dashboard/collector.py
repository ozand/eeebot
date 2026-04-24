from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from .config import DashboardConfig
from .reachability import probe_eeepc_reachability
from .storage import insert_collection, upsert_event
from nanobot.runtime.state import _subagent_rollup_snapshot


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _safe_json_load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def _json_loads_any(value: str | None):
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def _latest_json_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    matches = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return matches[0] if matches else None


def _load_hypothesis_backlog_snapshot(state_root: Path) -> dict[str, Any] | None:
    hypotheses_dir = state_root / 'hypotheses'
    if not hypotheses_dir.exists():
        return None
    backlog_path = hypotheses_dir / 'backlog.json'
    candidate_paths = [backlog_path] if backlog_path.exists() else []
    seen: set[str] = set()
    for path in candidate_paths:
        if str(path) in seen:
            continue
        seen.add(str(path))
        payload = _safe_json_load(path)
        if not isinstance(payload, dict):
            continue
        entries = payload.get('entries') if isinstance(payload.get('entries'), list) else payload.get('backlog') if isinstance(payload.get('backlog'), list) else payload.get('items') if isinstance(payload.get('items'), list) else []
        selected_id = payload.get('selected_hypothesis_id') or payload.get('selectedHypothesisId')
        selected_title = payload.get('selected_hypothesis_title') or payload.get('selectedHypothesisTitle')
        selected_status = payload.get('selected_hypothesis_status') or payload.get('selectedHypothesisStatus') or payload.get('selection_status') or payload.get('selectionStatus')
        selected_score = payload.get('selected_hypothesis_score') or payload.get('selectedHypothesisScore')
        return {
            'path': str(path),
            'schema_version': payload.get('schema_version') or payload.get('schemaVersion'),
            'entry_count': len(entries),
            'selected_hypothesis_id': selected_id,
            'selected_hypothesis_title': selected_title,
            'selected_hypothesis_status': selected_status,
            'selected_hypothesis_score': selected_score,
            'entries': entries,
            'raw': payload,
        }
    return None


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

def _normalize_repo_state(repo_root: Path, max_subagent_records: int = 200) -> dict[str, Any]:
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
        hypothesis_backlog = _load_hypothesis_backlog_snapshot(state_root)
        raw = dict(runtime)
        if hypothesis_backlog is not None:
            raw['hypothesis_backlog'] = hypothesis_backlog
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
            'events': _repo_events(runtime) + _subagent_events(state_root, max_records=max_subagent_records),
            'raw': raw,
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
                'artifact_path': runtime.get('promotion_artifact_path'),
                'readiness_checks': runtime.get('promotion_readiness_checks'),
                'readiness_reasons': runtime.get('promotion_readiness_reasons'),
                'governance_packet': runtime.get('promotion_governance_packet'),
            },
        })
    return events


def _load_subagent_telemetry(state_root: Path, max_records: int = 200) -> list[dict[str, Any]]:
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
    )[:max_records]


def _subagent_events_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for record in records:
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
                'current_task_id': record.get('current_task_id'),
                'task_reward_signal': record.get('task_reward_signal'),
                'task_feedback_decision': record.get('task_feedback_decision'),
                'origin': record.get('origin'),
                'parent_context': record.get('parent_context'),
                'summary': record.get('summary'),
                'result': record.get('result'),
                'workspace': record.get('workspace'),
                'source_path': record.get('_source_path'),
            },
        })
    return events


def _subagent_events(state_root: Path, max_records: int = 200) -> list[dict[str, Any]]:
    return _subagent_events_from_records(_load_subagent_telemetry(state_root, max_records=max_records))


def _load_ssh_subagent_telemetry(cfg: DashboardConfig, state_root: str) -> list[dict[str, Any]]:
    limit = max(0, int(cfg.max_subagent_records))
    script = f"""
import json
from pathlib import Path
limit = {limit!r}
root = Path({state_root!r}) / 'subagents'
if root.exists() and limit != 0:
    files = []
    for pattern in ('*.json', '*.jsonl'):
        files.extend(root.glob(pattern))
    emitted = 0
    for path in sorted(set(files), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if emitted >= limit:
            break
        try:
            if path.suffix == '.jsonl':
                with path.open('r', encoding='utf-8') as fh:
                    lines = [line.strip() for line in fh if line.strip()]
                for line in reversed(lines):
                    if emitted >= limit:
                        break
                    data = json.loads(line)
                    if isinstance(data, dict):
                        data['_source_path'] = str(path)
                        data['_source_mtime'] = path.stat().st_mtime if path.exists() else 0
                        print(json.dumps(data, ensure_ascii=False))
                        emitted += 1
            else:
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    data['_source_path'] = str(path)
                    data['_source_mtime'] = path.stat().st_mtime if path.exists() else 0
                    print(json.dumps(data, ensure_ascii=False))
                    emitted += 1
        except Exception:
            continue
"""
    records: list[dict[str, Any]] = []
    for line in _run_ssh_lines(cfg, f"python3 -c {shlex.quote(script)}"):
        try:
            data = json.loads(line)
        except Exception:
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


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
        'current_task': _pick(('current_task', 'currentTask', 'task', 'task_name', 'taskName', 'current_task_name', 'currentTaskName', 'current_task_id', 'currentTaskId')),
        'task_list': _pick_list(('task_list', 'taskList', 'tasks', 'task_queue', 'taskQueue')),
        'reward_signal': _pick(('reward_signal', 'rewardSignal', 'reward', 'reward_score', 'rewardScore')),
        'plan_history': _pick_list(('plan_history', 'planHistory', 'recent_plan_history', 'recentPlanHistory', 'history')),
    }


def _task_label(value) -> str:
    if isinstance(value, dict):
        for key in ('title', 'task', 'label', 'name', 'text', 'summary', 'id', 'task_id', 'taskId'):
            candidate = value.get(key)
            if _has_value(candidate):
                return str(candidate)
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return 'unknown'
    return str(value)


def _normalize_task_plan_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    tasks = payload.get('tasks') if isinstance(payload.get('tasks'), list) else payload.get('task_list') if isinstance(payload.get('task_list'), list) else payload.get('taskList') if isinstance(payload.get('taskList'), list) else []
    if not isinstance(tasks, list):
        tasks = [tasks] if _has_value(tasks) else []
    current_task_id = payload.get('current_task_id') or payload.get('currentTaskId')
    current_task = payload.get('current_task') or payload.get('currentTask') or payload.get('task') or payload.get('task_name') or payload.get('taskName') or payload.get('current_task_name') or payload.get('currentTaskName') or current_task_id
    if not _has_value(current_task) and tasks:
        for task in tasks:
            if isinstance(task, dict) and (task.get('status') or '').lower() == 'active':
                current_task = _task_label(task)
                current_task_id = current_task_id or task.get('task_id') or task.get('taskId')
                break
    reward_signal = payload.get('reward_signal') if _has_value(payload.get('reward_signal')) else payload.get('rewardSignal') if _has_value(payload.get('rewardSignal')) else payload.get('reward') if _has_value(payload.get('reward')) else None
    task_counts = payload.get('task_counts') or payload.get('taskCounts')
    if isinstance(reward_signal, str):
        parsed_reward = _json_loads_any(reward_signal)
        if parsed_reward is not None:
            reward_signal = parsed_reward
    history = payload.get('plan_history') if isinstance(payload.get('plan_history'), list) else payload.get('planHistory') if isinstance(payload.get('planHistory'), list) else payload.get('history') if isinstance(payload.get('history'), list) else []
    if not isinstance(history, list):
        history = [history] if _has_value(history) else []
    return {
        'current_task': current_task,
        'current_task_id': current_task_id,
        'task_list': tasks,
        'task_count': len(tasks),
        'task_counts': task_counts,
        'reward_signal': reward_signal,
        'plan_history': history,
        'schema_version': payload.get('schema_version') or payload.get('schemaVersion'),
    }


def _public_task_plan_snapshot(payload: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = dict(_normalize_task_plan_payload(payload))
    snapshot.pop('plan_history', None)
    return snapshot




def _load_local_runtime_state(workspace: Path) -> dict[str, Any]:
    state_root = workspace / 'state'
    reports_dir = state_root / 'reports'
    goals_dir = state_root / 'goals'
    outbox_dir = state_root / 'outbox'
    promotions_dir = state_root / 'promotions'

    latest_report = _latest_json_file(reports_dir, 'evolution-*.json') or _latest_json_file(reports_dir, '*.json')
    latest_goal = (
        goals_dir / 'current.json'
        if (goals_dir / 'current.json').exists()
        else goals_dir / 'active.json'
        if (goals_dir / 'active.json').exists()
        else _latest_json_file(goals_dir, '*.json')
    )
    latest_goal_history = _latest_json_file(goals_dir / 'history', 'cycle-*.json')
    latest_outbox = _latest_json_file(outbox_dir, 'latest.json') or _latest_json_file(outbox_dir, '*.json')
    latest_promotion = _latest_json_file(promotions_dir, 'latest.json') or _latest_json_file(promotions_dir, '*.json')

    report_data = _safe_json_load(latest_report)
    goal_data = _safe_json_load(latest_goal)
    goal_history_data = _safe_json_load(latest_goal_history)
    outbox_data = _safe_json_load(latest_outbox)
    promotion_data = _safe_json_load(latest_promotion)

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

    plan_sources: list[dict[str, Any]] = []
    if isinstance(goal_data, dict):
        plan_sources.append(goal_data)
    if isinstance(goal_history_data, dict):
        plan_sources.append(goal_history_data)
    if isinstance(report_data, dict):
        plan_sources.append(report_data)
    if isinstance(outbox_data, dict):
        plan_sources.append(outbox_data)
    if isinstance(promotion_data, dict):
        plan_sources.append(promotion_data)

    plan_state = _extract_plan_state(*plan_sources)
    normalized_current = _public_task_plan_snapshot(goal_data if isinstance(goal_data, dict) else None)
    if not _has_value(normalized_current.get('current_task')) and not normalized_current.get('task_count') and not _has_value(normalized_current.get('reward_signal')):
        normalized_current = _public_task_plan_snapshot(goal_history_data if isinstance(goal_history_data, dict) else None)
    if not _has_value(normalized_current.get('current_task')) and not normalized_current.get('task_count') and not _has_value(normalized_current.get('reward_signal')):
        normalized_current = _public_task_plan_snapshot(plan_state if isinstance(plan_state, dict) else None)

    plan_history: list[dict[str, Any]] = []
    if isinstance(goal_history_data, dict):
        plan_history.append(_public_task_plan_snapshot(goal_history_data))
    if isinstance(goal_data, dict):
        current_snapshot = _public_task_plan_snapshot(goal_data)
        if current_snapshot not in plan_history:
            plan_history.insert(0, current_snapshot)
    if not plan_history and normalized_current:
        plan_history = [dict(normalized_current)]

    if not _has_value(normalized_current.get('current_task')):
        normalized_current['current_task'] = plan_state.get('current_task')
    if not normalized_current.get('task_list'):
        normalized_current['task_list'] = plan_state.get('task_list') or []
    if not _has_value(normalized_current.get('reward_signal')):
        normalized_current['reward_signal'] = plan_state.get('reward_signal')
    if not normalized_current.get('plan_history'):
        normalized_current['plan_history'] = plan_history

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
        'current_task': normalized_current.get('current_task'),
        'task_list': normalized_current.get('task_list') or [],
        'reward_signal': normalized_current.get('reward_signal'),
        'plan_history': normalized_current.get('plan_history') or [],
        'promotion_path': str(latest_promotion) if latest_promotion else None,
        'subagent_rollup': _subagent_rollup_snapshot(
            state_root=state_root,
            current_task_id=normalized_current.get('current_task_id') if isinstance(normalized_current, dict) else None,
            current_task_title=normalized_current.get('current_task') if isinstance(normalized_current, dict) else None,
        ),
        'raw': {
            'report': report_data,
            'goal': goal_data,
            'goal_history': goal_history_data,
            'outbox': outbox_data,
            'promotion': promotion_data,
            'plan': goal_data,
        },
    }


def _normalize_eeepc_payloads(
    cfg: DashboardConfig,
    outbox: dict[str, Any],
    goals: dict[str, Any],
    reachability: dict[str, Any] | None = None,
    collection_error: dict[str, Any] | None = None,
    outbox_source: str | None = None,
    source_errors: dict[str, Any] | None = None,
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
    raw_payload: dict[str, Any] = {'outbox': outbox, 'goals': goals, 'reachability': reachability}
    if source_errors:
        raw_payload['source_errors'] = source_errors
    return {
        'source': 'eeepc',
        'status': outbox.get('status') or 'unknown',
        'active_goal': active_goal,
        'approval_gate': json.dumps(approval) if approval is not None else None,
        'gate_state': ((approval or {}).get('reason') or (approval or {}).get('state')) if isinstance(approval, dict) else None,
        'report_source': source_report,
        'outbox_source': outbox_source or f"{cfg.eeepc_state_root}/outbox/report.index.json",
        'artifact_paths': artifact_paths,
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'events': events,
        'reachability': reachability,
        'raw': raw_payload,
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
            'outbox_source': f"{cfg.eeepc_state_root}/goals/current.json",
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
    current_plan, current_plan_error = _load_ssh_json(cfg, f"{state_root}/goals/current.json")
    active_plan, active_plan_error = _load_ssh_json(cfg, f"{state_root}/goals/active.json")
    history_paths = _run_ssh_lines(cfg, f"sh -lc 'ls -1t {state_root}/goals/history/cycle-*.json 2>/dev/null | head -n 10'")
    history_payloads: list[dict[str, Any]] = []
    history_errors: list[dict[str, Any]] = []
    for path in history_paths:
        payload, error = _load_ssh_json(cfg, path)
        if isinstance(payload, dict):
            history_payloads.append(payload)
        if error:
            history_errors.append(error)

    canonical_sources_available = any(
        isinstance(payload, dict)
        for payload in (outbox, goals, current_plan, active_plan)
    ) or bool(history_payloads)
    source_errors: dict[str, Any] = {}
    if outbox_error:
        source_errors['outbox'] = outbox_error
    if goals_error:
        source_errors['goals'] = goals_error
    if current_plan_error:
        source_errors['current_plan'] = current_plan_error
    if active_plan_error:
        source_errors['active_plan'] = active_plan_error
    if history_errors:
        source_errors['history'] = history_errors

    collection_error = None if canonical_sources_available else (outbox_error or goals_error or current_plan_error or active_plan_error or (history_errors[0] if history_errors else None))

    plan_source = None
    if isinstance(current_plan, dict):
        plan_source = f"{state_root}/goals/current.json"
    elif isinstance(active_plan, dict):
        plan_source = f"{state_root}/goals/active.json"
    elif isinstance(goals, dict):
        plan_source = f"{state_root}/goals/registry.json"
    elif outbox is not None:
        plan_source = f"{state_root}/outbox/report.index.json"

    normalized = _normalize_eeepc_payloads(
        cfg,
        outbox or {},
        goals or {},
        reachability,
        collection_error,
        plan_source,
        source_errors or None,
    )
    eeepc_subagent_records = _load_ssh_subagent_telemetry(cfg, state_root)
    if eeepc_subagent_records:
        normalized['events'] = (normalized.get('events') or []) + _subagent_events_from_records(eeepc_subagent_records)
        normalized['raw']['subagents'] = eeepc_subagent_records

    current_snapshot = _public_task_plan_snapshot(current_plan if isinstance(current_plan, dict) else active_plan if isinstance(active_plan, dict) else None)
    if not _has_value(current_snapshot.get('current_task')) and not current_snapshot.get('task_count') and not _has_value(current_snapshot.get('reward_signal')):
        current_snapshot = _public_task_plan_snapshot(history_payloads[0] if history_payloads else None)
    plan_history = [_public_task_plan_snapshot(payload) for payload in history_payloads]
    if not plan_history and current_snapshot:
        plan_history = [dict(current_snapshot)]
    if current_snapshot:
        current_snapshot['plan_history'] = plan_history
        if not _has_value(current_snapshot.get('current_task')):
            current_snapshot['current_task'] = normalized.get('current_task')
        if not current_snapshot.get('task_list'):
            current_snapshot['task_list'] = normalized.get('task_list') or []
        if not _has_value(current_snapshot.get('reward_signal')):
            current_snapshot['reward_signal'] = normalized.get('reward_signal')
    normalized['current_task'] = current_snapshot.get('current_task') if current_snapshot else normalized.get('current_task')
    normalized['task_list'] = current_snapshot.get('task_list') if current_snapshot else normalized.get('task_list') or []
    normalized['reward_signal'] = current_snapshot.get('reward_signal') if current_snapshot else normalized.get('reward_signal')
    normalized['plan_history'] = current_snapshot.get('plan_history') if current_snapshot else normalized.get('plan_history') or []
    normalized['raw'] = {'outbox': outbox, 'goals': goals, 'reachability': reachability, 'current_plan': current_plan, 'active_plan': active_plan, 'plan_history': history_payloads}
    if eeepc_subagent_records:
        normalized['raw']['subagents'] = eeepc_subagent_records
    if source_errors:
        normalized['raw']['source_errors'] = source_errors
    return normalized


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
    repo = _normalize_repo_state(cfg.nanobot_repo_root, max_subagent_records=cfg.max_subagent_records)
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
