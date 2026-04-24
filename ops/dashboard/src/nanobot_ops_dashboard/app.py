from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from wsgiref.util import setup_testing_defaults
from urllib.parse import parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .collector import collect_once
from .config import DashboardConfig
from .storage import count_collections, count_events, fetch_events, fetch_latest_collections


MSK = timezone(timedelta(hours=3), name='MSK')


def _cycle_id_from_text(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    match = re.search(r'(cycle-[0-9a-f]{8,})', value)
    if match:
        return match.group(1)
    return None


def _overview_promotion_decision_trail(repo_latest: dict | None, control_plane: dict | None, promotions: list[dict] | None = None) -> str | None:
    repo_latest = dict(repo_latest) if isinstance(repo_latest, dict) else {}
    control_plane = dict(control_plane) if isinstance(control_plane, dict) else {}
    explicit = repo_latest.get('promotion_decision_record')
    if explicit:
        return explicit
    summary = repo_latest.get('promotion_summary')
    experiment = control_plane.get('experiment') if isinstance(control_plane.get('experiment'), dict) else {}
    review_status = experiment.get('review_status')
    decision = experiment.get('decision')
    if summary and (review_status or decision):
        parts = [summary]
        if review_status and review_status not in summary:
            parts.append(str(review_status))
        if decision and decision not in summary:
            parts.append(str(decision))
        return ' → '.join(parts)
    if summary:
        return str(summary)
    if review_status or decision:
        return ' → '.join(str(x) for x in [review_status, decision] if x)
    if promotions:
        latest = promotions[0] if promotions else None
        if isinstance(latest, dict):
            title = latest.get('title')
            status = latest.get('status')
            detail = latest.get('detail') if isinstance(latest.get('detail'), dict) else {}
            decision_record = detail.get('decision_record')
            accepted_record = detail.get('accepted_record')
            pieces = [part for part in [title, status, decision_record, accepted_record] if part]
            if pieces:
                return ' → '.join(str(p) for p in pieces)
    return None


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


def _json_loads_any(value: str | None):
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _coerce_timestamp(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _normalize_approval_gate_truth(approval, collected_at: str | None = None):
    now = datetime.now(timezone.utc)
    gate = approval
    if isinstance(approval, str):
        parsed = _json_loads_any(approval)
        if isinstance(parsed, dict):
            gate = parsed
    if not isinstance(gate, dict):
        state = str(gate) if gate else None
        return {
            'raw': gate,
            'state': state,
            'fresh': False if state in {'expired', 'missing', 'blocked'} else None,
            'ttl_minutes': None,
            'expires_at_utc': None,
            'expired': None,
        }
    expires_dt = _coerce_timestamp(gate.get('expires_at_utc') or gate.get('expiresAtUtc') or gate.get('expires_at_epoch'))
    ttl = gate.get('ttl_minutes') or gate.get('ttlMinutes')
    if ttl is None and expires_dt is not None:
        ttl = int((expires_dt - now).total_seconds() // 60)
    expired = gate.get('expired')
    if expired is None and ttl is not None:
        expired = ttl < 0
    raw_state = gate.get('state') or gate.get('status') or gate.get('reason') or ('ok' if gate.get('ok') else None)
    state = 'expired' if expired else raw_state
    fresh = None
    if state is not None:
        fresh = state in {'fresh', 'active', 'valid', 'ok'} and not bool(expired)
    normalized = dict(gate)
    normalized['state'] = state
    normalized['ttl_minutes'] = ttl
    normalized['expires_at_utc'] = expires_dt.isoformat().replace('+00:00', 'Z') if expires_dt else gate.get('expires_at_utc')
    normalized['expired'] = expired
    return {
        'raw': normalized,
        'state': state,
        'fresh': fresh,
        'ttl_minutes': ttl,
        'expires_at_utc': normalized.get('expires_at_utc'),
        'expired': expired,
        'collected_at': collected_at,
    }


def _experiment_truth_summary(snapshot: dict | None) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    execution_status = snapshot.get('raw', {}).get('result_status') or snapshot.get('status')
    outcome = snapshot.get('outcome')
    if execution_status and outcome and str(execution_status).upper() != str(outcome).upper():
        display = f"{execution_status} / {outcome}"
    else:
        display = execution_status or outcome or 'unknown'
    return {
        'execution_status': execution_status,
        'evaluation_outcome': outcome,
        'display_status': display,
        'reconciled': bool(execution_status and outcome and str(execution_status).upper() != str(outcome).upper()),
    }


def _systemd_user_service_guard(unit: str) -> dict:
    props = ['ActiveState', 'SubState', 'MemoryCurrent', 'MemoryMax', 'RuntimeMaxUSec']
    try:
        output = subprocess.check_output(
            ['systemctl', '--user', 'show', unit, *(f'-p{prop}' for prop in props), '--no-pager'],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception as exc:
        return {'unit': unit, 'available': False, 'error': str(exc)}
    result = {'unit': unit, 'available': True}
    for line in output.splitlines():
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        result[key] = value
    return result


def _control_plane_summary(repo_latest, eeepc_latest, current_experiment, current_blocker, cfg):
    repo_latest = dict(repo_latest) if repo_latest else {}
    eeepc_latest = dict(eeepc_latest) if eeepc_latest else {}
    repo_raw = _json_loads_dict(repo_latest.get('raw_json')) if repo_latest else {}
    producer_summary_path = cfg.project_root / 'workspace' / 'state' / 'control_plane' / 'current_summary.json'
    if not producer_summary_path.exists():
        alt_summary_path = cfg.nanobot_repo_root / 'workspace' / 'state' / 'control_plane' / 'current_summary.json'
        producer_summary_path = alt_summary_path if alt_summary_path.exists() else producer_summary_path
    producer_summary = _structured_file_payload(producer_summary_path) if producer_summary_path.exists() else {}
    guarded_state_path = cfg.nanobot_repo_root / 'workspace' / 'state' / 'self_evolution' / 'current_state.json'
    guarded_evolution = _structured_file_payload(guarded_state_path) if guarded_state_path.exists() else {}
    local_ci_state_path = cfg.nanobot_repo_root / 'workspace' / 'state' / 'local_ci' / 'current_state.json'
    local_ci = _structured_file_payload(local_ci_state_path) if local_ci_state_path.exists() else {}
    active_exec_path = cfg.project_root / 'control' / 'active_execution.json'
    active_exec = _structured_file_payload(active_exec_path) if active_exec_path.exists() else {}
    execution_completion_path = cfg.project_root / 'control' / 'execution_completion.json'
    execution_completion = _structured_file_payload(execution_completion_path) if execution_completion_path.exists() else {}
    approval_source = producer_summary.get('approval_gate') if isinstance(producer_summary, dict) and producer_summary.get('approval_gate') else (repo_latest.get('approval_gate') if repo_latest else None)
    approval = _normalize_approval_gate_truth(approval_source, repo_latest.get('collected_at') if repo_latest else None)
    human_review_boundary = {
        'state': 'open' if (approval.get('state') in {'fresh', 'active', 'valid', 'ok'}) else 'closed',
        'reason': 'approval_gate_valid' if (approval.get('state') in {'fresh', 'active', 'valid', 'ok'}) else (approval.get('state') or 'approval_gate_unavailable'),
        'expires_at_utc': approval.get('expires_at_utc'),
    }
    completion_status = (execution_completion.get('status') if isinstance(execution_completion, dict) else None) or (active_exec.get('execution_completion_status') if isinstance(active_exec, dict) else None)
    completion_verified = (execution_completion.get('verification_status') if isinstance(execution_completion, dict) else None) or (active_exec.get('execution_completion_verification_status') if isinstance(active_exec, dict) else None)
    completion_terminal = completion_status in {'completed', 'verified_completed'} and completion_verified in {'verified', 'pass', 'PASS', 'passed'}
    if completion_terminal:
        governance_enforcement = {'state': 'enforced', 'reason': 'verified_completion_pointer'}
    elif completion_status == 'completed':
        governance_enforcement = {'state': 'pending', 'reason': 'completion_unverified'}
    else:
        governance_enforcement = {'state': 'open', 'reason': 'no_verified_terminal_completion'}
    experiment_source = producer_summary.get('experiment') if isinstance(producer_summary, dict) and producer_summary.get('experiment') else current_experiment
    experiment_truth = _experiment_truth_summary(experiment_source)
    live_task = active_exec.get('live_task') if isinstance(active_exec, dict) and isinstance(active_exec.get('live_task'), dict) else {}
    has_executor_linkage = any(live_task.get(key) for key in (
        'delegated_executor_request_path',
        'executor_handoff_path',
        'execution_request_path',
        'pi_dev_request_path',
        'pi_dev_dispatch_path',
    ))
    stale_exec = False if completion_terminal else (bool((active_exec or {}).get('stale_execution_detected')) or (bool(live_task) and not has_executor_linkage))
    live_exec = False if completion_terminal else (bool((active_exec or {}).get('has_actually_executing_task')) and has_executor_linkage and not stale_exec)
    waiting_dispatch = False if completion_terminal else (bool(live_task) and not has_executor_linkage)
    execution_state = 'completed' if completion_terminal else 'stale' if stale_exec else 'live' if live_exec else 'waiting_for_dispatch' if waiting_dispatch else 'idle'
    return {
        'active_goal': (eeepc_latest or {}).get('active_goal') or (repo_latest or {}).get('active_goal'),
        'repo_status': (repo_latest or {}).get('status'),
        'eeepc_status': (eeepc_latest or {}).get('status'),
        'approval': approval,
        'current_blocker': current_blocker,
        'current_task': (producer_summary.get('task_plan') or {}).get('current_task') or (repo_latest or {}).get('current_task'),
        'producer_summary': producer_summary if isinstance(producer_summary, dict) else {},
        'guarded_evolution': guarded_evolution if isinstance(guarded_evolution, dict) else {},
        'local_ci': local_ci if isinstance(local_ci, dict) else {},
        'runtime_source': (producer_summary.get('runtime_source') if isinstance(producer_summary, dict) else None),
        'prompt_mass': (producer_summary.get('prompt_mass') if isinstance(producer_summary, dict) else None),
        'owner_utility': (producer_summary.get('owner_utility') if isinstance(producer_summary, dict) else None),
        'human_review_boundary': human_review_boundary,
        'governance_enforcement': governance_enforcement,
        'launch_criteria': {
            'state': 'healthy' if governance_enforcement.get('state') == 'enforced' and human_review_boundary.get('state') == 'open' else 'action_required',
            'latest_probe': 'dashboard_and_runtime_regression_matrix',
            'evidence': 'docs/LAUNCH_CRITERIA_AND_REGRESSION_PROBES.md',
        },
        'validation_summary': (producer_summary.get('validation_summary') if isinstance(producer_summary, dict) else None),
        'validation_warnings': (producer_summary.get('validation_warnings') if isinstance(producer_summary, dict) else None),
        'validation_errors': (producer_summary.get('validation_errors') if isinstance(producer_summary, dict) else None),
        'capabilities': repo_raw.get('capabilities') if isinstance(repo_raw, dict) else None,
        'host_resources': repo_raw.get('host_resources') if isinstance(repo_raw, dict) else None,
        'subagent_correlation': repo_raw.get('subagent_correlation') if isinstance(repo_raw, dict) else None,
        'operator_boost': repo_raw.get('operator_boost') if isinstance(repo_raw, dict) else None,
        'governance_schema': repo_raw.get('governance_schema') if isinstance(repo_raw, dict) else None,
        'governance_coverage': repo_raw.get('governance_coverage') if isinstance(repo_raw, dict) else None,
        'task_boundary': repo_raw.get('task_boundary') if isinstance(repo_raw, dict) else None,
        'mutation_lane': ((repo_raw.get('task_boundary') or {}).get('mutation_lane') if isinstance(repo_raw, dict) and isinstance(repo_raw.get('task_boundary'), dict) else None),
        'action_registry': repo_raw.get('action_registry') if isinstance(repo_raw, dict) else None,
        'experiment': experiment_truth,
        'active_execution': active_exec if isinstance(active_exec, dict) else {},
        'execution_completion': execution_completion if isinstance(execution_completion, dict) else {},
        'execution_state': execution_state,
        'service_guards': {
            'collector': _systemd_user_service_guard('nanobot-ops-dashboard-collector.service'),
            'web': _systemd_user_service_guard('nanobot-ops-dashboard-web.service'),
        },
        'stale_execution_detected': stale_exec,
        'live_execution_exists': live_exec,
        'waiting_for_dispatch': waiting_dispatch,
        'has_executor_linkage': has_executor_linkage,
    }



def _decorate_rows(rows):
    decorated = []
    for row in rows:
        item = dict(row)
        item['detail'] = _json_loads_dict(item.get('detail_json'))
        decorated.append(item)
    return decorated



def _row_timestamp(row) -> str:
    detail = row.get('detail') or {}
    return row.get('collected_at') or detail.get('finished_at') or detail.get('started_at') or ''



def _sort_rows_desc(rows):
    return sorted(rows, key=lambda row: _row_timestamp(row), reverse=True)



def _status_kind(status: str | None) -> str:
    normalized = (status or 'unknown').strip().upper()
    if normalized in {'PASS', 'ACCEPT', 'APPROVED', 'OK', 'SUCCESS'}:
        return 'pass'
    if normalized in {'BLOCK', 'FAIL', 'ERROR', 'REJECT', 'DECLINE', 'DENY'}:
        return 'block'
    if normalized in {'UNKNOWN', 'PENDING', 'REVIEW', 'NONE', 'IN_PROGRESS'}:
        return 'unknown'
    return 'unknown' if not status else 'neutral'



def _status_label(status: str | None) -> str:
    return (status or 'unknown').strip() or 'unknown'



def _origin_label(detail: dict | None) -> str:
    if not isinstance(detail, dict):
        return 'unknown'
    origin = detail.get('origin')
    if not isinstance(origin, dict):
        source = detail.get('source')
        return source or 'unknown'
    channel = origin.get('channel')
    chat_id = origin.get('chat_id')
    if channel and chat_id:
        return f'{channel}:{chat_id}'
    return channel or chat_id or detail.get('source') or 'unknown'


def _subagent_detail_value(detail: dict | None, *keys: str):
    if not isinstance(detail, dict):
        return None
    for key in keys:
        value = detail.get(key)
        if _has_value(value):
            return value
    origin = detail.get('origin') if isinstance(detail.get('origin'), dict) else None
    for key in keys:
        if origin:
            value = origin.get(key)
            if _has_value(value):
                return value
    return None



def _report_source_label(value) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return 'report source unavailable'



def _filter_rows(rows, source: str | None, status: str | None, origin: str | None = None):
    result = rows
    if source:
        result = [row for row in result if row.get('source') == source]
    if status:
        result = [row for row in result if row.get('status') == status]
    if origin:
        result = [row for row in result if _origin_label(row.get('detail')) == origin]
    return result



def _compute_status_streak(rows, status_name: str) -> int:
    streak = 0
    for row in rows:
        if (row.get('status') or 'unknown') == status_name:
            streak += 1
        else:
            break
    return streak



def _latest_status_timestamp(rows, status_name: str) -> str | None:
    for row in rows:
        if (row.get('status') or 'unknown') == status_name:
            return row.get('collected_at')
    return None


def _current_streak_summary(rows) -> dict:
    if not rows:
        return {'status': None, 'length': 0, 'started_at': None, 'latest_at': None}
    current_status = rows[0].get('status') or 'unknown'
    length = _compute_status_streak(rows, current_status)
    streak_rows = rows[:length]
    return {
        'status': current_status,
        'length': length,
        'latest_at': rows[0].get('collected_at'),
        'started_at': streak_rows[-1].get('collected_at') if streak_rows else rows[0].get('collected_at'),
    }



def _top_goals(rows, limit: int = 5) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        goal = row.get('title') or 'unknown'
        counts[goal] = counts.get(goal, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{'goal': goal, 'count': count} for goal, count in ordered]



def _top_block_reasons(rows, limit: int = 5) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        if (row.get('status') or 'unknown') != 'BLOCK':
            continue
        detail = row.get('detail') or {}
        reason = detail.get('failure_class') or detail.get('blocked_next_step') or 'unknown'
        counts[reason] = counts.get(reason, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{'reason': reason, 'count': count} for reason, count in ordered]



def _artifact_history(rows, limit: int = 10) -> list[dict]:
    items = []
    for row in rows:
        detail = row.get('detail') or {}
        for artifact in detail.get('artifact_paths') or []:
            items.append({
                'collected_at': row.get('collected_at'),
                'source': row.get('source'),
                'title': row.get('title'),
                'artifact': artifact,
                'status': row.get('status'),
            })
    return items[:limit]


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _plan_item_label(value) -> str:
    if isinstance(value, dict):
        for key in ('title', 'task', 'label', 'name', 'text', 'summary', 'id'):
            candidate = value.get(key)
            if _has_value(candidate):
                return str(candidate)
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return 'unknown'
    return str(value)


def _reward_signal_text(value) -> str:
    if value is None:
        return 'unknown'
    if isinstance(value, dict):
        parts = []
        for key in ('status', 'state', 'score', 'value', 'reason', 'signal'):
            candidate = value.get(key)
            if _has_value(candidate):
                parts.append(f'{key}={candidate}')
        if parts:
            return ' | '.join(parts)
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return ', '.join(_plan_item_label(item) for item in value) or 'unknown'
    return str(value)



def _budget_signal_text(value) -> str:
    if value is None:
        return 'unknown'
    if isinstance(value, dict):
        parts = []
        for key in ('status', 'state', 'spent', 'remaining', 'limit', 'budget', 'currency', 'reason'):
            candidate = value.get(key)
            if _has_value(candidate):
                parts.append(f'{key}={candidate}')
        if parts:
            return ' | '.join(parts)
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_SELECTED_TASK_LABEL_SUFFIX = re.compile(r'\s*\[task_id=[^\]]+\]\s*$')


def _selected_task_title(value) -> str | None:
    if isinstance(value, dict):
        for key in ('title', 'task', 'label', 'name', 'text', 'summary', 'id'):
            candidate = value.get(key)
            if _has_value(candidate):
                return str(candidate)
        return None
    if isinstance(value, list):
        titles = [_selected_task_title(item) or _plan_item_label(item) for item in value if _has_value(item)]
        return ', '.join(title for title in titles if _has_value(title)) or None
    if isinstance(value, str):
        cleaned = _SELECTED_TASK_LABEL_SUFFIX.sub('', value).strip()
        return cleaned or value.strip() or None
    if value is None:
        return None
    return str(value)


def _selected_tasks_text(value) -> str:
    if value is None:
        return 'unknown'
    if isinstance(value, list):
        labels = [_selected_task_title(item) or _plan_item_label(item) for item in value if _has_value(item)]
        return ', '.join(label for label in labels if _has_value(label)) or 'unknown'
    if isinstance(value, dict):
        return _selected_task_title(value) or json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or 'unknown'
    return str(value)



def _first_present(mapping: dict, keys: tuple[str, ...]):
    for key in keys:
        value = mapping.get(key)
        if _has_value(value):
            return value
    return None



def _display_or(value, fallback: str = 'unknown'):
    return value if _has_value(value) else fallback


def _structured_file_payload(path: Path):
    try:
        content = path.read_text(encoding='utf-8').strip()
    except Exception:
        return None
    if not content:
        return None
    if path.suffix == '.jsonl':
        for line in reversed(content.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except Exception:
                continue
        return None
    try:
        return json.loads(content)
    except Exception:
        return content



def _experiment_budget_candidates(state_root: Path) -> list[Path]:
    directories = [state_root / 'experiments', state_root / 'experiment', state_root / 'budgets', state_root / 'budget']
    files: list[Path] = []
    for directory in directories:
        if not directory.exists():
            continue
        for pattern in ('*.json', '*.jsonl'):
            files.extend(directory.glob(pattern))
    return sorted({path for path in files if path.exists()}, key=lambda path: path.stat().st_mtime, reverse=True)



def _experiment_snapshot_from_payload(payload, source_path: Path) -> dict | None:
    if not isinstance(payload, dict):
        return None
    experiment_payload = payload
    for key in ('current_experiment', 'currentExperiment', 'experiment', 'current_experiment_snapshot', 'current_experiment_state'):
        nested = payload.get(key)
        if isinstance(nested, dict):
            experiment_payload = nested
            break
    reward_signal = _first_present(experiment_payload, ('reward_signal', 'rewardSignal'))
    if reward_signal is None:
        reward_signal = _first_present(payload, ('reward_signal', 'rewardSignal'))
    if isinstance(reward_signal, str):
        parsed_reward = _json_loads_any(reward_signal)
        if parsed_reward is not None:
            reward_signal = parsed_reward
    budget_payload = _first_present(experiment_payload, ('budget',))
    if budget_payload is None:
        budget_payload = _first_present(payload, ('budget',))
    if not isinstance(budget_payload, dict):
        budget_payload = {
            key: value for key, value in {
                'budget': _first_present(experiment_payload, ('budget',)),
                'spent': _first_present(experiment_payload, ('budget_spent', 'budgetSpent', 'spent')),
                'remaining': _first_present(experiment_payload, ('budget_remaining', 'budgetRemaining', 'remaining')),
                'limit': _first_present(experiment_payload, ('budget_limit', 'budgetLimit', 'limit')),
                'currency': _first_present(experiment_payload, ('currency', 'budget_currency', 'budgetCurrency')),
                'status': _first_present(experiment_payload, ('budget_status', 'budgetStatus', 'status')),
            }.items() if _has_value(value)
        }
    experiment_id = _first_present(experiment_payload, ('experiment_id', 'experimentId', 'id', 'name', 'title', 'slug'))
    title_value = _first_present(experiment_payload, ('title', 'name', 'summary', 'label'))
    status = _first_present(experiment_payload, ('status', 'state', 'result_status', 'outcome')) or 'unknown'
    execution_status = _first_present(experiment_payload, ('result_status', 'resultStatus', 'status', 'state')) or status
    phase = _first_present(experiment_payload, ('phase', 'stage'))
    outcome = _first_present(experiment_payload, ('outcome',))
    metric_name = _first_present(experiment_payload, ('metric_name', 'metricName'))
    metric_baseline = _first_present(experiment_payload, ('metric_baseline', 'metricBaseline'))
    metric_current = _first_present(experiment_payload, ('metric_current', 'metricCurrent'))
    metric_frontier = _first_present(experiment_payload, ('metric_frontier', 'metricFrontier'))
    contract_path = _first_present(experiment_payload, ('contract_path', 'contractPath'))
    is_experiment_snapshot = any(_has_value(value) for value in (experiment_id, title_value, reward_signal, phase, outcome, metric_name, contract_path))
    title = title_value or experiment_id or 'unknown experiment'
    collected_at = _first_present(experiment_payload, ('collected_at', 'collectedAt', 'finished_at', 'finishedAt', 'started_at', 'startedAt'))
    if not collected_at:
        collected_at = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc).isoformat().replace('+00:00', 'Z') if source_path.exists() else None
    return {
        'source_path': str(source_path),
        'source_file': source_path.name,
        'collected_at': collected_at,
        'experiment_id': str(experiment_id) if _has_value(experiment_id) else None,
        'title': str(title),
        'status': str(status),
        'execution_status': str(execution_status) if _has_value(execution_status) else None,
        'phase': str(phase) if _has_value(phase) else None,
        'is_experiment_snapshot': is_experiment_snapshot,
        'reward_signal': reward_signal,
        'reward_text': _reward_signal_text(reward_signal),
        'budget': budget_payload if budget_payload else None,
        'budget_text': _budget_signal_text(budget_payload if budget_payload else None),
        'outcome': str(outcome) if _has_value(outcome) else None,
        'metric_name': str(metric_name) if _has_value(metric_name) else None,
        'metric_baseline': metric_baseline,
        'metric_current': metric_current,
        'metric_frontier': metric_frontier,
        'contract_path': str(contract_path) if _has_value(contract_path) else None,
        'revert_required': bool(experiment_payload.get('revert_required')),
        'revert_status': _first_present(experiment_payload, ('revert_status', 'revertStatus')),
        'revert_path': _first_present(experiment_payload, ('revert_path', 'revertPath')),
        'raw': payload,
    }



def _discover_experiment_visibility(cfg: DashboardConfig, plan_latest: dict | None = None) -> dict:
    state_roots = [cfg.nanobot_repo_root / 'workspace' / 'state', cfg.nanobot_repo_root / 'state']
    candidate_files: list[Path] = []
    for state_root in state_roots:
        candidate_files.extend(_experiment_budget_candidates(state_root))
    candidate_files = sorted({path for path in candidate_files if path.exists()}, key=lambda path: path.stat().st_mtime, reverse=True)

    experiment_history: list[dict] = []
    budget_history: list[dict] = []
    for path in candidate_files:
        payload = _structured_file_payload(path)
        if not isinstance(payload, dict):
            continue
        snapshot = _experiment_snapshot_from_payload(payload, path)
        if snapshot is None:
            continue
        has_experiment_fields = bool(snapshot.get('is_experiment_snapshot'))
        if snapshot.get('budget'):
            budget_history.append(snapshot)
        if has_experiment_fields:
            experiment_history.append(snapshot)

    current_experiment = experiment_history[0] if experiment_history else None
    current_budget = next((snapshot for snapshot in budget_history if '/budgets/' in (snapshot.get('source_path') or '')), None) or (budget_history[0] if budget_history else None)
    reward_source = 'experiment telemetry' if current_experiment and current_experiment.get('reward_signal') is not None else 'task plan snapshot' if plan_latest and plan_latest.get('reward_signal') is not None else 'unavailable'
    reward_signal = current_experiment.get('reward_signal') if current_experiment and current_experiment.get('reward_signal') is not None else (plan_latest.get('reward_signal') if isinstance(plan_latest, dict) else None)
    reward_text = _reward_signal_text(reward_signal)
    if current_budget is None and current_experiment and current_experiment.get('budget'):
        current_budget = current_experiment

    return {
        'available': bool(experiment_history or current_budget),
        'state_roots': [str(root) for root in state_roots],
        'candidate_files': [str(path) for path in candidate_files[:25]],
        'experiment_history': experiment_history[:10],
        'budget_history': budget_history[:10],
        'current_experiment': current_experiment,
        'current_budget': current_budget,
        'current_reward_signal': reward_signal,
        'current_reward_text': reward_text,
        'reward_source': reward_source,
        'empty_state_reason': (
            'No experiment or budget telemetry files were found under workspace/state/experiments or workspace/state/budgets.'
            if not (experiment_history or current_budget) else None
        ),
    }



def _discover_credits_visibility(cfg: DashboardConfig) -> dict:
    state_roots = [cfg.nanobot_repo_root / 'workspace' / 'state', cfg.nanobot_repo_root / 'state']
    candidate_files: list[Path] = []
    history_files: list[Path] = []
    for state_root in state_roots:
        credits_dir = state_root / 'credits'
        if not credits_dir.exists():
            continue
        latest = credits_dir / 'latest.json'
        history = credits_dir / 'history.jsonl'
        if latest.exists():
            candidate_files.append(latest)
        if history.exists():
            history_files.append(history)
    candidate_files = sorted({path for path in candidate_files if path.exists()}, key=lambda path: path.stat().st_mtime, reverse=True)
    history_files = sorted({path for path in history_files if path.exists()}, key=lambda path: path.stat().st_mtime, reverse=True)
    current = None
    current_path = None
    if candidate_files:
        payload = _structured_file_payload(candidate_files[0])
        if isinstance(payload, dict):
            current = payload
            current_path = candidate_files[0]
    history_rows = []
    for path in history_files[:3]:
        try:
            for line in reversed(path.read_text(encoding='utf-8').splitlines()):
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    history_rows.append({**payload, 'source_path': str(path)})
                if len(history_rows) >= 10:
                    break
        except Exception:
            continue
        if len(history_rows) >= 10:
            break
    return {
        'available': current is not None,
        'current': current,
        'current_path': str(current_path) if current_path else None,
        'history': history_rows[:10],
        'candidate_files': [str(path) for path in candidate_files[:10]],
        'history_files': [str(path) for path in history_files[:10]],
        'state_roots': [str(root) for root in state_roots],
        'empty_state_reason': 'No credits ledger files were found under workspace/state/credits.' if current is None else None,
    }



def _hypothesis_backlog_candidates(state_root: Path) -> list[Path]:
    directories = [state_root / 'hypotheses', state_root / 'hypothesis']
    files: list[Path] = []
    for directory in directories:
        if not directory.exists():
            continue
        backlog = directory / 'backlog.json'
        if backlog.exists():
            files.append(backlog)
    return sorted({path for path in files if path.exists()}, key=lambda path: path.stat().st_mtime, reverse=True)



def _hypothesis_score_text(value) -> str:
    if value is None:
        return 'unknown'
    return str(value)


def _wsjf_text(value) -> str:
    if not isinstance(value, dict):
        return 'unknown'
    keys = ['user_business_value', 'time_criticality', 'risk_reduction_opportunity_enablement', 'job_size', 'score']
    parts = [f'{key}={value[key]}' for key in keys if _has_value(value.get(key))]
    return ' | '.join(parts) if parts else 'unknown'


def _hadi_text(value) -> str:
    if not isinstance(value, dict):
        return 'unknown'
    return ' | '.join(
        f'{key}={value.get(key)}'
        for key in ('hypothesis', 'action')
        if _has_value(value.get(key))
    ) or 'unknown'


def _hypothesis_budget_text(value) -> str:
    if value is None:
        return 'unknown'
    if isinstance(value, dict):
        parts = []
        for key in ('limit', 'spent', 'remaining', 'currency', 'status', 'budget'):
            candidate = value.get(key)
            if _has_value(candidate):
                parts.append(f'{key}={candidate}')
        if parts:
            return ' | '.join(parts)
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _hypothesis_entry_snapshot(entry: dict, selected_id: str | None = None, selected_title: str | None = None) -> dict:
    item = dict(entry)
    hypothesis_id = _first_present(item, ('hypothesis_id', 'hypothesisId', 'id', 'name', 'slug'))
    title = _first_present(item, ('title', 'hypothesis_title', 'hypothesisTitle', 'name', 'summary', 'task', 'label', 'task_title'))
    bounded_priority_score = _first_present(item, ('bounded_priority_score', 'boundedPriorityScore', 'priority_score', 'priorityScore', 'score'))
    selection_status = _first_present(item, ('selection_status', 'selectionStatus', 'status'))
    if isinstance(selection_status, str):
        selection_status = selection_status.strip() or 'unknown'
    execution_spec = item.get('execution_spec') if isinstance(item.get('execution_spec'), dict) else item.get('executionSpec') if isinstance(item.get('executionSpec'), dict) else {}
    if not isinstance(execution_spec, dict):
        execution_spec = {}
    execution_goal = _first_present(execution_spec, ('goal', 'objective', 'target'))
    execution_task = _first_present(execution_spec, ('task', 'task_description', 'taskDescription', 'action', 'task_title'))
    execution_acceptance = _first_present(execution_spec, ('acceptance', 'acceptance_criteria', 'acceptanceCriteria', 'criteria'))
    execution_budget = _first_present(execution_spec, ('budget', 'budget_limit', 'budgetLimit', 'limit'))
    wsjf = item.get('wsjf') if isinstance(item.get('wsjf'), dict) else item.get('WSJF') if isinstance(item.get('WSJF'), dict) else None
    hadi = item.get('hadi') if isinstance(item.get('hadi'), dict) else item.get('HADI') if isinstance(item.get('HADI'), dict) else None
    selected = False
    if selected_id and hypothesis_id and str(hypothesis_id) == str(selected_id):
        selected = True
    elif selected_title and title and str(title) == str(selected_title):
        selected = True
    elif isinstance(selection_status, str) and selection_status.strip().lower() in {'selected', 'select', 'selected hypothesis', 'chosen', 'active'}:
        selected = True
    elif selection_status is True:
        selected = True
    return {
        'raw': item,
        'hypothesis_id': str(hypothesis_id) if _has_value(hypothesis_id) else None,
        'title': str(title) if _has_value(title) else None,
        'bounded_priority_score': bounded_priority_score,
        'bounded_priority_score_text': _hypothesis_score_text(bounded_priority_score),
        'selection_status': selection_status if _has_value(selection_status) else None,
        'selected': selected,
        'execution_spec': execution_spec or None,
        'execution_spec_goal': str(execution_goal) if _has_value(execution_goal) else None,
        'execution_spec_task': str(execution_task) if _has_value(execution_task) else None,
        'execution_spec_acceptance': str(execution_acceptance) if _has_value(execution_acceptance) else None,
        'execution_spec_budget': execution_budget if _has_value(execution_budget) else None,
        'execution_spec_budget_text': _hypothesis_budget_text(execution_budget),
        'wsjf': wsjf,
        'wsjf_text': _wsjf_text(wsjf),
        'hadi': hadi,
        'hadi_text': _hadi_text(hadi),
    }


def _discover_hypotheses_visibility(cfg: DashboardConfig) -> dict:
    state_roots = [cfg.nanobot_repo_root / 'workspace' / 'state', cfg.nanobot_repo_root / 'state']
    candidate_files: list[Path] = []
    for state_root in state_roots:
        candidate_files.extend(_hypothesis_backlog_candidates(state_root))
    candidate_files = sorted({path for path in candidate_files if path.exists()}, key=lambda path: path.stat().st_mtime, reverse=True)

    backlog_payload = None
    backlog_path = None
    for path in candidate_files:
        payload = _structured_file_payload(path)
        if isinstance(payload, dict):
            backlog_payload = payload
            backlog_path = path
            break

    entries_payload: list[dict] = []
    selected_id = None
    selected_title = None
    selected_status = None
    selected_score = None
    selected_wsjf = None
    schema_version = None
    backlog_model = None
    if isinstance(backlog_payload, dict):
        schema_version = backlog_payload.get('schema_version') or backlog_payload.get('schemaVersion')
        backlog_model = backlog_payload.get('model')
        entries_value = backlog_payload.get('entries')
        if not isinstance(entries_value, list):
            entries_value = backlog_payload.get('backlog') if isinstance(backlog_payload.get('backlog'), list) else backlog_payload.get('items') if isinstance(backlog_payload.get('items'), list) else []
        selected_id = backlog_payload.get('selected_hypothesis_id') or backlog_payload.get('selectedHypothesisId')
        selected_title = backlog_payload.get('selected_hypothesis_title') or backlog_payload.get('selectedHypothesisTitle')
        selected_status = backlog_payload.get('selected_hypothesis_status') or backlog_payload.get('selectedHypothesisStatus') or backlog_payload.get('selection_status') or backlog_payload.get('selectionStatus')
        selected_score = backlog_payload.get('selected_hypothesis_score') or backlog_payload.get('selectedHypothesisScore')
        entries_payload = [
            _hypothesis_entry_snapshot(entry, selected_id=str(selected_id) if _has_value(selected_id) else None, selected_title=str(selected_title) if _has_value(selected_title) else None)
            for entry in entries_value
            if isinstance(entry, dict)
        ]

    selected_entry = next((entry for entry in entries_payload if entry.get('selected')), None)
    if selected_entry is None and entries_payload:
        selected_entry = next((entry for entry in entries_payload if entry.get('hypothesis_id') and _has_value(selected_id) and str(entry.get('hypothesis_id')) == str(selected_id)), None)
    if selected_entry is None and entries_payload:
        selected_entry = next((entry for entry in entries_payload if entry.get('title') and _has_value(selected_title) and str(entry.get('title')) == str(selected_title)), None)
    if selected_entry:
        selected_id = selected_id or selected_entry.get('hypothesis_id')
        selected_title = selected_title or selected_entry.get('title')
        selected_status = selected_status or selected_entry.get('selection_status')
        selected_score = selected_score if _has_value(selected_score) else selected_entry.get('bounded_priority_score')
        selected_wsjf = selected_entry.get('wsjf')

    top_entries = sorted(
        entries_payload,
        key=lambda entry: (
            0 if isinstance(entry.get('bounded_priority_score'), (int, float)) else 1,
            -float(entry.get('bounded_priority_score') or 0),
            str(entry.get('title') or entry.get('hypothesis_id') or ''),
        ),
    )

    return {
        'available': backlog_path is not None,
        'state_roots': [str(root) for root in state_roots],
        'candidate_files': [str(path) for path in candidate_files[:25]],
        'backlog_path': str(backlog_path) if backlog_path else None,
        'backlog_collected_at': datetime.fromtimestamp(backlog_path.stat().st_mtime, tz=timezone.utc).isoformat().replace('+00:00', 'Z') if backlog_path else None,
        'research_feed': backlog_payload.get('research_feed') if isinstance(backlog_payload, dict) and isinstance(backlog_payload.get('research_feed'), dict) else None,
        'schema_version': schema_version,
        'model': backlog_model,
        'entry_count': len(entries_payload),
        'selected_hypothesis_id': str(selected_id) if _has_value(selected_id) else None,
        'selected_hypothesis_title': str(selected_title) if _has_value(selected_title) else None,
        'selected_hypothesis_status': str(selected_status) if _has_value(selected_status) else None,
        'selected_hypothesis_score': selected_score,
        'selected_hypothesis_score_text': _hypothesis_score_text(selected_score),
        'selected_hypothesis_wsjf': selected_wsjf,
        'selected_hypothesis_wsjf_text': _wsjf_text(selected_wsjf),
        'selected_hypothesis_execution_spec': selected_entry.get('execution_spec') if selected_entry else None,
        'selected_hypothesis_execution_spec_goal': selected_entry.get('execution_spec_goal') if selected_entry else None,
        'selected_hypothesis_execution_spec_task': selected_entry.get('execution_spec_task') if selected_entry else None,
        'selected_hypothesis_execution_spec_acceptance': selected_entry.get('execution_spec_acceptance') if selected_entry else None,
        'selected_hypothesis_execution_spec_budget': selected_entry.get('execution_spec_budget') if selected_entry else None,
        'selected_hypothesis_execution_spec_budget_text': selected_entry.get('execution_spec_budget_text') if selected_entry else 'unknown',
        'selected_hypothesis_hadi': selected_entry.get('hadi') if selected_entry else None,
        'selected_hypothesis_hadi_text': selected_entry.get('hadi_text') if selected_entry else 'unknown',
        'top_entries': top_entries[:5],
        'empty_state_reason': (
            'No hypothesis backlog file was found under workspace/state/hypotheses/backlog.json.'
            if backlog_path is None else None
        ),
    }


def _plan_snapshot_from_row(row) -> dict:
    item = dict(row)
    raw = _json_loads_dict(item.get('raw_json'))
    plan_payload_source = None
    if isinstance(raw, dict):
        item.update(raw)
        for key in ('current_plan', 'currentPlan', 'task_plan', 'taskPlan', 'plan'):
            nested = raw.get(key)
            if isinstance(nested, dict):
                plan_payload_source = plan_payload_source or key
                item.update(nested)
    if not _has_value(item.get('current_task')):
        item['current_task'] = _first_present(item, ('current_task', 'current_task_id', 'selected_task_title', 'selected_task_label'))
    task_list = _json_loads_any(item.get('task_list_json'))
    if isinstance(task_list, list):
        item['task_list'] = task_list
    elif isinstance(item.get('task_list'), list):
        item['task_list'] = item.get('task_list')
    elif isinstance(item.get('tasks'), list):
        item['task_list'] = item.get('tasks')
    elif _has_value(item.get('tasks')):
        item['task_list'] = [item.get('tasks')]
    elif _has_value(item.get('task_list')):
        item['task_list'] = [item.get('task_list')]
    else:
        item['task_list'] = []
    reward_signal = item.get('reward_signal')
    if isinstance(reward_signal, str):
        parsed_reward = _json_loads_any(reward_signal)
        if parsed_reward is not None:
            reward_signal = parsed_reward
    item['reward_signal'] = reward_signal
    if isinstance(item.get('plan_history'), list):
        plan_history = item.get('plan_history')
    else:
        plan_history = _json_loads_any(item.get('plan_history_json'))
        if not isinstance(plan_history, list):
            if _has_value(plan_history):
                plan_history = [plan_history]
            elif _has_value(item.get('plan_history')):
                plan_history = [item.get('plan_history')]
            else:
                plan_history = []
    item['plan_history'] = plan_history

    feedback_decision = item.get('feedback_decision')
    if isinstance(feedback_decision, str):
        parsed_feedback = _json_loads_any(feedback_decision)
        if isinstance(parsed_feedback, dict):
            feedback_decision = parsed_feedback
    if not isinstance(feedback_decision, dict) and isinstance(raw, dict):
        for parent_key in ('current_plan', 'outbox', 'active_plan'):
            parent = raw.get(parent_key)
            if not isinstance(parent, dict):
                continue
            candidate = parent.get('feedback_decision')
            if isinstance(candidate, str):
                parsed_feedback = _json_loads_any(candidate)
                if isinstance(parsed_feedback, dict):
                    candidate = parsed_feedback
            if isinstance(candidate, dict):
                feedback_decision = candidate
                break
            experiment = parent.get('experiment')
            if isinstance(experiment, dict):
                candidate = experiment.get('feedback_decision')
                if isinstance(candidate, str):
                    parsed_feedback = _json_loads_any(candidate)
                    if isinstance(parsed_feedback, dict):
                        candidate = parsed_feedback
                if isinstance(candidate, dict):
                    feedback_decision = candidate
                    break

    selected_tasks = item.get('selected_tasks')
    if isinstance(selected_tasks, str):
        selected_tasks = selected_tasks.strip() or None
    elif isinstance(selected_tasks, (dict, list)):
        pass
    else:
        selected_tasks = None
    if selected_tasks is None and isinstance(raw, dict):
        for parent_key in ('current_plan', 'outbox', 'active_plan'):
            parent = raw.get(parent_key)
            if not isinstance(parent, dict):
                continue
            candidate = parent.get('selected_tasks')
            if isinstance(candidate, str):
                candidate = candidate.strip() or None
            if _has_value(candidate):
                selected_tasks = candidate
                break
            experiment = parent.get('experiment')
            if isinstance(experiment, dict):
                candidate = experiment.get('selected_tasks')
                if isinstance(candidate, str):
                    candidate = candidate.strip() or None
                if _has_value(candidate):
                    selected_tasks = candidate
                    break

    task_selection_source = _first_present(item, ('task_selection_source', 'taskSelectionSource', 'selection_source', 'selectionSource'))
    if not task_selection_source and isinstance(feedback_decision, dict):
        task_selection_source = _first_present(feedback_decision, ('task_selection_source', 'taskSelectionSource', 'selection_source', 'selectionSource'))
    if not task_selection_source and isinstance(raw, dict):
        for parent_key in ('current_plan', 'outbox', 'active_plan'):
            parent = raw.get(parent_key)
            if not isinstance(parent, dict):
                continue
            candidate = _first_present(parent, ('task_selection_source', 'taskSelectionSource', 'selection_source', 'selectionSource'))
            if candidate:
                task_selection_source = candidate
                break
            experiment = parent.get('experiment')
            if isinstance(experiment, dict):
                candidate = _first_present(experiment, ('task_selection_source', 'taskSelectionSource', 'selection_source', 'selectionSource'))
                if candidate:
                    task_selection_source = candidate
                    break

    selected_task_title = None
    if isinstance(feedback_decision, dict):
        selected_task_title = feedback_decision.get('selected_task_title') or feedback_decision.get('selected_task_label')
    if not _has_value(selected_task_title):
        selected_task_title = _selected_task_title(selected_tasks)

    if not isinstance(feedback_decision, dict):
        for history_item in plan_history:
            if not isinstance(history_item, dict):
                continue
            candidate = history_item.get('feedback_decision')
            if isinstance(candidate, str):
                parsed_feedback = _json_loads_any(candidate)
                if isinstance(parsed_feedback, dict):
                    candidate = parsed_feedback
            if isinstance(candidate, dict):
                feedback_decision = candidate
                candidate_selected_tasks = history_item.get('selected_tasks')
                if not _has_value(candidate_selected_tasks):
                    candidate_selected_tasks = _first_present(history_item, ('selected_tasks', 'selectedTasks'))
                candidate_source = _first_present(history_item, ('task_selection_source', 'taskSelectionSource', 'selection_source', 'selectionSource'))
                if not _has_value(candidate_selected_tasks):
                    experiment = history_item.get('experiment')
                    if isinstance(experiment, dict):
                        candidate_selected_tasks = _first_present(experiment, ('selected_tasks', 'selectedTasks'))
                        if not _has_value(candidate_source):
                            candidate_source = _first_present(experiment, ('task_selection_source', 'taskSelectionSource', 'selection_source', 'selectionSource'))
                if _has_value(candidate_selected_tasks):
                    selected_tasks = candidate_selected_tasks
                    selected_task_title = _selected_task_title(candidate_selected_tasks) or selected_task_title
                if _has_value(candidate_source):
                    task_selection_source = candidate_source
                if not _has_value(selected_task_title):
                    selected_task_title = candidate.get('selected_task_title') or candidate.get('selected_task_label')
                break

    return {
        'collected_at': item.get('collected_at'),
        'source': item.get('source'),
        'status': item.get('status'),
        'current_task': item.get('current_task'),
        'task_list': item.get('task_list') or [],
        'task_count': len(item.get('task_list') or []),
        'reward_signal': item.get('reward_signal'),
        'reward_signal_text': _reward_signal_text(item.get('reward_signal')),
        'feedback_decision': feedback_decision,
        'selected_tasks': selected_tasks,
        'selected_tasks_text': _selected_tasks_text(selected_tasks),
        'selected_task_title': selected_task_title,
        'task_selection_source': task_selection_source,
        'plan_history': item.get('plan_history') or [],
        'plan_history_count': len(item.get('plan_history') or []),
        'plan_payload_source': plan_payload_source or 'row',
        'raw_json': item.get('raw_json'),
    }


def _latest_plan_snapshot(rows) -> dict | None:
    snapshots = [snapshot for snapshot in (_plan_snapshot_from_row(row) for row in rows) if _has_value(snapshot.get('current_task')) or snapshot.get('task_count') or _has_value(snapshot.get('reward_signal')) or snapshot.get('plan_history_count') or _has_value(snapshot.get('feedback_decision')) or _has_value(snapshot.get('selected_tasks')) or _has_value(snapshot.get('selected_task_title')) or _has_value(snapshot.get('task_selection_source'))]
    if not snapshots:
        return None
    for snapshot in snapshots:
        if _has_value(snapshot.get('feedback_decision')):
            return snapshot
    for snapshot in snapshots:
        if _has_value(snapshot.get('selected_tasks')) or _has_value(snapshot.get('selected_task_title')) or _has_value(snapshot.get('task_selection_source')):
            return snapshot
    return snapshots[0]


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _display_timestamp(value: str | None) -> str:
    ts = _parse_timestamp(value)
    if ts is None:
        return value or 'unknown'
    return ts.astimezone(MSK).strftime('%Y-%m-%d %H:%M:%S MSK')


def _age_text(value: str | None, now: datetime | None = None) -> str:
    ts = _parse_timestamp(value)
    if ts is None:
        return 'unknown'
    now = now or datetime.now(timezone.utc)
    delta = now - ts
    if delta.total_seconds() < 0:
        return '0s ago'
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f'{seconds}s ago'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes}m ago'
    hours = minutes // 60
    if hours < 24:
        return f'{hours}h ago'
    days = hours // 24
    return f'{days}d ago'


def _sum_observations(groups: list[dict]) -> int:
    return sum(int(group.get('observed_count') or 0) for group in groups)


def _repeat_observations(groups: list[dict]) -> int:
    return max(_sum_observations(groups) - len(groups), 0)


def _latest_cycle_timestamp(rows) -> str | None:
    if not rows:
        return None
    return rows[0].get('collected_at')


def _eeepc_observation_groups(rows, limit: int = 10) -> list[dict]:
    groups: list[dict] = []
    for row in rows:
        item = dict(row)
        report_source = _report_source_label(item.get('report_source'))
        collected_at = item.get('collected_at') or ''
        if groups and groups[-1]['report_source'] == report_source:
            group = groups[-1]
            group['observed_count'] += 1
            group['earliest_observed_at'] = collected_at or group['earliest_observed_at']
        else:
            groups.append({
                'report_source': report_source,
                'latest_observed_at': collected_at,
                'earliest_observed_at': collected_at,
                'observed_count': 1,
                'status': item.get('status') or 'unknown',
                'active_goal': item.get('active_goal'),
            })
    for group in groups:
        latest = _parse_timestamp(group.get('latest_observed_at'))
        earliest = _parse_timestamp(group.get('earliest_observed_at'))
        if latest and earliest and group.get('observed_count', 0) > 1:
            span_minutes = (latest - earliest).total_seconds() / 60
            group['observed_span_minutes'] = round(span_minutes, 1)
            group['approx_cadence_minutes'] = round(span_minutes / (group['observed_count'] - 1), 1)
        else:
            group['observed_span_minutes'] = 0.0 if group.get('observed_count') == 1 else None
            group['approx_cadence_minutes'] = None
    return groups[:limit]



def _compact_collection_row(row) -> dict | None:
    if row is None:
        return None
    if not isinstance(row, dict):
        row = dict(row)
    return {
        'id': row.get('id'),
        'collected_at': row.get('collected_at'),
        'source': row.get('source'),
        'status': row.get('status'),
        'active_goal': row.get('active_goal'),
        'current_task': row.get('current_task'),
        'gate_state': row.get('gate_state'),
        'report_source': row.get('report_source'),
        'outbox_source': row.get('outbox_source'),
        'promotion_summary': row.get('promotion_summary'),
        'promotion_candidate_path': row.get('promotion_candidate_path'),
        'promotion_decision_record': row.get('promotion_decision_record'),
        'promotion_accepted_record': row.get('promotion_accepted_record'),
    }



def _compact_observation_group(item: dict) -> dict:
    return {
        'report_source': item.get('report_source'),
        'latest_observed_at': item.get('latest_observed_at'),
        'earliest_observed_at': item.get('earliest_observed_at'),
        'observed_count': item.get('observed_count'),
        'status': item.get('status'),
        'active_goal': item.get('active_goal'),
        'approx_cadence_minutes': item.get('approx_cadence_minutes'),
    }



def _deployment_snapshot(row, plan_snapshot):
    compact = _compact_collection_row(row)
    if compact is None:
        return None
    raw = _json_loads_dict(row.get('raw_json')) if isinstance(row, dict) else {}
    reachability = raw.get('reachability') if isinstance(raw.get('reachability'), dict) else None
    compact['plan_snapshot'] = {
        'current_task': plan_snapshot.get('current_task') if isinstance(plan_snapshot, dict) else None,
        'task_count': plan_snapshot.get('task_count') if isinstance(plan_snapshot, dict) else None,
        'reward_signal': plan_snapshot.get('reward_signal') if isinstance(plan_snapshot, dict) else None,
        'feedback_decision': plan_snapshot.get('feedback_decision') if isinstance(plan_snapshot, dict) else None,
        'selection_source': plan_snapshot.get('task_selection_source') if isinstance(plan_snapshot, dict) else None,
    }
    compact['reachability'] = reachability
    compact['live_proof'] = 'PASS' if compact.get('status') == 'PASS' or (reachability and reachability.get('reachable') is True) else compact.get('status') or 'unknown'
    compact['recommended_next_action'] = (reachability or {}).get('recommended_next_action') if isinstance(reachability, dict) else None
    return compact



def _file_preview(path: Path, max_chars: int = 800) -> dict:
    exists = path.exists()
    preview = None
    if exists:
        try:
            preview = path.read_text(encoding='utf-8')[:max_chars]
        except Exception as exc:
            preview = f'<unreadable: {exc}>'
    return {'path': str(path), 'exists': exists, 'preview': preview}



def _remote_file_preview(cfg: DashboardConfig, remote_path: str, max_chars: int = 800) -> dict:
    shell_command = f"if [ -f {remote_path!r} ]; then head -c {max_chars} {remote_path!r}; else echo '__MISSING__'; fi"
    ssh_cmd = ['ssh', '-F', '/home/ozand/.ssh/config', '-i', str(cfg.eeepc_ssh_key), '-o', 'IdentitiesOnly=yes', cfg.eeepc_ssh_host, f"bash -lc {json.dumps(shell_command)}"]
    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=20, check=True)
        content = proc.stdout
        if content.strip() == '__MISSING__':
            return {'path': remote_path, 'exists': False, 'preview': None}
        return {'path': remote_path, 'exists': True, 'preview': content[:max_chars]}
    except Exception as exc:
        return {'path': remote_path, 'exists': False, 'preview': f'<remote preview failed: {exc}>'}



def _discover_system_visibility(cfg: DashboardConfig, eeepc_latest, repo_latest) -> dict:
    repo_root = cfg.nanobot_repo_root
    local_files = [
        _file_preview(repo_root / 'README.md'),
        _file_preview(repo_root / 'docs' / 'PROJECT_CHARTER.md'),
        _file_preview(repo_root / 'AGENT.md'),
        _file_preview(repo_root / 'agent.md'),
    ]
    eeepc_raw = _json_loads_dict(eeepc_latest['raw_json']) if eeepc_latest else {}
    goals_payload = eeepc_raw.get('goals') if isinstance(eeepc_raw.get('goals'), dict) else {}
    outbox_payload = eeepc_raw.get('outbox') if isinstance(eeepc_raw.get('outbox'), dict) else {}
    eeepc_files = [
        {'path': f"{cfg.eeepc_state_root}/goals/current.json", 'exists': True, 'preview': json.dumps(goals_payload.get('current') or goals_payload.get('current_goal') or {}, ensure_ascii=False, indent=2)[:800] if goals_payload else '{}'},
        {'path': f"{cfg.eeepc_state_root}/goals/active.json", 'exists': True, 'preview': json.dumps({'active_goal': eeepc_latest['active_goal'] if eeepc_latest else None}, ensure_ascii=False, indent=2)},
        {'path': f"{cfg.eeepc_state_root}/goals/registry.json", 'exists': True, 'preview': json.dumps(goals_payload, ensure_ascii=False, indent=2)[:800] if goals_payload else '{}'},
        _remote_file_preview(cfg, f"{cfg.eeepc_state_root}/ops/AGENT.md"),
        {'path': f"{cfg.eeepc_state_root}/AGENT.md", 'exists': False, 'preview': None},
        {'path': f"{cfg.eeepc_state_root}/agent.md", 'exists': False, 'preview': None},
    ]
    return {
        'eeepc_goal': eeepc_latest['active_goal'] if eeepc_latest else None,
        'eeepc_status': eeepc_latest['status'] if eeepc_latest else None,
        'repo_goal': repo_latest['active_goal'] if repo_latest else None,
        'repo_status': repo_latest['status'] if repo_latest else None,
        'local_files': local_files,
        'eeepc_files': eeepc_files,
        'eeepc_outbox_preview': json.dumps(outbox_payload, ensure_ascii=False, indent=2)[:800] if outbox_payload else '{}',
    }



def create_app(cfg: DashboardConfig):
    env = _env(cfg)
    env.globals['status_kind'] = _status_kind
    env.globals['status_label'] = _status_label
    env.globals['plan_task_label'] = _plan_item_label
    env.globals['reward_signal_text'] = _reward_signal_text
    env.globals['budget_signal_text'] = _budget_signal_text
    env.globals['selected_task_title'] = _selected_task_title
    env.globals['selected_tasks_text'] = _selected_tasks_text
    env.globals['display_or'] = _display_or
    env.globals['subagent_detail_value'] = _subagent_detail_value
    env.globals['display_timestamp'] = _display_timestamp

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
        eeepc_observation_groups = _eeepc_observation_groups(eeepc_rows)
        eeepc_latest_observation = eeepc_observation_groups[0] if eeepc_observation_groups else None
        cycle_source = query.get('source', [None])[0]
        cycle_status = query.get('status', [None])[0]
        promotion_source = query.get('source', [None])[0]
        promotion_status = query.get('status', [None])[0]
        cycles = _sort_rows_desc(_filter_rows(
            _decorate_rows(fetch_events(cfg.db_path, 'eeepc', 'cycle', limit=100) + fetch_events(cfg.db_path, 'repo', 'cycle', limit=100)),
            cycle_source,
            cycle_status,
        ))
        eeepc_cycle_events = [row for row in cycles if row.get('source') == 'eeepc']
        promotions = _filter_rows(
            _decorate_rows(fetch_events(cfg.db_path, 'repo', 'promotion', limit=100)),
            promotion_source,
            promotion_status,
        )
        for row in promotions:
            detail = row.get('detail') if isinstance(row.get('detail'), dict) else {}
            status = row.get('status') or 'unknown'
            if status == 'accept':
                lifecycle_phase = 'accepted'
            elif status in {'reject', 'rejected', 'needs_more_evidence', 'reviewed'}:
                lifecycle_phase = 'reviewed'
            elif status == 'ready_for_policy_review' or detail.get('decision_record') == 'pending_operator_review_packet':
                lifecycle_phase = 'ready'
            else:
                lifecycle_phase = 'candidate'
            ready = 'ready' if detail.get('accepted_record') == 'present' and detail.get('decision_record') == 'present' and detail.get('candidate_path') else 'review_packet_ready' if lifecycle_phase == 'ready' else 'blocked'
            row['replay_readiness'] = ready
            row['lifecycle_phase'] = lifecycle_phase
        all_subagent_events = _sort_rows_desc(
            _decorate_rows(
                fetch_events(cfg.db_path, 'repo', 'subagent', limit=1000) +
                fetch_events(cfg.db_path, 'eeepc', 'subagent', limit=1000)
            )
        )

        repo_latest = repo_rows[0] if repo_rows else None
        eeepc_latest = eeepc_rows[0] if eeepc_rows else None
        repo_plan_snapshot = _plan_snapshot_from_row(repo_latest) if repo_latest else None
        repo_plan_rows = [
            row for row in repo_rows
            if _has_value(_plan_snapshot_from_row(row).get('current_task'))
            or _plan_snapshot_from_row(row).get('task_count')
            or _has_value(_plan_snapshot_from_row(row).get('reward_signal'))
            or _plan_snapshot_from_row(row).get('plan_history_count')
            or _has_value(_plan_snapshot_from_row(row).get('feedback_decision'))
            or _has_value(_plan_snapshot_from_row(row).get('selected_tasks'))
            or _has_value(_plan_snapshot_from_row(row).get('selected_task_title'))
            or _has_value(_plan_snapshot_from_row(row).get('task_selection_source'))
        ]
        eeepc_plan_rows = [
            row for row in eeepc_rows
            if _has_value(_plan_snapshot_from_row(row).get('current_task'))
            or _plan_snapshot_from_row(row).get('task_count')
            or _has_value(_plan_snapshot_from_row(row).get('reward_signal'))
            or _plan_snapshot_from_row(row).get('plan_history_count')
            or _has_value(_plan_snapshot_from_row(row).get('feedback_decision'))
            or _has_value(_plan_snapshot_from_row(row).get('selected_tasks'))
            or _has_value(_plan_snapshot_from_row(row).get('selected_task_title'))
            or _has_value(_plan_snapshot_from_row(row).get('task_selection_source'))
        ]
        eeepc_plan_snapshot = _latest_plan_snapshot(eeepc_plan_rows) if eeepc_plan_rows else None
        plan_rows = repo_plan_rows or eeepc_plan_rows
        plan_history = [
            snapshot
            for snapshot in (_plan_snapshot_from_row(row) for row in plan_rows)
            if _has_value(snapshot.get('current_task')) or snapshot.get('task_count') or _has_value(snapshot.get('reward_signal')) or snapshot.get('plan_history_count')
        ]
        plan_latest = plan_history[0] if plan_history else None
        experiment_visibility = _discover_experiment_visibility(cfg, plan_latest)
        credits_visibility = _discover_credits_visibility(cfg)
        hypotheses_visibility = _discover_hypotheses_visibility(cfg)
        subagent_latest_event = all_subagent_events[0] if all_subagent_events else None
        latest_collected = None
        for row in [eeepc_latest, repo_latest]:
            if row and (latest_collected is None or row['collected_at'] > latest_collected):
                latest_collected = row['collected_at']

        now = datetime.now(timezone.utc)
        loaded_snapshot_count = len(repo_rows) + len(eeepc_rows)
        total_snapshot_count = count_collections(cfg.db_path)
        source_breakdown = {
            'repo': count_collections(cfg.db_path, 'repo'),
            'eeepc': count_collections(cfg.db_path, 'eeepc'),
        }
        loaded_cycle_count = len(cycles)
        total_cycle_count = count_events(cfg.db_path, event_type='cycle')
        eeepc_observation_total = _sum_observations(eeepc_observation_groups)
        eeepc_observation_repeat_count = _repeat_observations(eeepc_observation_groups)
        eeepc_latest_age = _age_text(eeepc_latest['collected_at'] if eeepc_latest else None, now)
        repo_latest_age = _age_text(repo_latest['collected_at'] if repo_latest else None, now)
        latest_collector_success_age = _age_text(latest_collected, now)
        latest_pass_at = _latest_status_timestamp(cycles, 'PASS')
        latest_block_at = _latest_status_timestamp(cycles, 'BLOCK')

        eeepc_raw = _json_loads_dict(eeepc_latest['raw_json']) if eeepc_latest else {}
        eeepc_outbox = eeepc_raw.get('outbox') if isinstance(eeepc_raw.get('outbox'), dict) else {}
        eeepc_reflection = eeepc_outbox.get('process_reflection') if isinstance(eeepc_outbox.get('process_reflection'), dict) else {}
        eeepc_follow = (eeepc_outbox.get('goal') or {}).get('follow_through') if isinstance(eeepc_outbox.get('goal'), dict) else {}
        eeepc_reachability = eeepc_raw.get('reachability') if isinstance(eeepc_raw.get('reachability'), dict) else {}
        eeepc_reachability_at = eeepc_reachability.get('collected_at') if eeepc_reachability else None
        eeepc_reachability_age = _age_text(eeepc_reachability_at, now)
        operator_plan = eeepc_plan_snapshot or plan_latest or {}
        current_blocker = {
            'kind': 'block' if (eeepc_reachability and not eeepc_reachability.get('reachable')) or eeepc_reflection.get('failure_class') or eeepc_follow.get('blocked_next_step') else 'unknown',
            'source': 'reachability watchdog' if eeepc_reachability and not eeepc_reachability.get('reachable') else 'outbox reflection',
            'failure_class': 'control_plane_unreachable' if eeepc_reachability and not eeepc_reachability.get('reachable') else eeepc_reflection.get('failure_class'),
            'improvement_score': eeepc_reflection.get('improvement_score'),
            'blocked_next_step': eeepc_reachability.get('recommended_next_action') if eeepc_reachability and not eeepc_reachability.get('reachable') else (eeepc_follow or {}).get('blocked_next_step'),
            'error': eeepc_reachability.get('error') if eeepc_reachability and not eeepc_reachability.get('reachable') else None,
            'reachable': eeepc_reachability.get('reachable') if eeepc_reachability else None,
            'feedback_decision': operator_plan.get('feedback_decision') if isinstance(operator_plan, dict) else None,
            'selected_tasks': operator_plan.get('selected_tasks') if isinstance(operator_plan, dict) else None,
            'selected_tasks_text': operator_plan.get('selected_tasks_text') if isinstance(operator_plan, dict) else None,
            'selected_task_title': operator_plan.get('selected_task_title') if isinstance(operator_plan, dict) else None,
            'task_selection_source': operator_plan.get('task_selection_source') if isinstance(operator_plan, dict) else None,
        }
        system_visibility = _discover_system_visibility(cfg, eeepc_latest, repo_latest) if path == '/system' else {
            'eeepc_goal': eeepc_latest['active_goal'] if eeepc_latest else None,
            'eeepc_status': eeepc_latest['status'] if eeepc_latest else None,
            'repo_goal': repo_latest['active_goal'] if repo_latest else None,
            'repo_status': repo_latest['status'] if repo_latest else None,
            'local_files': [],
            'eeepc_files': [],
            'eeepc_outbox_preview': '{}',
        }
        control_plane = _control_plane_summary(repo_latest, eeepc_latest, experiment_visibility['current_experiment'], current_blocker, cfg)
        overview_subagent_cycle_id = None
        if subagent_latest_event and isinstance(subagent_latest_event.get('detail'), dict):
            detail = subagent_latest_event['detail']
            overview_subagent_cycle_id = detail.get('cycle_id') or _cycle_id_from_text(detail.get('report_path')) or _cycle_id_from_text(detail.get('source_path'))
        if not overview_subagent_cycle_id and isinstance(control_plane.get('producer_summary'), dict):
            overview_subagent_cycle_id = control_plane['producer_summary'].get('cycle_id')
        if not overview_subagent_cycle_id and isinstance(plan_latest, dict):
            overview_subagent_cycle_id = plan_latest.get('cycle_id') or _cycle_id_from_text(plan_latest.get('report_path'))
        overview_promotion_decision_trail = _overview_promotion_decision_trail(repo_latest, control_plane, promotions)

        analytics = {
            'total_snapshots': total_snapshot_count,
            'loaded_snapshot_window': loaded_snapshot_count,
            'source_breakdown': source_breakdown,
            'loaded_source_breakdown': {
                'repo': len(repo_rows),
                'eeepc': len(eeepc_rows),
            },
            'cycle_status_breakdown': {},
            'cycle_failure_breakdown': {},
            'current_pass_streak': _compute_status_streak(cycles, 'PASS'),
            'current_block_streak': _compute_status_streak(cycles, 'BLOCK'),
            'current_streak': _current_streak_summary(cycles),
            'latest_status_at': cycles[0].get('collected_at') if cycles else None,
            'latest_pass_at': latest_pass_at,
            'latest_pass_age': _age_text(latest_pass_at, now),
            'latest_block_at': latest_block_at,
            'latest_block_age': _age_text(latest_block_at, now),
            'top_goals': _top_goals(cycles),
            'top_block_reasons': _top_block_reasons(cycles),
            'artifact_history': _artifact_history(cycles),
            'eeepc_unique_cycle_reports': len(eeepc_cycle_events),
            'eeepc_observation_groups': eeepc_observation_groups,
            'eeepc_observation_total': eeepc_observation_total,
            'eeepc_observation_repeat_count': eeepc_observation_repeat_count,
            'eeepc_observation_group_count': len(eeepc_observation_groups),
            'recent_unique_cycle_reports': [
                {
                    'collected_at': row.get('collected_at'),
                    'source': row.get('source'),
                    'status': row.get('status'),
                    'title': row.get('title'),
                }
                for row in eeepc_cycle_events[:10]
            ],
            'recent_cycle_timeline': [
                {
                    'collected_at': row.get('collected_at'),
                    'source': row.get('source'),
                    'status': row.get('status'),
                    'title': row.get('title'),
                }
                for row in cycles[:10]
            ],
            'recent_status_sequence': [
                {
                    'collected_at': row.get('collected_at'),
                    'source': row.get('source'),
                    'status': row.get('status'),
                    'title': row.get('title'),
                }
                for row in cycles[:20]
            ],
            'recent_goal_transitions': [
                {
                    'collected_at': row.get('collected_at'),
                    'source': row.get('source'),
                    'goal': row.get('title'),
                    'status': row.get('status'),
                }
                for row in cycles[:10]
            ],
            'loaded_cycle_window': loaded_cycle_count,
            'total_cycle_events': total_cycle_count,
        }
        for row in cycles:
            status_value = row.get('status') or 'unknown'
            analytics['cycle_status_breakdown'][status_value] = analytics['cycle_status_breakdown'].get(status_value, 0) + 1
            failure_class = (row.get('detail') or {}).get('failure_class')
            if failure_class:
                analytics['cycle_failure_breakdown'][failure_class] = analytics['cycle_failure_breakdown'].get(failure_class, 0) + 1
        if not analytics['cycle_failure_breakdown'] and current_blocker.get('failure_class'):
            analytics['cycle_failure_breakdown'][current_blocker['failure_class']] = 1
        if not analytics['top_block_reasons'] and current_blocker.get('blocked_next_step'):
            analytics['top_block_reasons'] = [{'reason': current_blocker['blocked_next_step'], 'count': 1}]
        if not analytics['artifact_history'] and control_plane.get('producer_summary'):
            summary = control_plane['producer_summary']
            if isinstance(summary, dict) and summary.get('report_path'):
                analytics['artifact_history'] = [{
                    'collected_at': summary.get('cycle_id'),
                    'source': 'repo',
                    'status': summary.get('result_status') or 'unknown',
                    'title': summary.get('goal_id') or 'unknown',
                    'artifact': summary.get('report_path'),
                }]

        request_source = query.get('source', [''])[0]
        request_status = query.get('status', [''])[0]
        request_origin = query.get('origin', [''])[0]
        request_limit_raw = query.get('limit', ['100'])[0]
        try:
            request_limit = max(1, min(200, int(request_limit_raw or '100')))
        except ValueError:
            request_limit = 100
        filtered_subagent_events = _filter_rows(all_subagent_events, request_source, request_status, request_origin)
        subagent_events = filtered_subagent_events[:request_limit]
        subagent_sources = sorted({row.get('source') for row in all_subagent_events if row.get('source')})
        subagent_origins = sorted({_origin_label(row.get('detail')) for row in all_subagent_events if _origin_label(row.get('detail')) != 'unknown'})
        subagent_statuses = sorted({row.get('status') or 'unknown' for row in all_subagent_events})
        subagent_total = len(all_subagent_events)
        subagent_filtered_total = len(filtered_subagent_events)

        approval_rows = [
            {**dict(row), 'plan_snapshot': _plan_snapshot_from_row(row), 'approval_truth': _normalize_approval_gate_truth(dict(row).get('approval_gate'), dict(row).get('collected_at'))}
            for row in (eeepc_rows + repo_rows)
        ]

        context = {
            'repo_latest': repo_latest,
            'eeepc_latest': eeepc_latest,
            'repo_rows': repo_rows,
            'eeepc_rows': eeepc_rows,
            'approval_rows': approval_rows,
            'cycles': cycles,
            'promotions': promotions,
            'subagent_events': subagent_events,
            'subagents_available': bool(all_subagent_events),
            'subagent_latest_event': subagent_latest_event,
            'subagent_latest_age': _age_text(subagent_latest_event.get('collected_at') if subagent_latest_event else None, now),
            'experiment_visibility': experiment_visibility,
            'experiments_available': experiment_visibility['available'],
            'current_experiment': experiment_visibility['current_experiment'],
            'current_budget': experiment_visibility['current_budget'],
            'current_reward_signal': experiment_visibility['current_reward_signal'],
            'current_reward_text': experiment_visibility['current_reward_text'],
            'credits_visibility': credits_visibility,
            'current_credits': credits_visibility['current'],
            'credits_history': credits_visibility['history'],
            'experiment_files': experiment_visibility['candidate_files'],
            'experiment_empty_state_reason': experiment_visibility['empty_state_reason'],
            'experiment_state_roots': experiment_visibility['state_roots'],
            'hypotheses_visibility': hypotheses_visibility,
            'hypotheses_available': hypotheses_visibility['available'],
            'hypotheses_files': hypotheses_visibility['candidate_files'],
            'hypotheses_empty_state_reason': hypotheses_visibility['empty_state_reason'],
            'hypothesis_backlog_path': hypotheses_visibility['backlog_path'],
            'hypothesis_selected': {
                'id': hypotheses_visibility['selected_hypothesis_id'],
                'title': hypotheses_visibility['selected_hypothesis_title'],
                'status': hypotheses_visibility['selected_hypothesis_status'],
                'score': hypotheses_visibility['selected_hypothesis_score'],
                'score_text': hypotheses_visibility['selected_hypothesis_score_text'],
                'wsjf': hypotheses_visibility['selected_hypothesis_wsjf'],
                'wsjf_text': hypotheses_visibility['selected_hypothesis_wsjf_text'],
                'hadi': hypotheses_visibility['selected_hypothesis_hadi'],
                'hadi_text': hypotheses_visibility['selected_hypothesis_hadi_text'],
                'execution_spec_goal': hypotheses_visibility['selected_hypothesis_execution_spec_goal'],
                'execution_spec_task': hypotheses_visibility['selected_hypothesis_execution_spec_task'],
                'execution_spec_acceptance': hypotheses_visibility['selected_hypothesis_execution_spec_acceptance'],
                'execution_spec_budget': hypotheses_visibility['selected_hypothesis_execution_spec_budget'],
                'execution_spec_budget_text': hypotheses_visibility['selected_hypothesis_execution_spec_budget_text'],
            },
            'hypothesis_top_entries': hypotheses_visibility['top_entries'],
            'hypothesis_entry_count': hypotheses_visibility['entry_count'],
            'hypothesis_backlog_age': _age_text(hypotheses_visibility.get('backlog_collected_at'), now),
            'latest_collected': latest_collected,
            'latest_collected_age': _age_text(latest_collected, now),
            'latest_collector_success_age': latest_collector_success_age,
            'snapshot_count': total_snapshot_count,
            'loaded_snapshot_count': loaded_snapshot_count,
            'snapshot_window_count': loaded_snapshot_count,
            'total_snapshot_count': total_snapshot_count,
            'eeepc_latest_age': eeepc_latest_age,
            'repo_latest_age': repo_latest_age,
            'eeepc_reachability_age': eeepc_reachability_age,
            'eeepc_reachability_collected_at': eeepc_reachability_at,
            'latest_pass_at': latest_pass_at,
            'latest_pass_age': analytics['latest_pass_age'],
            'latest_block_at': latest_block_at,
            'latest_block_age': analytics['latest_block_age'],
            'eeepc_artifacts': _json_loads_list(eeepc_latest['artifact_paths_json']) if eeepc_latest else [],
            'repo_artifacts': _json_loads_list(repo_latest['artifact_paths_json']) if repo_latest else [],
            'system_visibility': system_visibility,
            'control_plane': control_plane,
            'overview_subagent_cycle_id': overview_subagent_cycle_id,
            'overview_promotion_decision_trail': overview_promotion_decision_trail,
            'analytics': analytics,
            'plan_latest': plan_latest,
            'plan_latest_age': _age_text(plan_latest.get('collected_at') if isinstance(plan_latest, dict) else None, now),
            'plan_history': plan_history,
            'plan_history_count': len(plan_history),
            'plan_available': bool(plan_history),
            'repo_plan_snapshot': repo_plan_snapshot,
            'eeepc_plan_snapshot': eeepc_plan_snapshot,
            'analytics': analytics,
            'current_blocker': current_blocker,
            'eeepc_reachability': eeepc_reachability,
            'request_source': request_source,
            'request_status': request_status,
            'request_origin': request_origin,
            'request_limit': request_limit,
            'subagent_sources': subagent_sources,
            'subagent_origins': subagent_origins,
            'subagent_statuses': subagent_statuses,
            'subagent_total': subagent_total,
            'subagent_filtered_total': subagent_filtered_total,
            'eeepc_observation_groups': eeepc_observation_groups,
            'eeepc_latest_observation': eeepc_latest_observation,
            'eeepc_unique_cycle_reports': len(eeepc_cycle_events),
            'recent_snapshots': sorted([dict(r) for r in (repo_rows[:5] + eeepc_rows[:5])], key=lambda x: x['collected_at'], reverse=True)[:10],
            'recent_cycles': cycles[:10],
        }

        if path == '/api/summary':
            payload = {
                'latest_collected': latest_collected,
                'snapshot_count': total_snapshot_count,
                'loaded_snapshot_count': loaded_snapshot_count,
                'snapshot_window_count': loaded_snapshot_count,
                'total_snapshot_count': total_snapshot_count,
                'cycle_count': loaded_cycle_count,
                'loaded_cycle_count': loaded_cycle_count,
                'total_cycle_events': total_cycle_count,
                'latest_collector_success_age': latest_collector_success_age,
                'latest_collected_age': _age_text(latest_collected, now),
                'eeepc_latest_age': eeepc_latest_age,
                'repo_latest_age': repo_latest_age,
                'eeepc_reachability_age': eeepc_reachability_age,
                'eeepc_reachability_collected_at': eeepc_reachability_at,
                'latest_pass_at': latest_pass_at,
                'latest_pass_age': analytics['latest_pass_age'],
                'latest_block_at': latest_block_at,
                'latest_block_age': analytics['latest_block_age'],
                'eeepc_unique_cycle_reports': len(eeepc_cycle_events),
                'eeepc_observation_groups': [_compact_observation_group(item) for item in eeepc_observation_groups[:10]],
                'eeepc_observation_total': eeepc_observation_total,
                'eeepc_observation_repeat_count': eeepc_observation_repeat_count,
                'promotion_count': len(promotions),
                'repo_latest': _compact_collection_row(repo_latest),
                'eeepc_latest': _compact_collection_row(eeepc_latest),
                'eeepc_latest_observation': _compact_observation_group(eeepc_latest_observation) if eeepc_latest_observation else None,
                'eeepc_reachability': eeepc_reachability,
                'current_blocker': current_blocker,
                'control_plane': control_plane,
                'plan_latest': {
                    'current_task': plan_latest.get('current_task') if plan_latest else None,
                    'current_task_id': plan_latest.get('current_task_id') if plan_latest else None,
                    'task_count': plan_latest.get('task_count') if plan_latest else None,
                    'reward_signal': plan_latest.get('reward_signal') if plan_latest else None,
                    'feedback_decision': plan_latest.get('feedback_decision') if plan_latest else None,
                } if plan_latest else None,
                'plan_history_count': len(plan_history),
                'experiments_available': experiment_visibility['available'],
                'current_experiment': {
                    'experiment_id': experiment_visibility['current_experiment'].get('experiment_id') if experiment_visibility['current_experiment'] else None,
                    'status': experiment_visibility['current_experiment'].get('status') if experiment_visibility['current_experiment'] else None,
                    'reward_text': experiment_visibility['current_experiment'].get('reward_text') if experiment_visibility['current_experiment'] else None,
                },
                'current_budget': experiment_visibility['current_budget'],
                'current_reward_text': experiment_visibility['current_reward_text'],
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/summary/debug':
            payload = {
                'latest_collected': latest_collected,
                'repo_latest': dict(repo_latest) if repo_latest else None,
                'eeepc_latest': dict(eeepc_latest) if eeepc_latest else None,
                'eeepc_observation_groups': eeepc_observation_groups,
                'eeepc_latest_observation': eeepc_latest_observation,
                'plan_latest': plan_latest,
                'current_experiment': experiment_visibility['current_experiment'],
                'current_budget': experiment_visibility['current_budget'],
                'current_blocker': current_blocker,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/plan':
            payload = {
                'current_plan': plan_latest,
                'current_plan_source': plan_latest['source'] if plan_latest else None,
                'current_task': plan_latest['current_task'] if plan_latest else None,
                'selected_task_title': plan_latest['selected_task_title'] if plan_latest else None,
                'task_selection_source': plan_latest['task_selection_source'] if plan_latest else None,
                'selected_tasks_text': plan_latest['selected_tasks_text'] if plan_latest else None,
                'plan_history_count': len(plan_history),
                'recent_plan_history': plan_history[:10],
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/experiments':
            payload = {
                'available': experiment_visibility['available'],
                'current_experiment': experiment_visibility['current_experiment'],
                'current_budget': experiment_visibility['current_budget'],
                'current_reward_signal': experiment_visibility['current_reward_signal'],
                'current_reward_text': experiment_visibility['current_reward_text'],
                'reward_source': experiment_visibility['reward_source'],
                'experiment_history': experiment_visibility['experiment_history'],
                'budget_history': experiment_visibility['budget_history'],
                'candidate_files': experiment_visibility['candidate_files'],
                'state_roots': experiment_visibility['state_roots'],
                'credits': credits_visibility,
                'empty_state_reason': experiment_visibility['empty_state_reason'],
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/credits':
            body = json.dumps(credits_visibility, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/hypotheses':
            payload = {
                'available': hypotheses_visibility['available'],
                'backlog_path': hypotheses_visibility['backlog_path'],
                'schema_version': hypotheses_visibility['schema_version'],
                'model': hypotheses_visibility['model'],
                'entry_count': hypotheses_visibility['entry_count'],
                'selected_hypothesis_id': hypotheses_visibility['selected_hypothesis_id'],
                'selected_hypothesis_title': hypotheses_visibility['selected_hypothesis_title'],
                'selected_hypothesis_status': hypotheses_visibility['selected_hypothesis_status'],
                'selected_hypothesis_score': hypotheses_visibility['selected_hypothesis_score'],
                'selected_hypothesis_score_text': hypotheses_visibility['selected_hypothesis_score_text'],
                'selected_hypothesis_wsjf': hypotheses_visibility['selected_hypothesis_wsjf'],
                'selected_hypothesis_wsjf_text': hypotheses_visibility['selected_hypothesis_wsjf_text'],
                'selected_hypothesis_hadi': hypotheses_visibility['selected_hypothesis_hadi'],
                'selected_hypothesis_hadi_text': hypotheses_visibility['selected_hypothesis_hadi_text'],
                'selected_hypothesis_execution_spec': hypotheses_visibility['selected_hypothesis_execution_spec'],
                'selected_hypothesis_execution_spec_goal': hypotheses_visibility['selected_hypothesis_execution_spec_goal'],
                'selected_hypothesis_execution_spec_task': hypotheses_visibility['selected_hypothesis_execution_spec_task'],
                'selected_hypothesis_execution_spec_acceptance': hypotheses_visibility['selected_hypothesis_execution_spec_acceptance'],
                'selected_hypothesis_execution_spec_budget': hypotheses_visibility['selected_hypothesis_execution_spec_budget'],
                'selected_hypothesis_execution_spec_budget_text': hypotheses_visibility['selected_hypothesis_execution_spec_budget_text'],
                'research_feed': hypotheses_visibility['research_feed'],
                'top_entries': hypotheses_visibility['top_entries'],
                'candidate_files': hypotheses_visibility['candidate_files'],
                'state_roots': hypotheses_visibility['state_roots'],
                'empty_state_reason': hypotheses_visibility['empty_state_reason'],
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/cycles':
            body = json.dumps({'items': cycles}, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/promotions':
            body = json.dumps({'items': promotions}, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/approvals':
            payload = {
                'items': [
                    {**dict(r), 'plan_snapshot': _plan_snapshot_from_row(r)}
                    for r in (eeepc_rows + repo_rows)
                ],
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/deployments':
            payload = {
                'eeepc_latest': _deployment_snapshot(eeepc_latest, eeepc_plan_snapshot),
                'repo_latest': _deployment_snapshot(repo_latest, repo_plan_snapshot),
                'eeepc_latest_observation': _compact_observation_group(eeepc_latest_observation) if eeepc_latest_observation else None,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/deployments/debug':
            payload = {
                'eeepc_latest': {**dict(eeepc_latest), 'plan_snapshot': eeepc_plan_snapshot} if eeepc_latest else None,
                'repo_latest': {**dict(repo_latest), 'plan_snapshot': repo_plan_snapshot} if repo_latest else None,
                'eeepc_latest_observation': eeepc_latest_observation,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/analytics':
            body = json.dumps({'analytics': analytics, 'current_blocker': current_blocker}, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/system':
            payload = {
                'eeepc_goal': system_visibility['eeepc_goal'],
                'eeepc_status': system_visibility['eeepc_status'],
                'repo_goal': system_visibility['repo_goal'],
                'repo_status': system_visibility['repo_status'],
                'eeepc_files': system_visibility['eeepc_files'],
                'local_files': system_visibility['local_files'],
                'eeepc_outbox_preview': system_visibility['eeepc_outbox_preview'],
                'control_plane': control_plane,
                'host_resources': dict(repo_latest).get('host_resources') if repo_latest else None,
                'host_resources': (control_plane.get('host_resources') if isinstance(control_plane, dict) else None),
                'capabilities': control_plane.get('capabilities'),
                'runtime_source': control_plane.get('runtime_source'),
                'eeepc_reachability': eeepc_reachability,
                'eeepc_reachability_age': eeepc_reachability_age,
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
        elif path == '/experiments':
            template = env.get_template('experiments.html')
        elif path == '/credits':
            template = env.get_template('credits.html')
        elif path == '/system':
            template = env.get_template('system.html')
        elif path == '/subagents':
            template = env.get_template('subagents.html')
        elif path == '/plan':
            template = env.get_template('plan.html')
        elif path == '/hypotheses':
            template = env.get_template('hypotheses.html')
        else:
            template = env.get_template('index.html')

        body = template.render(**context).encode('utf-8')
        start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
        return [body]

    return app
