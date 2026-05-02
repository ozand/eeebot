from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from wsgiref.util import setup_testing_defaults
from urllib.parse import parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .collector import collect_once, _build_ssh_command
from .config import DashboardConfig
from .storage import count_collections, count_events, fetch_events, fetch_latest_collections
from nanobot.runtime.state import _subagent_rollup_snapshot


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


def _missing_record(value) -> bool:
    if not _has_value(value):
        return True
    if isinstance(value, str) and value.strip().lower() in {'missing', 'none', 'null', 'not_present', 'absent'}:
        return True
    return False


def _promotion_replay_readiness_from_promotions(promotions: list[dict] | None) -> dict | None:
    if not promotions:
        return None
    for row in promotions:
        detail = row.get('detail') if isinstance(row.get('detail'), dict) else _json_loads_dict(row.get('detail_json'))
        if not isinstance(detail, dict):
            detail = {}
        governance = detail.get('governance_packet') if isinstance(detail.get('governance_packet'), dict) else {}
        decision_record = detail.get('decision_record')
        accepted_record = detail.get('accepted_record')
        review_status = governance.get('review_status') or detail.get('review_status') or row.get('status')
        decision = governance.get('decision') or detail.get('decision') or row.get('status')
        review_packet_status = governance.get('review_packet_status') or detail.get('review_packet_status')
        replay_state = row.get('replay_readiness') or detail.get('replay_readiness')
        explicitly_not_ready = review_packet_status == 'not_ready' or review_status == 'not_ready_for_policy_review' or decision == 'not_ready_for_policy_review'
        packet_blocked_not_ready = review_packet_status == 'blocked_not_ready' or decision_record == 'blocked_not_ready' or accepted_record == 'not_created_not_ready'
        readiness_checks = detail.get('readiness_checks') or detail.get('readinessChecks')
        readiness_reasons = detail.get('readiness_reasons') or detail.get('readinessReasons') or []
        missing_records = [name for name, value in {'decision_record': decision_record, 'accepted_record': accepted_record}.items() if _missing_record(value)]
        ready_for_policy_review = (
            review_status == 'ready_for_policy_review'
            or decision == 'ready_for_policy_review'
            or review_packet_status == 'pending_operator_review'
            or decision_record == 'pending_operator_review_packet'
            or row.get('status') == 'ready_for_policy_review'
        )
        if ready_for_policy_review:
            return {
                'schema_version': 'promotion-replay-readiness-v1',
                'state': 'ready_for_policy_review',
                'reason': 'promotion_candidate_awaiting_policy_review',
                'promotion_id': row.get('identity_key') or row.get('title'),
                'status': row.get('status'),
                'review_status': review_status,
                'decision': decision,
                'review_packet_status': review_packet_status or 'pending_operator_review',
                'decision_record': decision_record,
                'accepted_record': accepted_record,
                'missing_records': [name for name in missing_records if name != 'accepted_record'],
                'readiness_checks': readiness_checks,
                'readiness_reasons': readiness_reasons,
                'recommended_next_action': detail.get('recommended_next_action') or 'review_promotion_candidate',
                'readiness_packet_path': detail.get('readiness_packet_path') or governance.get('readiness_packet_path'),
                'candidate_path': detail.get('candidate_path'),
                'artifact_path': detail.get('artifact_path'),
                'collected_at': row.get('collected_at'),
            }
        if explicitly_not_ready:
            return {
                'schema_version': 'promotion-replay-readiness-v1',
                'state': 'blocked' if packet_blocked_not_ready else 'not_ready',
                'reason': 'promotion_candidate_not_ready_for_policy_review',
                'promotion_id': row.get('identity_key') or row.get('title'),
                'status': row.get('status'),
                'review_status': review_status,
                'decision': decision,
                'review_packet_status': review_packet_status or ('blocked_not_ready' if packet_blocked_not_ready else 'not_ready'),
                'decision_record': decision_record,
                'accepted_record': accepted_record,
                'missing_records': [] if packet_blocked_not_ready else missing_records,
                'readiness_checks': readiness_checks,
                'readiness_reasons': readiness_reasons,
                'recommended_next_action': detail.get('recommended_next_action') or ('supply_missing_promotion_readiness_inputs' if packet_blocked_not_ready else 'complete_promotion_readiness_packet'),
                'readiness_packet_path': detail.get('readiness_packet_path') or governance.get('readiness_packet_path'),
                'candidate_path': detail.get('candidate_path'),
                'artifact_path': detail.get('artifact_path'),
                'collected_at': row.get('collected_at'),
            }
        pending_or_missing = (
            review_status == 'pending_policy_review'
            or decision == 'pending_policy_review'
            or _missing_record(decision_record)
            or _missing_record(accepted_record)
        )
        if replay_state == 'blocked' or pending_or_missing:
            blocked_reason = 'pending_policy_review_or_missing_records' if pending_or_missing else 'promotion_replay_not_ready'
            return {
                'schema_version': 'promotion-replay-readiness-v1',
                'state': 'blocked' if pending_or_missing else str(replay_state or 'unknown'),
                'reason': blocked_reason,
                'promotion_id': row.get('identity_key') or row.get('title'),
                'status': row.get('status'),
                'review_status': review_status,
                'decision': decision,
                'decision_record': decision_record,
                'accepted_record': accepted_record,
                'missing_records': missing_records,
                'readiness_checks': readiness_checks,
                'readiness_reasons': readiness_reasons,
                'recommended_next_action': 'review_promotion_candidate' if pending_or_missing else 'resolve_promotion_replay_blocker',
                'candidate_path': detail.get('candidate_path'),
                'artifact_path': detail.get('artifact_path'),
                'collected_at': row.get('collected_at'),
            }
    return {'schema_version': 'promotion-replay-readiness-v1', 'state': 'ready', 'reason': 'no_blocked_promotions'}


def _promotion_source_commit_blocker_resolved(promotion_readiness: dict | None) -> bool:
    if not isinstance(promotion_readiness, dict):
        return False
    checks = promotion_readiness.get('readiness_checks') if isinstance(promotion_readiness.get('readiness_checks'), dict) else {}
    missing_inputs = checks.get('missing_inputs') if isinstance(checks.get('missing_inputs'), list) else []
    readiness_reasons = promotion_readiness.get('readiness_reasons') if isinstance(promotion_readiness.get('readiness_reasons'), list) else []
    return bool(
        checks.get('provenance_complete') is True
        and 'source_commit' not in {str(item) for item in missing_inputs}
        and 'source_commit_missing' not in {str(item) for item in readiness_reasons}
    )


def _source_commit_blocker(value: dict | None) -> bool:
    if not isinstance(value, dict):
        return False
    markers = {
        value.get('failure_class'),
        value.get('reason'),
        value.get('blocked_next_step'),
        value.get('recommended_next_action'),
    }
    return any(str(item) in {'source_commit_missing', 'supply_source_commit_or_policy_override'} for item in markers if item is not None)


def _demote_resolved_source_commit_blocker(current_blocker: dict | None, control_plane: dict | None, promotion_readiness: dict | None) -> tuple[dict | None, dict | None]:
    if not _promotion_source_commit_blocker_resolved(promotion_readiness):
        return current_blocker, control_plane
    next_action = promotion_readiness.get('recommended_next_action') or 'review_promotion_candidate'
    blocker = dict(current_blocker) if isinstance(current_blocker, dict) else current_blocker
    if _source_commit_blocker(blocker):
        blocker = dict(blocker)
        blocker.update({
            'kind': 'unknown',
            'failure_class': None,
            'reason': 'none',
            'blocked_next_step': next_action,
            'recommended_next_action': next_action,
            'resolved_stale_blocker': 'source_commit_missing',
            'resolution_source': 'promotion_replay_readiness',
        })
    plane = dict(control_plane) if isinstance(control_plane, dict) else control_plane
    if isinstance(plane, dict):
        control_blocker = plane.get('current_blocker')
        if _source_commit_blocker(control_blocker):
            control_blocker = dict(control_blocker)
            control_blocker.update({
                'kind': 'unknown',
                'failure_class': None,
                'reason': 'none',
                'blocked_next_step': next_action,
                'recommended_next_action': next_action,
                'resolved_stale_blocker': 'source_commit_missing',
                'resolution_source': 'promotion_replay_readiness',
            })
            plane['current_blocker'] = control_blocker
        blocker_summary = plane.get('blocker_summary')
        if _source_commit_blocker(blocker_summary):
            blocker_summary = dict(blocker_summary)
            blocker_summary.update({
                'state': 'clear',
                'reason': 'none',
                'recommended_next_action': next_action,
                'resolved_stale_blocker': 'source_commit_missing',
                'resolution_source': 'promotion_replay_readiness',
            })
            plane['blocker_summary'] = blocker_summary
    return blocker, plane


def _env(cfg: DashboardConfig) -> Environment:
    templates = cfg.project_root / 'src' / 'nanobot_ops_dashboard' / 'templates'
    return Environment(
        loader=FileSystemLoader(str(templates)),
        autoescape=select_autoescape(['html', 'xml']),
    )


def _json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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


def _compact_selfevo_issue_reference(value: dict | None) -> dict | None:
    if not isinstance(value, dict):
        return None
    nested_issue = value.get('selfevo_issue') if isinstance(value.get('selfevo_issue'), dict) else {}
    pr = value.get('pr') if isinstance(value.get('pr'), dict) else {}
    compact = {
        'number': value.get('number') or value.get('issue_number') or nested_issue.get('number') or nested_issue.get('issue_number'),
        'title': value.get('title') or value.get('issue_title') or nested_issue.get('title') or nested_issue.get('issue_title'),
        'url': value.get('url') or value.get('issue_url') or nested_issue.get('url') or nested_issue.get('issue_url'),
        'state': value.get('state') or value.get('github_issue_state') or nested_issue.get('state') or nested_issue.get('github_issue_state'),
        'terminal_status': value.get('terminal_status') or value.get('status') or nested_issue.get('terminal_status') or nested_issue.get('status'),
        'retry_allowed': value.get('retry_allowed') if 'retry_allowed' in value else nested_issue.get('retry_allowed'),
        'branch': value.get('selfevo_branch') or value.get('branch') or nested_issue.get('selfevo_branch') or nested_issue.get('branch'),
        'pr_number': value.get('pr_number') or pr.get('number') or nested_issue.get('pr_number'),
        'pr_url': value.get('pr_url') or pr.get('url') or nested_issue.get('pr_url'),
    }
    return {key: compact_value for key, compact_value in compact.items() if _has_value(compact_value)}


def _compact_selfevo_lifecycle_evidence(value):
    if isinstance(value, list):
        return [_compact_selfevo_lifecycle_evidence(item) for item in value[:20]]
    if not isinstance(value, dict):
        return value
    compact: dict = {}
    for key, item in value.items():
        if key == 'selfevo_issue':
            issue = _compact_selfevo_issue_reference(item if isinstance(item, dict) else value)
            if issue:
                compact[key] = issue
            continue
        if key in {'last_issue_lifecycle', 'terminal_selfevo_issue'} and isinstance(item, dict):
            lifecycle = _compact_selfevo_lifecycle_evidence(item)
            if isinstance(lifecycle, dict):
                compact_lifecycle = {k: lifecycle.get(k) for k in ('status', 'issue_number', 'issue_title', 'issue_url', 'pr_number', 'pr_url', 'selfevo_branch', 'github_issue_state', 'retry_allowed', 'selfevo_issue') if _has_value(lifecycle.get(k))}
                pr = item.get('pr') if isinstance(item.get('pr'), dict) else {}
                if _has_value(pr.get('number')) and not _has_value(compact_lifecycle.get('pr_number')):
                    compact_lifecycle['pr_number'] = pr.get('number')
                if _has_value(pr.get('url')) and not _has_value(compact_lifecycle.get('pr_url')):
                    compact_lifecycle['pr_url'] = pr.get('url')
                compact[key] = compact_lifecycle
            continue
        if key in {'raw_json', 'stdout', 'stderr', 'stdout_tail', 'stderr_tail'}:
            if isinstance(item, str) and len(item) > 500:
                compact[key] = item[:500] + '…'
            else:
                compact[key] = item
            continue
        if isinstance(item, dict):
            compact[key] = _compact_selfevo_lifecycle_evidence(item)
        elif isinstance(item, list):
            compact[key] = _compact_selfevo_lifecycle_evidence(item)
        else:
            compact[key] = item
    return compact


def _material_progress_summary(material_progress: dict | None) -> dict:
    material_progress = dict(material_progress) if isinstance(material_progress, dict) else {}
    if not material_progress:
        return {
            'schema_version': 'material-progress-v1',
            'state': 'unavailable',
            'available': False,
            'reason': 'material_progress_unavailable',
            'healthy_autonomy_allowed': False,
            'proof_count': 0,
            'proofs': [],
            'qualifying_proofs': [],
            'blocking_reason': 'material_progress_unavailable',
        }
    material_progress = _compact_selfevo_lifecycle_evidence(material_progress)
    material_progress.setdefault('schema_version', 'material-progress-v1')
    material_progress.setdefault('available', True)
    return material_progress


def _reconcile_material_progress_with_subagent_visibility(material_progress: dict | None, subagent_visibility: dict | None) -> dict:
    material = _material_progress_summary(material_progress)
    visibility = dict(subagent_visibility) if isinstance(subagent_visibility, dict) else {}
    source = visibility.get('source') if isinstance(visibility.get('source'), dict) else {}
    latest_result = visibility.get('latest_result') if isinstance(visibility.get('latest_result'), dict) else {}
    latest_request = visibility.get('latest_request') if isinstance(visibility.get('latest_request'), dict) else {}
    selected_source = source.get('selected') or latest_result.get('source') or latest_request.get('source')
    request_id = latest_result.get('request_id') or latest_request.get('request_id')
    if selected_source != 'eeepc' or not request_id or not latest_result:
        return material
    result_status = str(latest_result.get('status') or latest_result.get('result_status') or '').lower()
    terminal_reason = latest_result.get('terminal_reason') or latest_result.get('reason')
    evidence = {
        'source': latest_result.get('source') or selected_source,
        'source_root': latest_result.get('source_root') or source.get('state_root'),
        'latest_result_path': latest_result.get('path'),
        'request_path': latest_result.get('request_path') or latest_request.get('path'),
        'request_id': request_id,
        'semantic_task_id': latest_result.get('semantic_task_id') or latest_request.get('semantic_task_id'),
        'verification_task_id': latest_result.get('verification_task_id') or latest_request.get('verification_task_id') or request_id,
        'verification_role': latest_result.get('verification_role') or latest_request.get('verification_role'),
        'status': latest_result.get('status') or latest_result.get('result_status'),
        'terminal_reason': terminal_reason,
        'recommended_next_action': latest_result.get('recommended_next_action'),
        'blocker': latest_result.get('blocker') if isinstance(latest_result.get('blocker'), dict) else None,
        'source_artifact': latest_result.get('source_artifact') or latest_request.get('source_artifact'),
    }
    if result_status == 'blocked':
        reason = 'subagent_result_terminal_blocked'
        material['blocking_reason'] = 'delegated_verification_terminal_blocked'
    elif result_status in {'completed', 'pass', 'passed', 'success'}:
        reason = 'subagent_result_available'
    else:
        reason = 'subagent_result_present'
    replacement = {
        'kind': 'consumed_subagent_result',
        'present': True,
        'reason': reason,
        'evidence': evidence,
    }
    proofs = material.get('proofs') if isinstance(material.get('proofs'), list) else []
    replaced = False
    reconciled_proofs = []
    for proof in proofs:
        if isinstance(proof, dict) and proof.get('kind') == 'consumed_subagent_result':
            reconciled_proofs.append(replacement)
            replaced = True
        else:
            reconciled_proofs.append(proof)
    if not replaced:
        reconciled_proofs.append(replacement)
    material['proofs'] = reconciled_proofs
    if result_status in {'completed', 'pass', 'passed', 'success'}:
        qualifying = material.get('qualifying_proofs') if isinstance(material.get('qualifying_proofs'), list) else []
        if not any(isinstance(proof, dict) and proof.get('kind') == 'consumed_subagent_result' for proof in qualifying):
            qualifying = list(qualifying) + [replacement]
        material['qualifying_proofs'] = qualifying
        material['proof_count'] = len(qualifying)
        material['healthy_autonomy_allowed'] = True
        if material.get('blocking_reason') == 'missing_current_material_progress':
            material['blocking_reason'] = None
    else:
        material['qualifying_proofs'] = material.get('qualifying_proofs') if isinstance(material.get('qualifying_proofs'), list) else []
        material['proof_count'] = len(material['qualifying_proofs'])
        material['healthy_autonomy_allowed'] = False
    return material


def _mission_control_summary(*, context: dict, control_plane: dict | None, current_blocker: dict | None, material_progress: dict | None, runtime_parity: dict | None, autonomy_verdict: dict | None, hypotheses_visibility: dict | None, experiment_visibility: dict | None, subagent_visibility: dict | None, analytics: dict | None) -> dict:
    control_plane = dict(control_plane) if isinstance(control_plane, dict) else {}
    current_blocker = dict(current_blocker) if isinstance(current_blocker, dict) else {}
    material = _material_progress_summary(material_progress)
    runtime_parity = dict(runtime_parity) if isinstance(runtime_parity, dict) else {}
    autonomy = dict(autonomy_verdict) if isinstance(autonomy_verdict, dict) else {}
    hypotheses = dict(hypotheses_visibility) if isinstance(hypotheses_visibility, dict) else {}
    experiment = dict(experiment_visibility) if isinstance(experiment_visibility, dict) else {}
    subagents = dict(subagent_visibility) if isinstance(subagent_visibility, dict) else {}
    analytics = dict(analytics) if isinstance(analytics, dict) else {}

    visible_plan = context.get('plan_latest') if isinstance(context.get('plan_latest'), dict) else {}
    hypothesis_selected = context.get('hypothesis_selected') if isinstance(context.get('hypothesis_selected'), dict) else {}
    selected_hadi = hypothesis_selected.get('hadi') if isinstance(hypothesis_selected.get('hadi'), dict) else {}
    current_experiment = experiment.get('current_experiment') if isinstance(experiment.get('current_experiment'), dict) else {}

    task_id = control_plane.get('current_task_id') or visible_plan.get('current_task_id')
    task_title = control_plane.get('current_task_title') or control_plane.get('current_task') or visible_plan.get('current_task') or current_blocker.get('selected_task_title') or current_blocker.get('current_task_title')
    hypothesis_id = hypothesis_selected.get('id') or hypotheses.get('selected_hypothesis_id')
    hypothesis_title = hypothesis_selected.get('title') or hypotheses.get('selected_hypothesis_title')

    autonomy_blocking_summary = autonomy.get('blocking_summary') if isinstance(autonomy.get('blocking_summary'), dict) else {}
    control_blocking_summary = control_plane.get('blocker_summary') if isinstance(control_plane.get('blocker_summary'), dict) else {}
    blocker_reason = current_blocker.get('failure_class') or current_blocker.get('kind') or current_blocker.get('reason')
    blocker_source = current_blocker.get('source') or 'unknown'
    if not blocker_reason or blocker_reason in {'unknown', 'none', 'clear'}:
        readiness_reasons = autonomy_blocking_summary.get('readiness_reasons') if isinstance(autonomy_blocking_summary.get('readiness_reasons'), list) else []
        missing_records = autonomy_blocking_summary.get('missing_records') if isinstance(autonomy_blocking_summary.get('missing_records'), list) else []
        concrete_readiness_reasons = [str(reason) for reason in readiness_reasons if _has_value(reason) and str(reason).lower() not in {'unknown', 'none', 'clear'}]
        concrete_missing_records = [str(record) for record in missing_records if _has_value(record) and str(record).lower() not in {'unknown', 'none', 'clear'}]
        blocker_reason = next(iter(concrete_readiness_reasons), None)
        blocker_reason = blocker_reason or next(iter(concrete_missing_records), None)
        blocker_reason = blocker_reason or autonomy_blocking_summary.get('reason') or control_blocking_summary.get('reason') or blocker_reason
        if blocker_reason and blocker_reason not in {'unknown', 'none', 'clear'}:
            blocker_source = autonomy_blocking_summary.get('source') or control_blocking_summary.get('source') or blocker_source
    next_action_label = (
        current_blocker.get('blocked_next_step')
        or current_blocker.get('recommended_next_action')
        or autonomy_blocking_summary.get('recommended_next_action')
        or autonomy.get('recommended_next_action')
        or control_blocking_summary.get('recommended_next_action')
    )
    if not next_action_label:
        next_action_label = 'inspect canonical state and continue the next bounded self-improvement cycle'

    blocker_state = 'none'
    if blocker_reason and blocker_reason not in {'unknown', 'none', 'clear'}:
        blocker_state = 'blocked'
    elif autonomy.get('state') in {'blocked', 'stagnant', 'degraded'}:
        blocker_state = autonomy.get('state')

    material_state = material.get('state')
    if not material_state:
        material_state = 'available' if material.get('healthy_autonomy_allowed') or material.get('proof_count') else 'missing'
    elif material_state in {'unavailable', 'missing_current_material_progress'}:
        material_state = 'missing'

    latest_result = subagents.get('latest_result') if isinstance(subagents.get('latest_result'), dict) else {}
    latest_request = subagents.get('latest_request') if isinstance(subagents.get('latest_request'), dict) else {}
    latest_sub_status = (latest_result.get('status') or latest_result.get('result_status') or latest_request.get('request_status') or 'unknown')
    latest_sub_status_key = str(latest_sub_status).lower()
    if latest_result:
        if latest_sub_status_key in {'completed', 'complete', 'pass', 'passed', 'success', 'ok', 'done'}:
            subagent_state = 'completed'
        elif latest_sub_status_key in {'blocked', 'failed', 'failure', 'error', 'crash'}:
            subagent_state = 'blocked'
        else:
            subagent_state = 'unknown'
    elif latest_request:
        subagent_state = 'requested'
    else:
        subagent_state = 'none'
    latest_consumed = False
    latest_consumed_as_blocker_evidence = False
    terminal_subagent_statuses = {'blocked', 'failed', 'failure', 'error', 'crash'}
    successful_subagent_statuses = {'completed', 'complete', 'pass', 'passed', 'success', 'ok', 'done'}
    for proof in material.get('qualifying_proofs') or material.get('proofs') or []:
        if not isinstance(proof, dict) or proof.get('kind') != 'consumed_subagent_result':
            continue
        proof_status = str(((proof.get('evidence') if isinstance(proof.get('evidence'), dict) else {}) or {}).get('status') or latest_sub_status_key).lower()
        if proof_status in terminal_subagent_statuses:
            latest_consumed_as_blocker_evidence = True
            continue
        if latest_sub_status_key in successful_subagent_statuses or proof_status in successful_subagent_statuses:
            latest_consumed = True
            break
    if latest_sub_status_key in terminal_subagent_statuses:
        latest_consumed_as_blocker_evidence = True
        latest_consumed = False

    parity_state = runtime_parity.get('state') or 'unknown'
    raw_authority_resolution = runtime_parity.get('authority_resolution') or runtime_parity.get('runtime_authority_resolution') or control_plane.get('runtime_authority_resolution')
    source_skew_payload = runtime_parity.get('source_skew')
    source_skew_reasons = []
    if isinstance(source_skew_payload, dict):
        source_skew = source_skew_payload.get('state') == 'skewed' or bool(source_skew_payload.get('reasons'))
        source_skew_reasons = [str(reason) for reason in source_skew_payload.get('reasons') or [] if _has_value(reason)]
    else:
        source_skew = bool(source_skew_payload or runtime_parity.get('source_skew_detected') or runtime_parity.get('source_skew_reasons'))
        source_skew_reasons = [str(reason) for reason in runtime_parity.get('source_skew_reasons') or [] if _has_value(reason)] if isinstance(runtime_parity.get('source_skew_reasons'), list) else []
    task_ids = [runtime_parity.get('canonical_current_task_id'), runtime_parity.get('local_current_task_id'), runtime_parity.get('live_current_task_id')]
    present_task_ids = [str(value) for value in task_ids if _has_value(value)]
    task_ids_match = len(present_task_ids) >= 2 and len(set(present_task_ids)) == 1
    authority_resolution = raw_authority_resolution or ('ids_match_no_resolution_needed' if source_skew and task_ids_match else 'unknown')
    source_skew_reason = None
    if source_skew:
        source_skew_reason = source_skew_reasons[0] if source_skew_reasons else ('metadata_or_timestamp_skew_only' if task_ids_match else 'unexplained_source_skew')
    canonical_source = 'eeepc' if context.get('eeepc_latest') else 'repo' if context.get('repo_latest') else 'unknown'
    freshness = 'fresh'
    if str(context.get('latest_collected_age') or '').lower() in {'unknown', 'never'}:
        freshness = 'unknown'
    if authority_resolution in {'authority_resolved_with_source_skew', 'source_skew'} or source_skew:
        freshness = 'stale' if not context.get('eeepc_latest') else 'fresh_with_skew'

    hadi_stage = 'unknown'
    if blocker_state not in {'none', 'unknown'}:
        hadi_stage = 'blocked'
    elif selected_hadi.get('insights') or selected_hadi.get('insight') or (current_experiment and current_experiment.get('outcome')):
        hadi_stage = 'insight'
    elif selected_hadi.get('data') or current_experiment:
        hadi_stage = 'data'
    elif selected_hadi.get('action') or task_title:
        hadi_stage = 'action'
    elif selected_hadi.get('hypothesis') or hypothesis_id:
        hadi_stage = 'hypothesis'
    stage_labels = {
        'hypothesis': 'Hypothesis selected',
        'action': 'Action/task formulated',
        'data': 'Data/proof collection',
        'insight': 'Insight/decision',
        'follow_up': 'Follow-up selected',
        'blocked': 'Blocked with next action',
        'unknown': 'Stage unknown',
    }

    headline = str(autonomy.get('state') or 'unknown').replace('_', ' ')
    if blocker_state not in {'none', 'unknown'}:
        headline = f"Blocked: {blocker_reason or 'next action required'}"
    elif material_state == 'available':
        headline = 'Healthy progress: material proof is available'

    timeline: list[dict] = []
    if hypothesis_id or hypothesis_title:
        timeline.append({'kind': 'hypothesis', 'title': hypothesis_title or hypothesis_id or 'Selected hypothesis', 'status': hypothesis_selected.get('status') or 'selected', 'timestamp': context.get('latest_collected'), 'source': hypotheses.get('canonical_source') or 'hypothesis_backlog', 'evidence_url': '/api/hypotheses'})
    if task_title or task_id:
        timeline.append({'kind': 'action', 'title': task_title or task_id or 'Current action', 'status': 'current', 'timestamp': visible_plan.get('collected_at') or context.get('latest_collected'), 'source': visible_plan.get('source') or 'plan', 'evidence_url': '/api/plan'})
    if material_state == 'available':
        timeline.append({'kind': 'data', 'title': 'Material progress proof available', 'status': 'available', 'timestamp': context.get('latest_collected'), 'source': 'material_progress', 'evidence_url': '/api/system'})
    if latest_result or latest_request:
        timeline.append({'kind': 'subagent', 'title': latest_result.get('request_id') or latest_request.get('request_id') or 'Subagent verification', 'status': latest_sub_status, 'timestamp': latest_result.get('updated_at') or latest_request.get('created_at') or context.get('latest_collected'), 'source': (subagents.get('source') or {}).get('selected') if isinstance(subagents.get('source'), dict) else 'subagents', 'evidence_url': '/api/subagents'})
    if blocker_state not in {'none', 'unknown'}:
        timeline.append({'kind': 'blocker', 'title': blocker_reason or 'Current blocker', 'status': blocker_state, 'timestamp': context.get('latest_collected'), 'source': blocker_source, 'evidence_url': '/api/system', 'recommended_next_action': next_action_label})

    experiment_history = experiment.get('experiment_history') if isinstance(experiment.get('experiment_history'), list) else []
    discarded_attempts = []
    seen_discarded_keys = set()
    for item in experiment_history:
        if not isinstance(item, dict) or item.get('outcome') != 'discard':
            continue
        dedupe_key = item.get('experiment_id') or item.get('contract_path') or item.get('title') or json.dumps(item, sort_keys=True, default=str)
        if dedupe_key in seen_discarded_keys:
            continue
        seen_discarded_keys.add(dedupe_key)
        discarded_attempts.append({
            'experiment_id': item.get('experiment_id'),
            'title': item.get('title') or item.get('experiment_id') or 'discarded attempt',
            'outcome': item.get('outcome'),
            'revert_status': item.get('revert_status'),
            'revert_reason': item.get('revert_reason'),
            'metric_name': item.get('metric_name'),
            'metric_current': item.get('metric_current'),
            'metric_frontier': item.get('metric_frontier'),
            'collected_at': item.get('collected_at') or item.get('finished_at') or item.get('created_at'),
            'evidence_url': '/api/experiments',
        })
        if len(discarded_attempts) >= 3:
            break
    subagent_learnings = latest_result.get('key_learnings') if isinstance(latest_result.get('key_learnings'), list) else []
    blocker_learnings = []
    subagent_blocker = latest_result.get('blocker') if isinstance(latest_result.get('blocker'), dict) else {}
    subagent_blocker_reason = subagent_blocker.get('reason') or latest_result.get('terminal_reason') or latest_result.get('failure_class')
    if blocker_reason and blocker_reason not in {'unknown', 'none', 'clear'}:
        blocker_learnings.append(f"Current blocker is {blocker_reason}; next action is {next_action_label}")
    if subagent_blocker_reason and subagent_blocker_reason not in {'unknown', 'none', 'clear'}:
        blocker_learnings.append(f"Latest subagent blocker is {subagent_blocker_reason}; treat it as blocker evidence, not material progress")
    effective_key_learnings = [str(item) for item in subagent_learnings[:5] if _has_value(item)] or blocker_learnings[:5]
    if subagent_learnings:
        last_learning_summary = str(subagent_learnings[0])
        last_learning_source = 'subagent_result'
        last_learning_evidence = '/api/subagents'
    elif discarded_attempts:
        latest_discard = discarded_attempts[0]
        reason = latest_discard.get('revert_reason') or latest_discard.get('revert_status') or latest_discard.get('outcome')
        last_learning_summary = f"Discarded {latest_discard.get('title')}: {reason}"
        last_learning_source = 'discarded_experiment'
        last_learning_evidence = '/api/experiments'
    elif blocker_reason:
        last_learning_summary = f"Current blocker remains {blocker_reason}; next cycle should execute {next_action_label}"
        last_learning_source = 'current_blocker'
        last_learning_evidence = '/api/system'
    else:
        last_learning_summary = 'No explicit learning event has been recorded yet.'
        last_learning_source = 'none'
        last_learning_evidence = '/api/mission-control'
    learning_loop = {
        'last_learning': {
            'summary': last_learning_summary,
            'source': last_learning_source,
            'collected_at': context.get('latest_collected'),
            'evidence_url': last_learning_evidence,
            'key_learnings': effective_key_learnings,
        },
        'discarded_attempts': discarded_attempts,
    }

    return {
        'schema_version': 'mission-control-v1',
        'autonomy_state': autonomy.get('state') or 'unknown',
        'headline': headline,
        'current_improvement': {
            'task_id': task_id,
            'task_title': task_title or 'unknown',
            'hypothesis_id': hypothesis_id,
            'hypothesis_title': hypothesis_title,
            'why_selected': current_blocker.get('task_selection_source') or visible_plan.get('task_selection_source') or hypothesis_selected.get('status') or 'unknown',
            'wsjf': hypothesis_selected.get('wsjf_text') or hypothesis_selected.get('wsjf') or hypotheses.get('selected_hypothesis_wsjf_text'),
        },
        'hadi': {
            'stage': hadi_stage,
            'stage_label': stage_labels[hadi_stage],
            'hypothesis': {'label': selected_hadi.get('hypothesis') or hypothesis_title or hypothesis_id or 'unknown'},
            'action': {'label': selected_hadi.get('action') or task_title or 'unknown'},
            'data': {'label': selected_hadi.get('data') or current_experiment.get('result_status') or current_experiment.get('status') or material.get('reason') or 'unknown'},
            'insight': {'label': selected_hadi.get('insights') or selected_hadi.get('insight') or current_experiment.get('outcome') or 'unknown'},
            'follow_up': {'label': next_action_label},
        },
        'last_material_progress': {
            'state': material_state,
            'canonical_state': material.get('state') or material_state,
            'available': material_state in {'available', 'proven'},
            'reason': material.get('blocking_reason') or material.get('reason'),
            'blocking_reason': material.get('blocking_reason') or material.get('reason'),
            'source_reason': material.get('reason'),
            'reason_mismatch': bool(material.get('reason') and material.get('blocking_reason') and material.get('reason') != material.get('blocking_reason')),
            'proof_count': material.get('proof_count') or 0,
            'qualifying_proofs': material.get('qualifying_proofs') or [],
            'healthy_autonomy_allowed': bool(material.get('healthy_autonomy_allowed')),
        },
        'current_blocker': {
            'state': blocker_state,
            'reason': blocker_reason or 'none',
            'failure_class': current_blocker.get('failure_class') or blocker_reason,
            'blocked_next_step': current_blocker.get('blocked_next_step') or next_action_label,
            'improvement_score': current_blocker.get('improvement_score'),
            'feedback_decision': current_blocker.get('feedback_decision') if isinstance(current_blocker.get('feedback_decision'), dict) else {},
            'selected_tasks_text': current_blocker.get('selected_tasks_text'),
            'selected_task_title': current_blocker.get('selected_task_title'),
            'task_selection_source': current_blocker.get('task_selection_source'),
            'recommended_next_action': next_action_label,
            'source': blocker_source,
        },
        'truth_status': {
            'canonical_source': canonical_source,
            'freshness': freshness,
            'runtime_parity_state': parity_state,
            'authority_resolution': authority_resolution,
            'source_skew': source_skew,
            'source_skew_reason': source_skew_reason,
            'source_skew_reasons': source_skew_reasons,
        },
        'subagents': {
            'state': subagent_state,
            'latest_status': latest_sub_status,
            'latest_consumed_as_material_progress': latest_consumed,
            'latest_consumed_as_blocker_evidence': latest_consumed_as_blocker_evidence,
            'latest_request_id': latest_result.get('request_id') or latest_request.get('request_id'),
            'recommended_next_action': latest_result.get('recommended_next_action') or next_action_label,
        },
        'learning_loop': learning_loop,
        'next_action': {
            'label': next_action_label,
            'source': current_blocker.get('source') or autonomy.get('source') or 'mission_control',
            'issue_url': None,
        },
        'process_timeline': timeline,
        'debug_links': {
            'system': '/api/system',
            'plan': '/api/plan',
            'hypotheses': '/api/hypotheses',
            'experiments': '/api/experiments',
            'subagents': '/api/subagents',
            'mission_control': '/api/mission-control',
        },
    }


def _task_plan_truth(task_plan: dict | None) -> dict:
    task_plan = dict(task_plan) if isinstance(task_plan, dict) else {}
    current_task_id = _first_present(task_plan, ('current_task_id', 'currentTaskId'))
    selected_tasks = _first_present(task_plan, ('selected_tasks', 'selectedTasks'))
    if not _has_value(current_task_id):
        current_task_id = _selected_task_id(selected_tasks)
    current_task = _first_present(task_plan, ('current_task', 'currentTask')) or current_task_id
    task_selection_source = _first_present(task_plan, ('task_selection_source', 'taskSelectionSource', 'selection_source', 'selectionSource'))
    selected_task_title = _first_present(task_plan, ('selected_task_title', 'selectedTaskTitle', 'selected_task_label', 'selectedTaskLabel')) or current_task
    if not _has_value(selected_tasks) and _has_value(current_task):
        selected_tasks = current_task
    return {
        'current_task_id': current_task_id,
        'current_task': current_task,
        'selected_tasks': selected_tasks,
        'selected_tasks_text': _selected_tasks_text(selected_tasks),
        'selected_task_title': selected_task_title,
        'task_selection_source': task_selection_source,
        'task_plan': task_plan,
    }


def _canonicalize_current_blocker(current_blocker, producer_summary):
    blocker = dict(current_blocker) if isinstance(current_blocker, dict) else {}
    task_truth = _task_plan_truth(producer_summary.get('task_plan') if isinstance(producer_summary, dict) else None)
    canonical_task_id = task_truth.get('current_task_id')
    canonical_task = task_truth.get('current_task')
    canonical_selected_tasks = task_truth.get('selected_tasks')
    canonical_selected_tasks_text = task_truth.get('selected_tasks_text')
    canonical_selected_task_title = task_truth.get('selected_task_title')
    canonical_task_selection_source = task_truth.get('task_selection_source')
    if not any(_has_value(value) for value in (canonical_task_id, canonical_task, canonical_selected_tasks, canonical_selected_task_title, canonical_task_selection_source)):
        return blocker

    original_selected_tasks = blocker.get('selected_tasks')
    original_selected_tasks_text = blocker.get('selected_tasks_text')
    original_selected_task_title = blocker.get('selected_task_title')
    original_task_selection_source = blocker.get('task_selection_source')

    if _has_value(canonical_task_id):
        blocker['current_task_id'] = canonical_task_id
    if _has_value(canonical_task):
        blocker['current_task'] = canonical_task
        blocker['current_task_title'] = canonical_task
    if _has_value(canonical_selected_tasks):
        blocker['selected_tasks'] = canonical_selected_tasks
        blocker['selected_tasks_text'] = canonical_selected_tasks_text
    elif _has_value(canonical_task):
        blocker['selected_tasks'] = canonical_task
        blocker['selected_tasks_text'] = _selected_tasks_text(canonical_task)
    if _has_value(canonical_selected_task_title):
        blocker['selected_task_title'] = canonical_selected_task_title
    elif _has_value(canonical_task):
        blocker['selected_task_title'] = canonical_task
    if _has_value(canonical_task_selection_source):
        blocker['task_selection_source'] = canonical_task_selection_source
    blocker['task_truth_source'] = 'producer_summary.task_plan'

    stale_outbox_fields = {
        'selected_tasks': original_selected_tasks,
        'selected_tasks_text': original_selected_tasks_text,
        'selected_task_title': original_selected_task_title,
        'task_selection_source': original_task_selection_source,
    }
    if any(_has_value(value) and value != blocker.get(key) for key, value in stale_outbox_fields.items()):
        blocker['stale_outbox_selected_tasks'] = original_selected_tasks
        blocker['stale_outbox_selected_tasks_text'] = original_selected_tasks_text or _selected_tasks_text(original_selected_tasks)
        blocker['stale_outbox_selected_task_title'] = original_selected_task_title or _selected_task_title(original_selected_tasks)
        blocker['stale_outbox_task_selection_source'] = original_task_selection_source
        blocker['stale_outbox_is_secondary'] = True
    return blocker


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


def _selfevo_current_proof_summary(cfg, guarded_evolution: dict | None, selfevo_remote_freshness: dict | None) -> dict:
    current_state = dict(guarded_evolution) if isinstance(guarded_evolution, dict) else {}
    state_root = cfg.nanobot_repo_root / 'workspace' / 'state' / 'self_evolution'
    runtime_root = state_root / 'runtime'
    current_state_path = state_root / 'current_state.json'
    latest_issue_lifecycle_path = runtime_root / 'latest_issue_lifecycle.json'
    latest_noop_path = runtime_root / 'latest_noop.json'

    latest_issue_lifecycle = (
        _structured_file_payload(latest_issue_lifecycle_path)
        if latest_issue_lifecycle_path.exists()
        else current_state.get('last_issue_lifecycle')
    )
    latest_noop = (
        _structured_file_payload(latest_noop_path)
        if latest_noop_path.exists()
        else current_state.get('last_noop')
    )
    latest_merge = current_state.get('last_merge') if isinstance(current_state.get('last_merge'), dict) else None
    latest_pr = current_state.get('last_pr') if isinstance(current_state.get('last_pr'), dict) else None

    evidence_paths = [str(path) for path in (current_state_path, latest_issue_lifecycle_path, latest_noop_path) if path.exists()]

    def _git_head(repo_root: Path) -> str | None:
        try:
            result = subprocess.run(
                ['git', '-C', str(repo_root), 'rev-parse', 'HEAD'],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return None
        value = result.stdout.strip()
        return value or None

    def _compact_issue_lifecycle(record: dict | None) -> dict | None:
        if not isinstance(record, dict):
            return None
        issue = record.get('selfevo_issue') if isinstance(record.get('selfevo_issue'), dict) else {}
        pr = record.get('pr') if isinstance(record.get('pr'), dict) else {}
        return {
            'status': record.get('status'),
            'issue_number': record.get('issue_number') or issue.get('number'),
            'issue_title': record.get('issue_title') or issue.get('title'),
            'issue_url': issue.get('url') or record.get('issue_url'),
            'pr_number': record.get('pr_number') or pr.get('number'),
            'pr_url': pr.get('url') or record.get('pr_url'),
            'selfevo_branch': record.get('selfevo_branch'),
            'github_issue_state': record.get('github_issue_state'),
            'linked_issue_action': record.get('linked_issue_action'),
            'retry_allowed': record.get('retry_allowed'),
        }

    def _compact_noop(record: dict | None) -> dict | None:
        if not isinstance(record, dict):
            return None
        export = record.get('export') if isinstance(record.get('export'), dict) else {}
        return {
            'status': record.get('status'),
            'reason': record.get('reason'),
            'selfevo_branch': record.get('selfevo_branch'),
            'publish_repo': record.get('publish_repo'),
            'publish_remote_branch': record.get('publish_remote_branch'),
            'pr_creation_allowed': record.get('pr_creation_allowed'),
            'retry_allowed': record.get('retry_allowed'),
            'export_summary': export.get('summary') or export.get('status') or export.get('stdout_tail') or None,
        }

    def _compact_merge(record: dict | None) -> dict | None:
        if not isinstance(record, dict):
            return None
        return {
            'pr_number': record.get('pr_number'),
            'merged': record.get('merged'),
            'dry_run': record.get('dry_run'),
        }

    def _compact_pr(record: dict | None) -> dict | None:
        if not isinstance(record, dict):
            return None
        return {
            'number': record.get('number'),
            'url': record.get('url'),
            'title': record.get('title'),
            'head_branch': record.get('head_branch') or record.get('headRefName'),
            'base_branch': record.get('base_branch') or record.get('baseRefName'),
            'created': record.get('created'),
            'dry_run': record.get('dry_run'),
        }

    compact_issue_lifecycle = _compact_issue_lifecycle(latest_issue_lifecycle if isinstance(latest_issue_lifecycle, dict) else None)
    compact_noop = _compact_noop(latest_noop if isinstance(latest_noop, dict) else None)
    compact_merge = _compact_merge(latest_merge)
    compact_pr = _compact_pr(latest_pr)
    current_candidate = current_state.get('current_candidate') if isinstance(current_state.get('current_candidate'), dict) else {}
    observed_product_head = current_state.get('observed_product_head') if isinstance(current_state.get('observed_product_head'), dict) else {}
    product_head = _git_head(cfg.nanobot_repo_root)
    current_candidate_commit = current_candidate.get('commit') or current_state.get('current_candidate_commit')
    remote_head = current_state.get('remote_head')
    observed_product_head_commit = observed_product_head.get('commit') or current_state.get('product_head') or current_state.get('observed_product_head_commit')
    state_commit = observed_product_head_commit or current_candidate_commit or remote_head
    state_fresh = bool(product_head and state_commit and product_head == state_commit)
    product_head_freshness = {
        'schema_version': 'selfevo-product-head-freshness-v1',
        'state': 'fresh' if state_fresh else ('unknown' if not product_head or not state_commit else 'stale'),
        'product_head': product_head,
        'observed_product_head_commit': observed_product_head_commit,
        'current_candidate_commit': current_candidate_commit,
        'remote_head': remote_head,
        'state_commit': state_commit,
        'state_fresh': state_fresh,
    }

    evidence_kind = None
    summary = None
    state = 'missing'
    if compact_issue_lifecycle:
        evidence_kind = 'latest_issue_lifecycle'
        state = 'available'
        issue_number = compact_issue_lifecycle.get('issue_number')
        pr_number = compact_issue_lifecycle.get('pr_number')
        branch = compact_issue_lifecycle.get('selfevo_branch')
        status = compact_issue_lifecycle.get('status') or 'unknown'
        summary_bits = [f'latest issue lifecycle {status}']
        if issue_number is not None:
            summary_bits.append(f'issue #{issue_number}')
        if pr_number is not None:
            summary_bits.append(f'PR #{pr_number}')
        if branch:
            summary_bits.append(f'branch {branch}')
        summary = ' / '.join(summary_bits)
    elif compact_noop:
        evidence_kind = 'latest_noop'
        state = 'available'
        branch = compact_noop.get('selfevo_branch')
        status = compact_noop.get('status') or 'unknown'
        summary_bits = [f'latest noop {status}']
        if branch:
            summary_bits.append(f'branch {branch}')
        if compact_noop.get('pr_creation_allowed') is False:
            summary_bits.append('PR creation disabled')
        summary = ' / '.join(summary_bits)
    elif compact_merge:
        evidence_kind = 'latest_merge'
        state = 'available'
        summary_bits = ['latest merge evidence']
        if compact_merge.get('pr_number') is not None:
            summary_bits.append(f'PR #{compact_merge["pr_number"]}')
        if compact_merge.get('merged') is not None:
            summary_bits.append('merged' if compact_merge.get('merged') else 'not merged')
        summary = ' / '.join(summary_bits)
    elif compact_pr:
        evidence_kind = 'latest_pr'
        state = 'available'
        summary_bits = ['latest PR evidence']
        if compact_pr.get('number') is not None:
            summary_bits.append(f'PR #{compact_pr["number"]}')
        if compact_pr.get('head_branch'):
            summary_bits.append(f'branch {compact_pr["head_branch"]}')
        summary = ' / '.join(summary_bits)
    else:
        summary = 'No local selfevo lifecycle or merge evidence found'

    return {
        'schema_version': 'selfevo-current-proof-v1',
        'state': state,
        'mode': 'bounded_local_reader',
        'source': 'local_runtime_artifacts',
        'live_github_api': 'out_of_scope',
        'summary': summary,
        'evidence_kind': evidence_kind,
        'evidence_paths': evidence_paths,
        'latest_issue_lifecycle': compact_issue_lifecycle,
        'latest_noop': compact_noop,
        'latest_merge': compact_merge,
        'latest_pr': compact_pr,
        'remote_freshness': selfevo_remote_freshness,
        'product_head_freshness': product_head_freshness,
    }


def _control_plane_summary(repo_latest, eeepc_latest, current_experiment, current_blocker, cfg):
    repo_latest = dict(repo_latest) if repo_latest else {}
    eeepc_latest = dict(eeepc_latest) if eeepc_latest else {}
    repo_raw = _json_loads_dict(repo_latest.get('raw_json')) if repo_latest else {}
    eeepc_raw = _canonical_report_payload(cfg, _json_loads_dict(eeepc_latest.get('raw_json')) if eeepc_latest else {}, allow_remote=True)
    selfevo_remote_freshness = repo_raw.get('selfevo_remote_freshness') if isinstance(repo_raw, dict) else None
    producer_summary_path = cfg.project_root / 'workspace' / 'state' / 'control_plane' / 'current_summary.json'
    if not producer_summary_path.exists():
        alt_summary_path = cfg.nanobot_repo_root / 'workspace' / 'state' / 'control_plane' / 'current_summary.json'
        producer_summary_path = alt_summary_path if alt_summary_path.exists() else producer_summary_path
    producer_summary = _structured_file_payload(producer_summary_path) if producer_summary_path.exists() else {}
    producer_summary = _canonical_report_payload(cfg, producer_summary) if isinstance(producer_summary, dict) else {}
    guarded_state_path = cfg.nanobot_repo_root / 'workspace' / 'state' / 'self_evolution' / 'current_state.json'
    guarded_evolution = _structured_file_payload(guarded_state_path) if guarded_state_path.exists() else {}
    if isinstance(guarded_evolution, dict) and selfevo_remote_freshness is not None:
        guarded_evolution = dict(guarded_evolution)
        guarded_evolution['remote_ref_freshness'] = selfevo_remote_freshness
    selfevo_current_proof = _selfevo_current_proof_summary(cfg, guarded_evolution, selfevo_remote_freshness)
    current_blocker = _canonicalize_current_blocker(current_blocker, producer_summary)
    local_ci_state_path = cfg.nanobot_repo_root / 'workspace' / 'state' / 'local_ci' / 'current_state.json'
    local_ci = _structured_file_payload(local_ci_state_path) if local_ci_state_path.exists() else {}
    active_exec_path = cfg.project_root / 'control' / 'active_execution.json'
    active_exec = _structured_file_payload(active_exec_path) if active_exec_path.exists() else {}
    if isinstance(active_exec, dict) and active_exec_path.exists():
        active_exec = dict(active_exec)
        try:
            age_seconds = max(0, int(time.time() - active_exec_path.stat().st_mtime))
        except OSError:
            age_seconds = None
        stale_by_age = age_seconds is not None and age_seconds > 3600
        active_exec['staleness'] = {
            'state': 'stale' if stale_by_age else 'fresh',
            'age_seconds': age_seconds,
            'path': str(active_exec_path),
        }
        active_exec['legacy_path_reference_detected'] = 'nanobot-ops-dashboard' in json.dumps(active_exec, ensure_ascii=False)
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
    producer_task_source = (
        producer_summary.get('task_plan') if isinstance(producer_summary, dict) and isinstance(producer_summary.get('task_plan'), dict) else None
        or eeepc_raw.get('task_plan') if isinstance(eeepc_raw, dict) and isinstance(eeepc_raw.get('task_plan'), dict) else None
        or eeepc_raw.get('current_plan') if isinstance(eeepc_raw, dict) and isinstance(eeepc_raw.get('current_plan'), dict) else None
        or eeepc_raw.get('currentPlan') if isinstance(eeepc_raw, dict) and isinstance(eeepc_raw.get('currentPlan'), dict) else None
        or eeepc_raw.get('plan') if isinstance(eeepc_raw, dict) and isinstance(eeepc_raw.get('plan'), dict) else None
    )
    producer_task_truth = _task_plan_truth(producer_task_source)
    producer_current_task = producer_task_truth.get('current_task')
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
    stale_exec = False if completion_terminal else (bool((active_exec or {}).get('stale_execution_detected')) or ((active_exec or {}).get('staleness') or {}).get('state') == 'stale' or (bool(live_task) and not has_executor_linkage))
    live_exec = False if completion_terminal else (bool((active_exec or {}).get('has_actually_executing_task')) and has_executor_linkage and not stale_exec)
    waiting_dispatch = False if completion_terminal else (bool(live_task) and not has_executor_linkage)
    execution_state = 'completed' if completion_terminal else 'stale' if stale_exec else 'live' if live_exec else 'waiting_for_dispatch' if waiting_dispatch else 'idle'
    source_skew = _snapshot_source_skew(repo_latest, eeepc_latest)
    material_progress_source = (
        (eeepc_raw.get('material_progress') if isinstance(eeepc_raw, dict) else None)
        or (producer_summary.get('material_progress') if isinstance(producer_summary, dict) else None)
        or (repo_raw.get('material_progress') if isinstance(repo_raw, dict) else None)
    )
    return {
        'active_goal': (eeepc_latest or {}).get('active_goal') or (repo_latest or {}).get('active_goal'),
        'repo_status': (repo_latest or {}).get('status'),
        'eeepc_status': (eeepc_latest or {}).get('status'),
        'approval': approval,
        'current_blocker': None if ((isinstance(producer_summary.get('task_plan'), dict) and producer_summary.get('task_plan', {}).get('current_task')) or (repo_latest or {}).get('current_task')) else current_blocker,
        'current_task': producer_current_task or (repo_latest or {}).get('current_task'),
        'producer_summary': producer_summary if isinstance(producer_summary, dict) else {},
        'blocker_summary': (producer_summary.get('blocker_summary') if isinstance(producer_summary, dict) else None) or {
            'schema_version': 'blocker-summary-v1',
            'state': 'blocked' if current_blocker.get('kind') == 'block' else 'stagnant' if current_blocker.get('blocked_next_step') or current_blocker.get('failure_class') else 'clear',
            'reason': current_blocker.get('blocked_next_step') or current_blocker.get('failure_class') or current_blocker.get('source') or 'none',
            'recommended_next_action': current_blocker.get('blocked_next_step') or current_blocker.get('selected_task_title') or current_blocker.get('selected_tasks_text') or 'continue the current plan',
            'source': current_blocker.get('source') or 'dashboard_current_blocker',
            'current_task_id': (producer_summary.get('task_plan') or {}).get('current_task_id') if isinstance(producer_summary, dict) else None,
            'current_task_title': (producer_summary.get('task_plan') or {}).get('current_task') if isinstance(producer_summary, dict) else None,
        },
        'guarded_evolution': guarded_evolution if isinstance(guarded_evolution, dict) else {},
        'selfevo_current_proof': selfevo_current_proof,
        'selfevo_remote_freshness': selfevo_remote_freshness,
        'local_ci': local_ci if isinstance(local_ci, dict) else {},
        'runtime_source': (producer_summary.get('runtime_source') if isinstance(producer_summary, dict) else None),
        'prompt_mass': (producer_summary.get('prompt_mass') if isinstance(producer_summary, dict) else None),
        'owner_utility': (producer_summary.get('owner_utility') if isinstance(producer_summary, dict) else None),
        'subagent_rollup': (repo_raw.get('subagent_rollup') if isinstance(repo_raw, dict) else None) or (producer_summary.get('subagent_rollup') if isinstance(producer_summary, dict) else None),
        'material_progress': _material_progress_summary(material_progress_source),
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
        'source_skew': source_skew,
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



def _remote_subagent_state_payload(cfg: DashboardConfig, state_root: str) -> dict:
    remote_root = str(state_root).rstrip('/')
    if not remote_root or not getattr(cfg, 'eeepc_ssh_host', None):
        return {'ok': False, 'error': 'remote_root_or_host_missing'}
    script = f'''
import json, pathlib, time
root=pathlib.Path(__import__('sys').argv[1])
limit=max(0, int(__import__('sys').argv[2]))
now=time.time()
def read(p):
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        return {{'_error': str(exc)}}
requests=[]
for p in sorted((root/'subagents'/'requests').glob('*.json'), key=lambda x:x.stat().st_mtime, reverse=True)[:limit]:
    payload=read(p)
    request_id=payload.get('request_id') or payload.get('id')
    semantic=payload.get('semantic_task_id') or payload.get('task_id')
    status=payload.get('request_status') or payload.get('status') or 'queued'
    requests.append({{'path': str(p), 'source': 'eeepc', 'source_root': str(root), 'task_id': payload.get('task_id'), 'semantic_task_id': semantic, 'request_id': request_id, 'verification_task_id': payload.get('verification_task_id') or request_id, 'verification_role': payload.get('verification_role'), 'cycle_id': payload.get('cycle_id'), 'profile': payload.get('profile'), 'status': status, 'request_status': status, 'age_seconds': max(0, int(now-p.stat().st_mtime)), 'source_artifact': payload.get('source_artifact')}})
results=[]
for p in sorted((root/'subagents'/'results').glob('*.json'), key=lambda x:x.stat().st_mtime, reverse=True)[:limit]:
    payload=read(p)
    request_id=payload.get('request_id') or payload.get('id')
    semantic=payload.get('semantic_task_id') or payload.get('task_id')
    results.append({{'path': str(p), 'source': 'eeepc', 'source_root': str(root), 'request_path': payload.get('request_path'), 'request_id': request_id, 'semantic_task_id': semantic, 'verification_task_id': payload.get('verification_task_id') or request_id, 'verification_role': payload.get('verification_role'), 'report_path': payload.get('report_path') or payload.get('report_source'), 'task_id': payload.get('task_id'), 'cycle_id': payload.get('cycle_id'), 'status': payload.get('status') or payload.get('result_status') or 'completed', 'terminal_reason': payload.get('terminal_reason') or payload.get('reason'), 'recommended_next_action': payload.get('recommended_next_action'), 'blocker': payload.get('blocker') if isinstance(payload.get('blocker'), dict) else None, 'summary': payload.get('summary'), 'age_seconds': max(0, int(now-p.stat().st_mtime)), 'source_artifact': payload.get('source_artifact')}})
print(json.dumps({{'ok': True, 'source_root': str(root), 'requests': requests, 'results': results}}, sort_keys=True))
'''
    limit = max(0, int(getattr(cfg, 'max_subagent_records', 200) or 0))
    remote_command = f"python3 -c {shlex.quote(script)} {shlex.quote(remote_root)} {limit}"
    ssh_cmd = _build_ssh_command(cfg, remote_command)
    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=8, check=True)
        payload = json.loads(proc.stdout or '{}')
        return payload if isinstance(payload, dict) else {'ok': False, 'error': 'remote_payload_not_dict'}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)[:500], 'source_root': remote_root}


def _discover_subagent_requests(cfg: DashboardConfig, stale_after_seconds: int = 3600) -> dict:
    local_state_root = cfg.nanobot_repo_root / 'workspace' / 'state'
    canonical_state_root = Path(str(cfg.eeepc_state_root)) if getattr(cfg, 'eeepc_state_root', None) else None
    local_has_activity = (local_state_root / 'subagents' / 'requests').exists() or (local_state_root / 'subagents' / 'results').exists()
    canonical_has_activity = bool(canonical_state_root and ((canonical_state_root / 'subagents' / 'requests').exists() or (canonical_state_root / 'subagents' / 'results').exists()))
    remote_payload: dict | None = None
    canonical_remote = False
    if canonical_state_root and not canonical_has_activity:
        remote_payload = _remote_subagent_state_payload(cfg, str(cfg.eeepc_state_root))
        canonical_remote = bool(remote_payload.get('ok') and (remote_payload.get('requests') or remote_payload.get('results')))
        canonical_has_activity = canonical_remote
    if canonical_has_activity:
        state_root = canonical_state_root
        selected_source = 'eeepc'
    else:
        state_root = local_state_root
        selected_source = 'local'
    source_skew_state = 'skewed' if canonical_has_activity and local_has_activity and canonical_state_root != local_state_root else 'aligned'
    source_skew_reasons = ['local_and_canonical_subagent_roots_present'] if source_skew_state == 'skewed' else []
    request_dir = state_root / 'subagents' / 'requests'
    result_dir = state_root / 'subagents' / 'results'
    now = time.time()
    requests: list[dict] = []
    if canonical_remote and isinstance(remote_payload, dict):
        requests = [dict(item) for item in remote_payload.get('requests', []) if isinstance(item, dict)]
    elif request_dir.exists():
        for path in sorted(request_dir.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            payload = _json_file(path)
            status = payload.get('request_status') or payload.get('status') or 'queued'
            age = max(0, int(now - path.stat().st_mtime))
            request_id = payload.get('request_id') or payload.get('id')
            semantic_task_id = payload.get('semantic_task_id') or payload.get('task_id')
            verification_task_id = payload.get('verification_task_id') or request_id
            requests.append({
                'path': str(path),
                'source': selected_source,
                'source_root': str(state_root),
                'task_id': payload.get('task_id'),
                'semantic_task_id': semantic_task_id,
                'request_id': request_id,
                'verification_task_id': verification_task_id,
                'verification_role': payload.get('verification_role'),
                'cycle_id': payload.get('cycle_id'),
                'profile': payload.get('profile'),
                'status': status,
                'request_status': status,
                'age_seconds': age,
                'source_artifact': payload.get('source_artifact'),
            })
    results: list[dict] = []
    results_by_request_path: dict[str, dict] = {}
    results_by_request_id: dict[str, dict] = {}
    results_by_cycle_id: dict[str, dict] = {}
    results_by_task_id: dict[str, dict] = {}
    result_dirs = [result_dir, cfg.nanobot_repo_root / '.nanobot' / 'subagents'] if selected_source == 'local' else [result_dir]
    if canonical_remote and isinstance(remote_payload, dict):
        results = [dict(item) for item in remote_payload.get('results', []) if isinstance(item, dict)]
        for result in results:
            if result.get('request_path'):
                results_by_request_path.setdefault(str(result['request_path']), result)
            if result.get('request_id'):
                results_by_request_id.setdefault(str(result['request_id']), result)
            if result.get('cycle_id'):
                results_by_cycle_id.setdefault(str(result['cycle_id']), result)
            if result.get('task_id'):
                results_by_task_id.setdefault(str(result['task_id']), result)
    else:
        for current_result_dir in result_dirs:
            if not current_result_dir.exists():
                continue
            for path in sorted(current_result_dir.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
                payload = _json_file(path)
                hydrated_report = _canonical_report_payload(cfg, {'report_source': payload.get('report_path') or payload.get('report_source')}) if (payload.get('report_path') or payload.get('report_source')) else {}
                hydrated_budget = hydrated_report.get('budget_used') if isinstance(hydrated_report.get('budget_used'), dict) else None
                follow_through = hydrated_report.get('follow_through') if isinstance(hydrated_report.get('follow_through'), dict) else {}
                hydrated_artifacts = hydrated_report.get('artifact_paths') or follow_through.get('artifact_paths')
                result = {
                    'path': str(path),
                    'source': selected_source,
                    'source_root': str(state_root),
                    'request_path': payload.get('request_path'),
                    'request_id': payload.get('request_id') or payload.get('id'),
                    'semantic_task_id': payload.get('semantic_task_id') or payload.get('task_id') or hydrated_report.get('current_task_id'),
                    'verification_task_id': payload.get('verification_task_id') or payload.get('request_id') or payload.get('id'),
                    'verification_role': payload.get('verification_role'),
                    'report_path': payload.get('report_path') or payload.get('report_source'),
                    'task_id': payload.get('task_id') or hydrated_report.get('current_task_id'),
                    'cycle_id': payload.get('cycle_id') or hydrated_report.get('cycle_id'),
                    'status': payload.get('status') or payload.get('result_status') or 'completed',
                    'terminal_reason': payload.get('terminal_reason') or payload.get('reason'),
                    'recommended_next_action': payload.get('recommended_next_action'),
                    'blocker': payload.get('blocker') if isinstance(payload.get('blocker'), dict) else None,
                    'summary': payload.get('summary'),
                    'age_seconds': max(0, int(now - path.stat().st_mtime)),
                    'hydrated_report_current_task_id': hydrated_report.get('current_task_id'),
                    'hydrated_report_result_status': hydrated_report.get('result_status') or hydrated_report.get('status'),
                    'budget_used': hydrated_budget,
                    'artifact_paths': hydrated_artifacts,
                    'canonical_report_hydrated': bool(hydrated_report),
                }
                results.append(result)
                if result.get('request_path'):
                    results_by_request_path.setdefault(str(result['request_path']), result)
                if result.get('request_id'):
                    results_by_request_id.setdefault(str(result['request_id']), result)
                if result.get('cycle_id'):
                    results_by_cycle_id.setdefault(str(result['cycle_id']), result)
                if result.get('task_id'):
                    results_by_task_id.setdefault(str(result['task_id']), result)
    for request in requests:
        materialized_result = (
            (results_by_request_id.get(str(request.get('request_id'))) if request.get('request_id') else None)
            or results_by_request_path.get(str(request.get('path')))
            or (results_by_cycle_id.get(str(request.get('cycle_id'))) if request.get('cycle_id') else None)
            or (results_by_task_id.get(str(request.get('task_id'))) if request.get('task_id') else None)
        )
        if isinstance(materialized_result, dict):
            request['status'] = str(materialized_result.get('status') or 'completed').lower()
            request['materialized_result_path'] = materialized_result.get('path')
            request['materialized_result_status'] = materialized_result.get('status')
            if materialized_result.get('terminal_reason'):
                request['terminal_reason'] = materialized_result.get('terminal_reason')
        elif request.get('request_status') in {'queued', 'pending'} and request.get('age_seconds', 0) >= stale_after_seconds:
            request['status'] = 'stale'
    stale_count = sum(1 for item in requests if item.get('request_status') in {'queued', 'pending'} and not item.get('materialized_result_path') and item.get('age_seconds', 0) >= stale_after_seconds)
    queued_count = sum(1 for item in requests if item.get('request_status') in {'queued', 'pending'} and not item.get('materialized_result_path'))
    blocked_count = sum(1 for item in results if str(item.get('status') or '').lower() in {'blocked', 'terminal_blocked'})
    result_count = len(results)
    rollup = None if canonical_remote else _subagent_rollup_snapshot(state_root=state_root)
    if isinstance(rollup, dict):
        stale_count = int(rollup.get('stale_request_count') or 0)
        queued_count = int(rollup.get('queued_request_count') or 0)
        result_count = int(rollup.get('completed_result_count') or rollup.get('result_count') or result_count)
        blocked_count = int(rollup.get('blocked_result_count') or blocked_count)
        state = rollup.get('state') or ('stale' if stale_count else ('available' if requests or results else 'empty'))
        reason = rollup.get('reason') or ('stale_requests_present' if stale_count else ('queued_requests_present' if queued_count else ('completed_results_only' if result_count else 'no_subagent_activity')))
    else:
        state = 'stale' if stale_count else ('available' if requests or results else 'empty')
        reason = 'stale_requests_present' if stale_count else ('available_subagent_activity' if requests or results else 'no_subagent_activity')
    def _result_age_seconds(item: dict) -> int | None:
        try:
            return int(item.get('age_seconds')) if item.get('age_seconds') is not None else None
        except (TypeError, ValueError):
            return None

    stale_result_count = sum(
        1 for item in results
        if _result_age_seconds(item) is None or _result_age_seconds(item) >= 6 * 60 * 60
    )
    fresh_result_count = max(0, result_count - stale_result_count)
    return {
        'schema_version': 'subagent-visibility-v1',
        'source': {
            'selected': selected_source,
            'state_root': str(state_root),
            'local_state_root': str(local_state_root),
            'canonical_state_root': str(canonical_state_root) if canonical_state_root else None,
            'local_available': bool(local_has_activity),
            'canonical_available': bool(canonical_has_activity),
            'canonical_remote': bool(canonical_remote),
        },
        'source_skew': {
            'state': source_skew_state,
            'reasons': source_skew_reasons,
        },
        'requests': requests,
        'results': results,
        'subagent_rollup': rollup,
        'summary': {
            'total_requests': len(requests),
            'stale_request_count': stale_count,
            'queued_request_count': queued_count,
            'result_count': result_count,
            'blocked_result_count': blocked_count,
            'stale_result_count': stale_result_count,
            'fresh_result_count': fresh_result_count,
            'latest_result_age_seconds': (results[0].get('age_seconds') if results else ((rollup or {}).get('latest_result') or {}).get('age_seconds') if isinstance((rollup or {}).get('latest_result'), dict) else None),
            'freshness_state': 'fresh' if fresh_result_count else ('stale' if stale_result_count else state),
            'freshness_window_seconds': 6 * 60 * 60,
            'sources': [selected_source] if requests or results or isinstance(rollup, dict) else [],
            'state': state,
            'reason': reason,
        },
        'latest_request': requests[0] if requests else ((rollup or {}).get('latest_request') if isinstance(rollup, dict) else None),
        'latest_result': results[0] if results else ((rollup or {}).get('latest_result') if isinstance(rollup, dict) else None),
        'latest_telemetry': (rollup or {}).get('latest_telemetry') if isinstance(rollup, dict) else None,
    }


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



def _eeepc_privileged_rollout_readiness(eeepc_latest: dict | None, runtime_parity: dict | None) -> dict:
    raw = _json_loads_dict(dict(eeepc_latest).get('raw_json')) if eeepc_latest else {}
    source_errors = raw.get('source_errors') if isinstance(raw.get('source_errors'), dict) else {}
    outbox = raw.get('outbox') if isinstance(raw.get('outbox'), dict) else {}
    runtime_parity = runtime_parity if isinstance(runtime_parity, dict) else {}
    blocked_capabilities: list[str] = []
    outbox_error = source_errors.get('outbox') if isinstance(source_errors.get('outbox'), dict) else None
    goals_error = source_errors.get('goals') if isinstance(source_errors.get('goals'), dict) else None
    if outbox_error:
        blocked_capabilities.append('read_authority_outbox')
    if goals_error:
        blocked_capabilities.append('read_goal_registry')
    runtime_reasons = runtime_parity.get('reasons') if isinstance(runtime_parity.get('reasons'), list) else []
    if runtime_parity.get('state') == 'legacy_reward_loop' or 'live_feedback_decision_missing' in runtime_reasons:
        blocked_capabilities.append('execute_opencode_nanobot_or_sudo')
    report_source = outbox.get('source') or (dict(eeepc_latest).get('report_source') if eeepc_latest else None)
    outbox_source = dict(eeepc_latest).get('outbox_source') if eeepc_latest else None
    partial_report = bool(report_source and outbox_source == report_source and '/reports/evolution-' in str(report_source))
    blocked_capabilities = sorted(set(blocked_capabilities))
    if blocked_capabilities:
        state = 'blocked_privileged_access'
    elif partial_report:
        state = 'partial_report_only'
    else:
        state = 'ready'
    return {
        'schema_version': 'eeepc-privileged-rollout-readiness-v1',
        'state': state,
        'host': 'eeepc',
        'requires_privileged_access': bool(blocked_capabilities),
        'blocked_capabilities': blocked_capabilities,
        'available_partial_proof': 'latest_readable_report' if partial_report else None,
        'report_source': report_source,
        'outbox_source': outbox_source,
        'source_errors': source_errors,
        'runtime_parity_state': runtime_parity.get('state'),
        'runtime_parity_reasons': runtime_reasons,
        'next_issue': 210 if blocked_capabilities else None,
    }


def _dashboard_runtime_parity(repo_plan: dict | None, eeepc_plan: dict | None, cfg: DashboardConfig) -> dict:
    repo_plan = repo_plan if isinstance(repo_plan, dict) else {}
    eeepc_plan = eeepc_plan if isinstance(eeepc_plan, dict) else {}
    state_root = cfg.nanobot_repo_root / 'workspace' / 'state'
    artifacts = {
        'hypotheses_backlog': (state_root / 'hypotheses' / 'backlog.json').exists(),
        'credits_latest': (state_root / 'credits' / 'latest.json').exists(),
        'control_plane_current_summary': (state_root / 'control_plane' / 'current_summary.json').exists(),
        'self_evolution_current_state': (state_root / 'self_evolution' / 'current_state.json').exists(),
    }
    reasons = []
    eeepc_raw = _json_loads_dict(eeepc_plan.get('raw_json')) if _has_value(eeepc_plan.get('raw_json')) else {}
    live_reachability = eeepc_plan.get('reachability') if isinstance(eeepc_plan.get('reachability'), dict) else None
    if live_reachability is None and isinstance(eeepc_raw.get('reachability'), dict):
        live_reachability = eeepc_raw.get('reachability')
    live_authority = None
    if isinstance(live_reachability, dict):
        live_authority = {
            'reachable': bool(live_reachability.get('reachable')),
            'host': live_reachability.get('host') or cfg.eeepc_ssh_host,
            'port': live_reachability.get('port') or 22,
            'error': live_reachability.get('error') or live_reachability.get('stderr') or live_reachability.get('reason'),
        }
        if live_reachability.get('reachable') is False:
            reasons.append('live_authority_unreachable')
    local_feedback = repo_plan.get('feedback_decision') if isinstance(repo_plan.get('feedback_decision'), dict) else None
    live_feedback = eeepc_plan.get('feedback_decision') if isinstance(eeepc_plan.get('feedback_decision'), dict) else None
    if local_feedback and not live_feedback:
        reasons.append('live_feedback_decision_missing')
    local_task = repo_plan.get('current_task_id') or repo_plan.get('current_task')
    live_task = eeepc_plan.get('current_task_id') or eeepc_plan.get('current_task') or eeepc_plan.get('selected_tasks_text') or eeepc_plan.get('selected_tasks')
    local_task_identity = _task_identity_tokens({
        'current_task_id': repo_plan.get('current_task_id'),
        'current_task': repo_plan.get('current_task'),
        'selected_task_title': repo_plan.get('selected_task_title'),
        'selected_task_label': repo_plan.get('selected_task_label'),
    })
    live_task_identity = _task_identity_tokens({
        'current_task_id': eeepc_plan.get('current_task_id'),
        'current_task': eeepc_plan.get('current_task'),
        'selected_task_title': eeepc_plan.get('selected_task_title'),
        'selected_task_label': eeepc_plan.get('selected_task_label'),
    })
    task_identity_match = bool(local_task_identity & live_task_identity)
    live_is_legacy_reward = (
        'record-reward' in str(live_task or '')
        and not live_feedback
        and (
            eeepc_plan.get('task_selection_source') == 'recorded_current_task'
            or bool(local_task)
        )
    )
    live_hadi_handoff_selected_task = live_feedback.get('selected_task_id') if isinstance(live_feedback, dict) else None
    live_hadi_handoff = (
        all(artifacts.values())
        and isinstance(live_feedback, dict)
        and live_feedback.get('mode') in {'handoff_to_next_candidate', 'promote_review_followup', 'feedback_post_completion_handoff'}
        and live_feedback.get('selection_source') in {'feedback_post_completion_handoff', 'feedback_review_to_execution'}
        and _has_value(live_hadi_handoff_selected_task)
        and str(live_hadi_handoff_selected_task) == str(live_task)
    )
    live_pass_streak_switch = (
        all(artifacts.values())
        and isinstance(live_feedback, dict)
        and live_feedback.get('mode') == 'retire_goal_artifact_pair'
        and live_feedback.get('retire_goal_artifact_pair') is True
        and live_feedback.get('selection_source') == 'feedback_pass_streak_switch'
        and _has_value(live_hadi_handoff_selected_task)
        and str(live_hadi_handoff_selected_task) in {str(live_task), str(local_task)}
    )
    live_terminal_selfevo_retire = (
        all(artifacts.values())
        and isinstance(live_feedback, dict)
        and live_feedback.get('mode') == 'retire_terminal_selfevo_lane'
        and live_feedback.get('selection_source') == 'feedback_terminal_selfevo_retire'
        and _has_value(live_hadi_handoff_selected_task)
        and str(live_hadi_handoff_selected_task) == str(live_task)
        and bool(live_feedback.get('terminal_selfevo_issue'))
    )
    live_active_lane = (
        all(artifacts.values())
        and isinstance(live_feedback, dict)
        and live_feedback.get('mode') == 'continue_active_lane'
        and live_feedback.get('selection_source') == 'feedback_continue_active_lane'
        and _has_value(live_hadi_handoff_selected_task)
        and str(live_hadi_handoff_selected_task) == str(live_task)
        and _has_value(live_feedback.get('current_task_id'))
        and str(live_feedback.get('current_task_id')) == str(live_task)
        and 'record-reward' not in str(live_task or '')
    )
    live_synthesized_materialization = (
        isinstance(live_feedback, dict)
        and live_feedback.get('mode') == 'materialize_synthesized_improvement'
        and live_feedback.get('selection_source') == 'feedback_synthesis_materialization'
        and _has_value(live_feedback.get('selected_task_id'))
        and str(live_feedback.get('selected_task_id')) == str(live_task)
    )
    live_post_materialization_reward = (
        isinstance(live_feedback, dict)
        and live_feedback.get('mode') == 'record_reward_after_synthesized_materialization'
        and live_feedback.get('selection_source') == 'feedback_synthesized_materialization_complete_reward'
        and _has_value(live_feedback.get('selected_task_id'))
        and str(live_feedback.get('selected_task_id')) == 'record-reward'
        and str(live_task or '') == 'record-reward'
    )
    live_synthesis_candidate = (
        isinstance(live_feedback, dict)
        and live_feedback.get('mode') == 'synthesize_next_candidate'
        and live_feedback.get('selection_source') == 'feedback_no_selectable_retired_lane_synthesis'
        and _has_value(live_feedback.get('selected_task_id'))
        and str(live_feedback.get('selected_task_id')) == str(live_task)
        and str(live_task or '') == 'synthesize-next-improvement-candidate'
    )
    live_failure_learning_handoff = (
        all(artifacts.values())
        and isinstance(live_feedback, dict)
        and live_feedback.get('mode') in {'complete_active_lane', 'stale_complete_lane_record_reward_repair'}
        and live_feedback.get('selection_source') == 'feedback_complete_active_lane_to_failure_learning'
        and live_feedback.get('selected_task_id') == 'analyze-last-failed-candidate'
        and str(live_task) == 'analyze-last-failed-candidate'
    )
    local_complete_lane_failure_repair = (
        all(artifacts.values())
        and isinstance(local_feedback, dict)
        and local_feedback.get('mode') in {'complete_active_lane', 'stale_complete_lane_record_reward_repair'}
        and local_feedback.get('selection_source') == 'feedback_complete_active_lane_to_failure_learning'
        and local_feedback.get('selected_task_id') == 'analyze-last-failed-candidate'
        and str(local_task) == 'analyze-last-failed-candidate'
    )
    live_stale_complete_lane_reward = (
        isinstance(live_feedback, dict)
        and live_feedback.get('mode') == 'complete_active_lane'
        and live_feedback.get('current_task_id') == 'materialize-pass-streak-improvement'
        and live_feedback.get('selected_task_id') == 'record-reward'
        and live_feedback.get('selection_source') == 'feedback_complete_active_lane'
        and 'record-reward' in str(live_task or '')
    )
    authority_resolution = None
    canonical_task = local_task or live_task
    if local_task and live_task and not task_identity_match:
        if live_is_legacy_reward:
            reasons.append('legacy_live_reward_loop_current_task')
        elif live_hadi_handoff:
            authority_resolution = 'fresh_live_hadi_handoff'
            canonical_task = live_task
        elif live_pass_streak_switch:
            authority_resolution = 'fresh_live_pass_streak_switch'
            canonical_task = live_task
        elif live_terminal_selfevo_retire:
            authority_resolution = 'fresh_live_terminal_selfevo_retire'
            canonical_task = live_task
        elif live_active_lane:
            authority_resolution = 'fresh_live_active_lane'
            canonical_task = live_task
        elif live_synthesized_materialization:
            authority_resolution = 'fresh_live_synthesized_materialization'
            canonical_task = live_task
        elif live_post_materialization_reward:
            authority_resolution = 'fresh_live_post_materialization_reward'
            canonical_task = live_task
        elif live_synthesis_candidate:
            authority_resolution = 'fresh_live_synthesis_candidate'
            canonical_task = live_task
        elif live_failure_learning_handoff:
            authority_resolution = 'fresh_live_failure_learning_handoff'
            canonical_task = live_task
        elif local_complete_lane_failure_repair and live_stale_complete_lane_reward:
            authority_resolution = 'local_failure_learning_repair_over_stale_live_complete_lane'
            canonical_task = local_task
        else:
            reasons.append('current_task_drift')
    missing = [key for key, present in artifacts.items() if not present]
    if missing:
        reasons.append('live_hadi_artifacts_missing')
    legacy = live_is_legacy_reward or (not live_feedback and 'record-reward' in str(live_task or '') and bool(missing))
    source_skew = _snapshot_source_skew(repo_plan, eeepc_plan)
    source_skew_state = source_skew.get('state') if isinstance(source_skew, dict) else None
    parity_state = 'legacy_reward_loop' if legacy else ('healthy' if not reasons else 'degraded')
    if parity_state == 'healthy' and authority_resolution and source_skew_state == 'skewed':
        parity_state = 'authority_resolved_with_source_skew'
    return {
        'schema_version': 'runtime-parity-v1',
        'state': parity_state,
        'reasons': reasons,
        'missing_live_artifacts': missing,
        'local_current_task_id': local_task,
        'live_current_task_id': live_task,
        'canonical_current_task_id': canonical_task,
        'live_task_selection_source': eeepc_plan.get('task_selection_source'),
        'live_authority': live_authority,
        'next_action': 'restore_live_authority_reachability_then_recollect' if live_authority and live_authority.get('reachable') is False else None,
        'authority_resolution': authority_resolution,
        'source_skew': source_skew,
    }


def _strong_reflection_freshness(cfg: DashboardConfig, now: datetime, eeepc_latest: dict | None = None) -> dict:
    local_path = cfg.nanobot_repo_root / 'workspace' / 'state' / 'strong_reflection' / 'latest.json'
    payload = _json_file(local_path)
    source = 'local'
    path = str(local_path)
    errors: dict[str, str] = {}
    if not payload and eeepc_latest is not None:
        eeepc_row = dict(eeepc_latest)
        eeepc_raw = _json_loads_dict(eeepc_row.get('raw_json'))
        collected_payload = eeepc_raw.get('strong_reflection') if isinstance(eeepc_raw.get('strong_reflection'), dict) else None
        if collected_payload is None and isinstance(eeepc_raw.get('payloads'), dict):
            collected_payload = eeepc_raw['payloads'].get('strong_reflection') if isinstance(eeepc_raw['payloads'].get('strong_reflection'), dict) else None
        if collected_payload is not None:
            payload = collected_payload
            source = 'eeepc'
            path = str(collected_payload.get('path') or f"{cfg.eeepc_state_root}/strong_reflection/latest.json")
    if not payload and cfg.eeepc_ssh_key.exists():
        remote_path = f"{cfg.eeepc_state_root}/strong_reflection/latest.json"
        remote = _remote_file_preview(cfg, remote_path, max_chars=20000)
        if remote.get('exists') and remote.get('preview'):
            try:
                parsed = json.loads(str(remote.get('preview')))
                if isinstance(parsed, dict):
                    payload = parsed
                    source = 'eeepc'
                    path = remote_path
            except Exception as exc:
                errors['eeepc_parse_error'] = str(exc)
        elif remote.get('preview'):
            errors['eeepc_preview_error'] = str(remote.get('preview'))[:500]
    if not payload:
        result = {
            'schema_version': 'strong-reflection-freshness-v1',
            'state': 'missing',
            'available': False,
            'source': source,
            'path': path,
            'local_path': str(local_path),
            'reason': 'strong_reflection_latest_missing',
        }
        if errors:
            result['errors'] = errors
        return result
    recorded_at = payload.get('recorded_at_utc')
    ts = _parse_timestamp(recorded_at) if recorded_at else None
    age = max(0, int((now.astimezone(timezone.utc) - ts).total_seconds())) if ts is not None else None
    state = 'fresh' if isinstance(age, int) and age <= 8 * 3600 else 'stale'
    return {
        'schema_version': 'strong-reflection-freshness-v1',
        'state': state,
        'available': True,
        'source': source,
        'path': path,
        'local_path': str(local_path),
        'recorded_at_utc': recorded_at,
        'age_seconds': age,
        'summary': payload.get('summary'),
        'mode': payload.get('mode'),
    }


def _ambition_utilization_verdict(*, analytics: dict, experiment_visibility: dict, subagent_visibility: dict | None = None) -> dict:
    """Classify whether recent autonomous activity is substantive or shallow.

    PASS cycles alone are not enough: repeated discarded cycles with almost no
    tool/subagent/time usage are active telemetry, not ambitious self-development.
    """
    recent = analytics.get('recent_status_sequence') or []
    low_budget_discard_count = 0
    inspected = 0
    total_requests = 0
    total_tool_calls = 0
    total_subagents = 0
    total_elapsed = 0
    repeated_tasks: list[str] = []
    feedback_modes: list[str] = []
    materialized_artifacts: list[str] = []
    blocked_escalation: dict | None = None
    for row in recent[:20]:
        detail = row.get('detail') if isinstance(row.get('detail'), dict) else {}
        feedback_decision = detail.get('feedback_decision') if isinstance(detail.get('feedback_decision'), dict) else {}
        feedback_mode = feedback_decision.get('mode')
        if feedback_mode:
            feedback_modes.append(str(feedback_mode))
        artifact_path = detail.get('materialized_improvement_artifact_path') or feedback_decision.get('artifact_path')
        if artifact_path:
            materialized_artifacts.append(str(artifact_path))
        experiment = detail.get('experiment') if isinstance(detail.get('experiment'), dict) else {}
        budget_used = detail.get('budget_used') if isinstance(detail.get('budget_used'), dict) else experiment.get('budget_used') if isinstance(experiment.get('budget_used'), dict) else {}
        if not isinstance(budget_used, dict):
            budget_used = {}
        if not detail.get('current_task_id') and not feedback_decision and not budget_used and not experiment:
            continue
        outcome = experiment.get('outcome') or detail.get('outcome')
        task_id = detail.get('current_task_id') or row.get('title')
        if task_id:
            repeated_tasks.append(str(task_id))
        requests = int(budget_used.get('requests') or 0)
        tool_calls = int(budget_used.get('tool_calls') or 0)
        subagents = int(budget_used.get('subagents') or 0)
        elapsed = int(budget_used.get('elapsed_seconds') or 0)
        total_requests += requests
        total_tool_calls += tool_calls
        total_subagents += subagents
        total_elapsed += elapsed
        inspected += 1
        if outcome == 'discard' and requests <= 1 and tool_calls <= 2 and subagents == 0 and elapsed <= 1:
            low_budget_discard_count += 1
    current_experiment = experiment_visibility.get('current_experiment') if isinstance(experiment_visibility, dict) else {}
    current_budget_used = current_experiment.get('budget_used') if isinstance(current_experiment, dict) and isinstance(current_experiment.get('budget_used'), dict) else {}
    current_outcome = current_experiment.get('outcome') if isinstance(current_experiment, dict) else None
    if isinstance(current_experiment, dict):
        current_feedback_decision = current_experiment.get('feedback_decision') if isinstance(current_experiment.get('feedback_decision'), dict) else None
        current_raw = current_experiment.get('raw') if isinstance(current_experiment.get('raw'), dict) else {}
        raw_feedback_decision = current_raw.get('feedback_decision') if isinstance(current_raw.get('feedback_decision'), dict) else None
        for candidate in (current_feedback_decision, raw_feedback_decision):
            if isinstance(candidate, dict) and candidate.get('mode') == 'ambition_escalation_blocked':
                blocked_escalation = candidate
                break
    if inspected == 0 and isinstance(current_budget_used, dict):
        inspected = 1
        total_requests = int(current_budget_used.get('requests') or 0)
        total_tool_calls = int(current_budget_used.get('tool_calls') or 0)
        total_subagents = int(current_budget_used.get('subagents') or 0)
        total_elapsed = int(current_budget_used.get('elapsed_seconds') or 0)
        if current_outcome == 'discard' and total_requests <= 1 and total_tool_calls <= 2 and total_subagents == 0 and total_elapsed <= 1:
            low_budget_discard_count = 1
    same_task_streak = len(repeated_tasks) >= 5 and len(set(repeated_tasks[:5])) == 1
    recent_mode_set = set(feedback_modes[:8])
    rotating_synthesis_reward_window = (
        len(feedback_modes) >= 3
        and 'synthesize_next_candidate' in recent_mode_set
        and 'complete_active_lane' in recent_mode_set
        and 'record_reward_after_synthesized_materialization' in recent_mode_set
        and len(set(materialized_artifacts[:8])) >= 2
    )
    bridge_summary = subagent_visibility if isinstance(subagent_visibility, dict) else {}
    reasons: list[str] = []
    if blocked_escalation is not None:
        reasons.append('ambition_escalation_blocked')
    if not rotating_synthesis_reward_window:
        if low_budget_discard_count >= 5:
            reasons.append('low_budget_discard_streak')
        if same_task_streak:
            reasons.append('same_task_streak')
    if inspected >= 5 and total_subagents == 0 and (bridge_summary or not rotating_synthesis_reward_window):
        reasons.append('subagents_unused')
    if inspected >= 5 and total_tool_calls <= inspected * 2 and (bridge_summary or not rotating_synthesis_reward_window):
        reasons.append('tool_budget_underused')
    state = 'underutilized' if reasons else 'substantive'
    escalation = None
    if blocked_escalation is not None:
        blocked_payload = blocked_escalation.get('ambition_escalation') if isinstance(blocked_escalation.get('ambition_escalation'), dict) else {}
        escalation = {
            'schema_version': 'ambition-escalation-v1',
            'state': 'blocked',
            'safe_bounded_lanes': blocked_payload.get('safe_bounded_lanes') or [
                'materialize-synthesized-improvement',
                'subagent-verify-materialized-improvement',
                'synthesize-next-improvement-candidate',
            ],
            'policy': blocked_payload.get('policy') or 'select_safe_bounded_lane_or_emit_precise_blocker',
            'blocker': blocked_payload.get('blocker') or 'ambition_escalation_blocked',
            'source': 'feedback_decision',
        }
    elif state == 'underutilized':
        escalation = {
            'schema_version': 'ambition-escalation-v1',
            'state': 'required',
            'safe_bounded_lanes': [
                'materialize-synthesized-improvement',
                'subagent-verify-materialized-improvement',
                'synthesize-next-improvement-candidate',
            ],
            'policy': 'select_safe_bounded_lane_or_emit_precise_blocker',
            'blocker': None,
        }
    return {
        'schema_version': 'ambition-utilization-v1',
        'state': state,
        'reasons': reasons,
        'recent_window': inspected,
        'low_budget_discard_count': low_budget_discard_count,
        'budget_used_sum': {
            'requests': total_requests,
            'tool_calls': total_tool_calls,
            'subagents': total_subagents,
            'elapsed_seconds': total_elapsed,
        },
        'same_task_streak': same_task_streak,
        'rotating_synthesis_reward_window': rotating_synthesis_reward_window,
        'subagent_visibility_available': bool(bridge_summary),
        'recommended_next_action': 'resolve_ambition_escalation_blocker' if blocked_escalation is not None else ('escalate_to_higher_ambition_lane_or_emit_precise_blocker' if state == 'underutilized' else None),
        'escalation': escalation,
    }


def _autonomy_verdict(*, analytics: dict, plan_latest: dict | None, experiment_visibility: dict, credits_visibility: dict, cfg: DashboardConfig, material_progress: dict | None = None, runtime_parity: dict | None = None, ambition_utilization: dict | None = None, hypothesis_dynamics: dict | None = None, promotion_replay_readiness: dict | None = None, strong_reflection_freshness: dict | None = None, subagent_visibility: dict | None = None) -> dict:
    reasons: list[str] = []
    state_root = cfg.nanobot_repo_root / 'workspace' / 'state'
    recent = analytics.get('recent_status_sequence') or []
    task_ids = []
    for row in recent:
        detail = row.get('detail') if isinstance(row.get('detail'), dict) else {}
        task_id = detail.get('current_task_id') or row.get('title')
        if task_id:
            task_ids.append(str(task_id))
    if len(task_ids) >= 5 and len(set(task_ids[:5])) == 1:
        reasons.append('same_task_streak')
    current_experiment = experiment_visibility.get('current_experiment') or {}
    if current_experiment.get('outcome') == 'discard':
        reasons.append('discarded_experiment')
    current_credits = credits_visibility.get('current') or {}
    reward_gate = current_credits.get('reward_gate') if isinstance(current_credits.get('reward_gate'), dict) else {}
    if current_credits.get('delta') == 0.0 and reward_gate.get('status') == 'suppressed':
        reasons.append('suppressed_reward')
    latest_noop = _json_file(state_root / 'self_evolution' / 'runtime' / 'latest_noop.json')
    if latest_noop.get('status') == 'terminal_noop':
        reasons.append('terminal_noop')
    material_progress = material_progress if isinstance(material_progress, dict) else {}
    material_allows_healthy = bool(material_progress.get('healthy_autonomy_allowed'))
    if material_progress and not material_allows_healthy:
        reasons.append('material_progress_missing')
    recent_discard_rows = []
    recent_budget_rows = []
    recent_subagent_sum = 0
    for row in recent[:20]:
        detail = row.get('detail') if isinstance(row.get('detail'), dict) else {}
        experiment = detail.get('experiment') if isinstance(detail.get('experiment'), dict) else {}
        outcome = experiment.get('outcome') or detail.get('outcome')
        budget_used = detail.get('budget_used') if isinstance(detail.get('budget_used'), dict) else experiment.get('budget_used') if isinstance(experiment.get('budget_used'), dict) else {}
        if outcome:
            recent_discard_rows.append(outcome == 'discard')
        if isinstance(budget_used, dict) and budget_used:
            recent_budget_rows.append(budget_used)
            try:
                recent_subagent_sum += int(budget_used.get('subagents') or 0)
            except (TypeError, ValueError):
                pass
    subagent_visibility = subagent_visibility if isinstance(subagent_visibility, dict) else {}
    subagent_summary = subagent_visibility.get('summary') if isinstance(subagent_visibility.get('summary'), dict) else {}
    fresh_subagent_result_count = int(subagent_summary.get('fresh_result_count') or 0) if subagent_summary else 0
    subagent_result_count = int(subagent_summary.get('result_count') or 0) if subagent_summary else 0
    stale_subagent_result_count = int(subagent_summary.get('stale_result_count') or 0) if subagent_summary else 0
    queued_subagent_request_count = int(subagent_summary.get('queued_request_count') or 0) if subagent_summary else 0
    blocked_subagent_result_count = int(subagent_summary.get('blocked_result_count') or 0) if subagent_summary else 0
    no_fresh_subagent_result = bool(
        subagent_summary
        and fresh_subagent_result_count == 0
        and (subagent_result_count > 0 or stale_subagent_result_count > 0)
    )
    unresolved_subagent_request = bool(
        subagent_summary
        and queued_subagent_request_count > 0
        and fresh_subagent_result_count == 0
        and blocked_subagent_result_count == 0
    )
    discard_only_recent_window = bool(len(recent_discard_rows) >= 5 and all(recent_discard_rows) and recent_budget_rows and recent_subagent_sum == 0)
    if material_allows_healthy and discard_only_recent_window:
        reasons.append('recent_window_discard_only')
    if material_allows_healthy and no_fresh_subagent_result:
        reasons.append('subagent_evidence_stale')
    if material_allows_healthy and unresolved_subagent_request:
        reasons.append('subagent_request_unresolved')
    runtime_parity = runtime_parity if isinstance(runtime_parity, dict) else {}
    runtime_reasons = runtime_parity.get('reasons') if isinstance(runtime_parity.get('reasons'), list) else []
    runtime_tasks_aligned = (
        _has_value(runtime_parity.get('local_current_task_id'))
        and runtime_parity.get('local_current_task_id') == runtime_parity.get('live_current_task_id')
    )
    runtime_has_canonical_authority = _has_value(runtime_parity.get('canonical_current_task_id'))
    runtime_parity_is_blocking = runtime_parity.get('state') in {'legacy_reward_loop', 'degraded', 'unknown'}
    downgradeable_runtime_reasons = {'live_feedback_decision_missing', 'legacy_live_reward_loop_current_task'}
    runtime_has_only_historical_reasons = set(str(reason) for reason in runtime_reasons).issubset(downgradeable_runtime_reasons)
    runtime_can_be_historical = (
        material_allows_healthy
        and runtime_has_canonical_authority
        and 'current_task_drift' not in runtime_reasons
        and runtime_has_only_historical_reasons
        and (runtime_tasks_aligned or 'legacy_live_reward_loop_current_task' in runtime_reasons)
    )
    if runtime_parity_is_blocking and not runtime_can_be_historical:
        reasons.append('runtime_parity_blocked')
    ambition_utilization = ambition_utilization if isinstance(ambition_utilization, dict) else {}
    ambition_reasons = ambition_utilization.get('reasons') if isinstance(ambition_utilization.get('reasons'), list) else []
    ambition_escalation = ambition_utilization.get('escalation') if isinstance(ambition_utilization.get('escalation'), dict) else {}
    if 'ambition_escalation_blocked' in ambition_reasons or ambition_escalation.get('state') == 'blocked' or ambition_utilization.get('state') == 'underutilized':
        reasons.append('ambition_underutilized')
    hypothesis_dynamics = hypothesis_dynamics if isinstance(hypothesis_dynamics, dict) else {}
    if hypothesis_dynamics.get('state') == 'stagnant':
        reasons.append('hypothesis_dynamics_stagnant')
    promotion_replay_readiness = promotion_replay_readiness if isinstance(promotion_replay_readiness, dict) else {}
    promotion_pending = (
        promotion_replay_readiness.get('review_status') == 'pending_policy_review'
        or promotion_replay_readiness.get('decision') == 'pending_policy_review'
        or _missing_record(promotion_replay_readiness.get('decision_record'))
        or _missing_record(promotion_replay_readiness.get('accepted_record'))
    )
    if promotion_replay_readiness.get('state') == 'blocked' and promotion_pending:
        reasons.append('promotion_lifecycle_blocked')
    if promotion_replay_readiness.get('state') == 'not_ready':
        reasons.append('promotion_readiness_not_ready')
    strong_reflection_freshness = strong_reflection_freshness if isinstance(strong_reflection_freshness, dict) else {}
    if strong_reflection_freshness.get('state') in {'missing', 'stale', 'degraded'}:
        reasons.append('strong_reflection_not_fresh')
    historical_reasons: list[str] = []
    if material_allows_healthy:
        stale_after_material_progress = {'same_task_streak', 'discarded_experiment', 'suppressed_reward', 'terminal_noop'}
        freshness_blockers = {'recent_window_discard_only', 'subagent_evidence_stale', 'subagent_request_unresolved'}
        blocking_reasons = []
        for reason in reasons:
            if reason in stale_after_material_progress:
                historical_reasons.append(reason)
            elif (
                reason == 'ambition_underutilized'
                and not any(blocker in reasons for blocker in freshness_blockers)
                and 'ambition_escalation_blocked' not in ambition_reasons
                and ambition_escalation.get('state') != 'blocked'
            ):
                historical_reasons.append(reason)
            else:
                blocking_reasons.append(reason)
        if runtime_parity_is_blocking and runtime_can_be_historical:
            historical_reasons.append('runtime_parity_blocked')
        reasons = blocking_reasons
    promotion_next_action = promotion_replay_readiness.get('recommended_next_action') if isinstance(promotion_replay_readiness, dict) else None
    recommended_next_action = promotion_next_action
    blocking_summary = None
    if promotion_replay_readiness.get('state') in {'not_ready', 'blocked'}:
        blocking_summary = {
            'schema_version': 'promotion-followthrough-blocker-v1',
            'source': 'promotion_replay_readiness',
            'state': promotion_replay_readiness.get('state'),
            'reason': promotion_replay_readiness.get('reason'),
            'recommended_next_action': promotion_next_action or 'resolve_promotion_replay_blocker',
            'missing_records': promotion_replay_readiness.get('missing_records') or [],
            'readiness_reasons': promotion_replay_readiness.get('readiness_reasons') or [],
            'candidate_path': promotion_replay_readiness.get('candidate_path'),
            'artifact_path': promotion_replay_readiness.get('artifact_path'),
        }
    status = 'healthy_progress' if material_allows_healthy and not reasons else ('stagnant' if any(reason in reasons for reason in {'same_task_streak', 'discarded_experiment', 'terminal_noop', 'material_progress_missing', 'runtime_parity_blocked', 'ambition_underutilized', 'hypothesis_dynamics_stagnant', 'promotion_lifecycle_blocked', 'promotion_readiness_not_ready', 'strong_reflection_not_fresh', 'recent_window_discard_only', 'subagent_evidence_stale', 'subagent_request_unresolved'}) else 'healthy')
    return {
        'schema_version': 'autonomy-verdict-v1',
        'state': status,
        'reasons': reasons,
        'historical_reasons': historical_reasons,
        'current_task_id': (plan_latest or {}).get('current_task_id') or (plan_latest or {}).get('current_task'),
        'pass_streak': analytics.get('current_streak'),
        'material_progress': material_progress or None,
        'ambition_utilization': ambition_utilization or None,
        'promotion_replay_readiness': promotion_replay_readiness or None,
        'recommended_next_action': recommended_next_action,
        'blocking_summary': blocking_summary,
    }


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
_SELECTED_TASK_ID = re.compile(r'\[task_id=([^\]]+)\]')


def _selected_task_id(value) -> str | None:
    if isinstance(value, dict):
        candidate = _first_present(value, ('current_task_id', 'currentTaskId', 'task_id', 'taskId', 'id'))
        return str(candidate) if _has_value(candidate) else None
    if isinstance(value, list):
        for item in value:
            candidate = _selected_task_id(item)
            if _has_value(candidate):
                return candidate
        return None
    if isinstance(value, str):
        match = _SELECTED_TASK_ID.search(value)
        if match:
            return match.group(1).strip() or None
    return None


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



def _normalize_task_identity_text(value) -> str | None:
    if not _has_value(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = re.sub(r'\s+', ' ', text).casefold()
    if normalized in {'unknown', 'none', 'null', 'missing', 'not_present', 'absent', 'n/a', 'na'}:
        return None
    return normalized



def _task_identity_tokens(value) -> set[str]:
    tokens: set[str] = set()
    if isinstance(value, dict):
        for key in (
            'current_task_id', 'currentTaskId', 'task_id', 'taskId', 'id',
            'current_task', 'currentTask', 'selected_task_title', 'selectedTaskTitle',
            'selected_task_label', 'selectedTaskLabel', 'title', 'task', 'label', 'name', 'text', 'summary',
            'selected_tasks', 'selectedTasks', 'selected_tasks_text', 'selectedTasksText',
        ):
            candidate = value.get(key)
            if _has_value(candidate):
                tokens.update(_task_identity_tokens(candidate))
        return tokens
    if isinstance(value, list):
        for item in value:
            tokens.update(_task_identity_tokens(item))
        return tokens
    if isinstance(value, str):
        normalized = _normalize_task_identity_text(value)
        if normalized:
            tokens.add(normalized)
        cleaned = _SELECTED_TASK_LABEL_SUFFIX.sub('', value).strip()
        if cleaned:
            normalized_cleaned = _normalize_task_identity_text(cleaned)
            if normalized_cleaned:
                tokens.add(normalized_cleaned)
        task_id = _selected_task_id(value)
        if task_id:
            normalized_task_id = _normalize_task_identity_text(task_id)
            if normalized_task_id:
                tokens.add(normalized_task_id)
        title = _selected_task_title(value)
        if title:
            normalized_title = _normalize_task_identity_text(title)
            if normalized_title:
                tokens.add(normalized_title)
        return tokens
    normalized = _normalize_task_identity_text(value)
    if normalized:
        tokens.add(normalized)
    return tokens



def _task_identities_match(left, right) -> bool:
    left_tokens = _task_identity_tokens(left)
    right_tokens = _task_identity_tokens(right)
    return bool(left_tokens & right_tokens)



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
    budget_used_payload = _first_present(experiment_payload, ('budget_used', 'budgetUsed'))
    if budget_used_payload is None:
        budget_used_payload = _first_present(payload, ('budget_used', 'budgetUsed'))
    if isinstance(budget_used_payload, str):
        parsed_budget_used = _json_loads_any(budget_used_payload)
        if isinstance(parsed_budget_used, dict):
            budget_used_payload = parsed_budget_used
    if not isinstance(budget_used_payload, dict):
        budget_used_payload = None
    subagent_consumption = _first_present(experiment_payload, ('subagent_consumption', 'subagentConsumption'))
    if subagent_consumption is None:
        subagent_consumption = _first_present(payload, ('subagent_consumption', 'subagentConsumption'))
    if isinstance(subagent_consumption, str):
        parsed_subagent_consumption = _json_loads_any(subagent_consumption)
        if isinstance(parsed_subagent_consumption, dict):
            subagent_consumption = parsed_subagent_consumption
    if not isinstance(subagent_consumption, dict):
        subagent_count = None
        if isinstance(budget_used_payload, dict):
            subagent_count = budget_used_payload.get('subagents')
        subagent_consumption = {
            'schema_version': 'subagent-consumption-v1',
            'state': 'consumed' if isinstance(subagent_count, (int, float)) and subagent_count > 0 else 'unused',
            'used': int(subagent_count or 0) if isinstance(subagent_count, (int, float)) else 0,
            'source': 'budget_used' if isinstance(budget_used_payload, dict) else 'missing_explicit_subagent_consumption',
        }
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
    if not _has_value(phase):
        if _has_value(outcome):
            phase = 'completed'
        elif str(status).upper() in {'PASS', 'FAIL', 'BLOCK', 'ERROR'}:
            phase = 'completed'
    metric_name = _first_present(experiment_payload, ('metric_name', 'metricName'))
    metric_baseline = _first_present(experiment_payload, ('metric_baseline', 'metricBaseline'))
    metric_current = _first_present(experiment_payload, ('metric_current', 'metricCurrent'))
    metric_frontier = _first_present(experiment_payload, ('metric_frontier', 'metricFrontier'))
    contract_path = _first_present(experiment_payload, ('contract_path', 'contractPath'))
    revert_payload = _first_present(experiment_payload, ('revert', 'revertRecord', 'revert_record'))
    revert_path = _first_present(experiment_payload, ('revert_path', 'revertPath'))
    revert_status = _first_present(experiment_payload, ('revert_status', 'revertStatus'))
    revert_reason = _first_present(experiment_payload, ('revert_reason', 'revertReason', 'reason'))
    revert_terminal = _first_present(experiment_payload, ('revert_terminal', 'revertTerminal', 'terminal'))
    revert_contract_path = _first_present(experiment_payload, ('revert_contract_path', 'revertContractPath'))
    if not isinstance(revert_payload, dict):
        revert_payload = None
    if revert_payload is None and (_has_value(revert_path) or _has_value(revert_status) or _has_value(revert_reason)):
        revert_payload = {
            'revert_path': revert_path,
            'revert_status': revert_status,
            'reason': revert_reason,
            'terminal': revert_terminal,
            'contract_path': revert_contract_path,
        }
    if isinstance(revert_payload, dict):
        revert_path = revert_payload.get('revert_path') or revert_payload.get('revertPath') or revert_path
        revert_status = revert_payload.get('revert_status') or revert_payload.get('revertStatus') or revert_status
        revert_reason = revert_payload.get('reason') or revert_payload.get('revert_reason') or revert_reason
        revert_terminal = revert_payload.get('terminal') if revert_payload.get('terminal') is not None else revert_terminal
        revert_contract_path = revert_payload.get('contract_path') or revert_payload.get('contractPath') or revert_contract_path
    is_experiment_snapshot = any(_has_value(value) for value in (experiment_id, title_value, reward_signal, phase, outcome, metric_name, contract_path, revert_path, revert_status))
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
        'budget_used': budget_used_payload,
        'subagent_consumption': subagent_consumption,
        'outcome': str(outcome) if _has_value(outcome) else None,
        'metric_name': str(metric_name) if _has_value(metric_name) else None,
        'metric_baseline': metric_baseline,
        'metric_current': metric_current,
        'metric_frontier': metric_frontier,
        'contract_path': str(contract_path) if _has_value(contract_path) else None,
        'revert_required': bool(experiment_payload.get('revert_required')),
        'revert_status': str(revert_status) if _has_value(revert_status) else None,
        'revert_path': str(revert_path) if _has_value(revert_path) else None,
        'revert_reason': str(revert_reason) if _has_value(revert_reason) else None,
        'revert_terminal': bool(revert_terminal) if _has_value(revert_terminal) else None,
        'revert_contract_path': str(revert_contract_path) if _has_value(revert_contract_path) else None,
        'revert': revert_payload,
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

    latest = current_experiment or current_budget or (experiment_history[0] if experiment_history else None) or (budget_history[0] if budget_history else None)
    summary = {
        'schema_version': 'experiment-summary-v1',
        'available': bool(latest),
        'latest_experiment_id': latest.get('experiment_id') if isinstance(latest, dict) else None,
        'latest_title': latest.get('title') if isinstance(latest, dict) else None,
        'latest_status': latest.get('status') if isinstance(latest, dict) else None,
        'latest_outcome': latest.get('outcome') if isinstance(latest, dict) else None,
        'reward_source': reward_source,
        'current_reward_text': reward_text,
        'experiment_count': len(experiment_history),
        'budget_count': len(budget_history),
    }
    items = experiment_history[:10] if experiment_history else budget_history[:10]
    return {
        'available': bool(experiment_history or current_budget),
        'state_roots': [str(root) for root in state_roots],
        'candidate_files': [str(path) for path in candidate_files[:25]],
        'experiment_history': experiment_history[:10],
        'budget_history': budget_history[:10],
        'current_experiment': current_experiment,
        'current_budget': current_budget,
        'latest': latest,
        'summary': summary,
        'items': items,
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

    def _snapshot(backlog_payload, backlog_path: str | None, source: str, collected_at: str | None = None) -> dict:
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
            'available': backlog_payload is not None,
            'source': source,
            'path': backlog_path,
            'backlog_path': backlog_path,
            'backlog_collected_at': collected_at,
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
        }

    local_backlog_payload = None
    local_backlog_path = None
    for path in candidate_files:
        payload = _structured_file_payload(path)
        if isinstance(payload, dict):
            local_backlog_payload = payload
            local_backlog_path = path
            break

    live_backlog_path = f"{cfg.eeepc_state_root}/hypotheses/backlog.json"
    live_backlog_payload = None
    live_errors: dict[str, str] = {}
    if cfg.eeepc_ssh_key.exists():
        remote = _remote_file_preview(cfg, live_backlog_path, max_chars=20000)
        if remote.get('exists') and remote.get('preview'):
            try:
                parsed = json.loads(str(remote.get('preview')))
                if isinstance(parsed, dict):
                    live_backlog_payload = parsed
            except Exception as exc:
                live_errors['eeepc_parse_error'] = str(exc)
        elif remote.get('preview'):
            live_errors['eeepc_preview_error'] = str(remote.get('preview'))[:500]

    local_snapshot = _snapshot(local_backlog_payload, str(local_backlog_path) if local_backlog_path else None, 'local', datetime.fromtimestamp(local_backlog_path.stat().st_mtime, tz=timezone.utc).isoformat().replace('+00:00', 'Z') if local_backlog_path else None)
    live_snapshot = _snapshot(live_backlog_payload, live_backlog_path, 'eeepc')
    canonical_snapshot = live_snapshot if live_snapshot['available'] else local_snapshot
    canonical_source = canonical_snapshot['source'] if canonical_snapshot['available'] else None

    mismatch_reasons: list[str] = []
    if local_snapshot['available'] and live_snapshot['available']:
        if local_snapshot['entry_count'] != live_snapshot['entry_count']:
            mismatch_reasons.append('entry_count_drift')
        if (
            local_snapshot.get('selected_hypothesis_id') != live_snapshot.get('selected_hypothesis_id')
            or local_snapshot.get('selected_hypothesis_title') != live_snapshot.get('selected_hypothesis_title')
        ):
            mismatch_reasons.append('selected_hypothesis_drift')
    elif local_snapshot['available'] and not live_snapshot['available']:
        mismatch_reasons.append('live_backlog_unavailable')
    elif live_snapshot['available'] and not local_snapshot['available']:
        mismatch_reasons.append('local_backlog_unavailable')
    else:
        mismatch_reasons.extend(['local_backlog_unavailable', 'live_backlog_unavailable'])
    if live_errors:
        mismatch_reasons.extend(sorted(live_errors))

    return {
        'available': canonical_snapshot['available'],
        'source': canonical_source,
        'canonical_source': canonical_source,
        'canonical_path': canonical_snapshot['path'],
        'path': canonical_snapshot['path'],
        'backlog_path': canonical_snapshot['path'],
        'backlog_collected_at': canonical_snapshot['backlog_collected_at'],
        'local_path': local_snapshot['path'],
        'live_path': live_snapshot['path'],
        'local_entry_count': local_snapshot['entry_count'] if local_snapshot['available'] else None,
        'live_entry_count': live_snapshot['entry_count'] if live_snapshot['available'] else None,
        'canonical_entry_count': canonical_snapshot['entry_count'] if canonical_snapshot['available'] else None,
        'source_mismatch': bool(local_snapshot['available'] and live_snapshot['available'] and mismatch_reasons),
        'mismatch_reasons': mismatch_reasons,
        'state_roots': [str(root) for root in state_roots],
        'candidate_files': [str(path) for path in candidate_files[:25]],
        'research_feed': canonical_snapshot['research_feed'],
        'schema_version': canonical_snapshot['schema_version'],
        'model': canonical_snapshot['model'],
        'entry_count': canonical_snapshot['entry_count'],
        'selected_hypothesis_id': canonical_snapshot['selected_hypothesis_id'],
        'selected_hypothesis_title': canonical_snapshot['selected_hypothesis_title'],
        'selected_hypothesis_status': canonical_snapshot['selected_hypothesis_status'],
        'selected_hypothesis_score': canonical_snapshot['selected_hypothesis_score'],
        'selected_hypothesis_score_text': canonical_snapshot['selected_hypothesis_score_text'],
        'selected_hypothesis_wsjf': canonical_snapshot['selected_hypothesis_wsjf'],
        'selected_hypothesis_wsjf_text': canonical_snapshot['selected_hypothesis_wsjf_text'],
        'selected_hypothesis_execution_spec': canonical_snapshot['selected_hypothesis_execution_spec'],
        'selected_hypothesis_execution_spec_goal': canonical_snapshot['selected_hypothesis_execution_spec_goal'],
        'selected_hypothesis_execution_spec_task': canonical_snapshot['selected_hypothesis_execution_spec_task'],
        'selected_hypothesis_execution_spec_acceptance': canonical_snapshot['selected_hypothesis_execution_spec_acceptance'],
        'selected_hypothesis_execution_spec_budget': canonical_snapshot['selected_hypothesis_execution_spec_budget'],
        'selected_hypothesis_execution_spec_budget_text': canonical_snapshot['selected_hypothesis_execution_spec_budget_text'],
        'selected_hypothesis_hadi': canonical_snapshot['selected_hypothesis_hadi'],
        'selected_hypothesis_hadi_text': canonical_snapshot['selected_hypothesis_hadi_text'],
        'top_entries': canonical_snapshot['top_entries'],
        'empty_state_reason': (
            'No hypothesis backlog file was found under workspace/state/hypotheses/backlog.json.'
            if not canonical_snapshot['available'] else None
        ),
    }


def _reconcile_hypotheses_visibility_with_runtime(hypotheses_visibility: dict, runtime_parity: dict | None, plan_latest: dict | None = None) -> dict:
    if not isinstance(hypotheses_visibility, dict) or not isinstance(runtime_parity, dict):
        return hypotheses_visibility
    canonical_task_id = runtime_parity.get('canonical_current_task_id')
    if not _has_value(canonical_task_id):
        return hypotheses_visibility
    authority_resolution = runtime_parity.get('authority_resolution')
    runtime_reasons = runtime_parity.get('reasons') if isinstance(runtime_parity.get('reasons'), list) else []
    trusted_authority = authority_resolution in {'fresh_live_terminal_selfevo_retire', 'fresh_live_active_lane', 'fresh_live_synthesized_materialization', 'fresh_live_post_materialization_reward', 'fresh_live_synthesis_candidate', 'fresh_live_failure_learning_handoff'}
    if runtime_parity.get('state') != 'healthy' and not trusted_authority:
        return hypotheses_visibility
    if 'current_task_drift' in runtime_reasons and not trusted_authority:
        return hypotheses_visibility
    if hypotheses_visibility.get('selected_hypothesis_id') == canonical_task_id:
        return hypotheses_visibility

    reconciled = dict(hypotheses_visibility)
    stale_id = reconciled.get('selected_hypothesis_id')
    stale_title = reconciled.get('selected_hypothesis_title')
    canonical_title = None
    canonical_entry_found = False
    for entry in reconciled.get('top_entries') or []:
        if isinstance(entry, dict) and entry.get('hypothesis_id') == canonical_task_id:
            canonical_title = entry.get('title')
            canonical_entry_found = True
            break
    if not canonical_entry_found and not trusted_authority:
        mismatch_reasons = list(reconciled.get('mismatch_reasons') or [])
        if 'selected_hypothesis_not_hydrated' not in mismatch_reasons:
            mismatch_reasons.append('selected_hypothesis_not_hydrated')
        reconciled['mismatch_reasons'] = mismatch_reasons
        reconciled['canonical_runtime_task_id'] = str(canonical_task_id)
        reconciled['canonical_runtime_authority_resolution'] = authority_resolution
        reconciled['runtime_reconciled_selected_hypothesis'] = False
        return reconciled
    if not canonical_title and isinstance(plan_latest, dict) and plan_latest.get('current_task_id') == canonical_task_id:
        canonical_title = plan_latest.get('current_task') or plan_latest.get('selected_task_title')
    canonical_title = canonical_title or canonical_task_id
    reconciled.update({
        'selected_hypothesis_id': str(canonical_task_id),
        'selected_hypothesis_title': str(canonical_title),
        'runtime_reconciled_selected_hypothesis': True,
        'stale_selected_hypothesis_id': stale_id,
        'stale_selected_hypothesis_title': stale_title,
        'canonical_runtime_task_id': str(canonical_task_id),
        'canonical_runtime_authority_resolution': authority_resolution,
    })
    mismatch_reasons = list(reconciled.get('mismatch_reasons') or [])
    if 'selected_hypothesis_reconciled_to_runtime' not in mismatch_reasons:
        mismatch_reasons.append('selected_hypothesis_reconciled_to_runtime')
    reconciled['mismatch_reasons'] = mismatch_reasons
    return reconciled


def _terminal_issue_evidence_is_live(issue: dict | None) -> bool:
    if not isinstance(issue, dict) or not issue:
        return False
    issue_state = str(issue.get('github_issue_state') or issue.get('state') or '').strip().upper()
    status = str(issue.get('terminal_status') or issue.get('status') or '').strip().lower()
    retry_allowed = issue.get('retry_allowed')
    terminal_status = status.startswith('terminal_') or status in {'merged', 'closed', 'terminal-merged', 'terminal-closed'}
    if issue_state == 'CLOSED' and retry_allowed is False:
        return False
    if terminal_status and retry_allowed is False:
        return False
    return True


def _terminal_pr_evidence_is_live(pr: dict | None) -> bool:
    if not isinstance(pr, dict) or not pr:
        return False
    state = str(pr.get('state') or '').strip().upper()
    if pr.get('merged') is True or state == 'MERGED':
        return False
    if state == 'CLOSED':
        return False
    return True


def _selected_hypothesis_terminal_evidence(cfg: DashboardConfig) -> tuple[dict | None, dict | None]:
    state_root = cfg.nanobot_repo_root / 'workspace' / 'state' / 'self_evolution'
    current_state = _json_file(state_root / 'current_state.json')
    latest_noop = _json_file(state_root / 'runtime' / 'latest_noop.json')
    issue = current_state.get('selfevo_issue') if isinstance(current_state.get('selfevo_issue'), dict) else latest_noop.get('selfevo_issue') if isinstance(latest_noop.get('selfevo_issue'), dict) else None
    pr = current_state.get('last_pr') if isinstance(current_state.get('last_pr'), dict) else latest_noop.get('pr') if isinstance(latest_noop.get('pr'), dict) else None
    issue_live = _terminal_issue_evidence_is_live(issue)
    pr_live = _terminal_pr_evidence_is_live(pr)
    pr_state = str(pr.get('state') or '').strip().upper() if isinstance(pr, dict) else ''
    if not issue_live and pr_live and isinstance(issue, dict) and not pr_state and pr.get('merged') is not False:
        pr_live = False
    return issue if issue_live else None, pr if pr_live else None


def _selected_hypothesis_diagnostics(*, cycles: list[dict], hypotheses_visibility: dict, credits_visibility: dict, cfg: DashboardConfig) -> dict:
    visibility = hypotheses_visibility if isinstance(hypotheses_visibility, dict) else {}
    selected_id = visibility.get('selected_hypothesis_id')
    selected_title = visibility.get('selected_hypothesis_title')
    selected_score = visibility.get('selected_hypothesis_score')
    selected_wsjf = visibility.get('selected_hypothesis_wsjf')

    def _report_source_candidates(detail: dict) -> list[str]:
        candidates: list[str] = []
        for key in ('report_source', 'reportSource', 'report_path', 'reportPath'):
            value = detail.get(key)
            if _has_value(value):
                candidates.append(str(value))
        artifact_paths = detail.get('artifact_paths')
        if isinstance(artifact_paths, str):
            parsed = _json_loads_any(artifact_paths)
            if isinstance(parsed, list):
                artifact_paths = parsed
        if isinstance(artifact_paths, list):
            for artifact_path in artifact_paths:
                if _has_value(artifact_path):
                    candidates.append(str(artifact_path))
        artifact_paths_json = detail.get('artifact_paths_json')
        if isinstance(artifact_paths_json, str):
            for artifact_path in _json_loads_list(artifact_paths_json):
                if _has_value(artifact_path):
                    candidates.append(str(artifact_path))
        seen: set[str] = set()
        ordered_candidates: list[str] = []
        for candidate in candidates:
            normalized = candidate.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered_candidates.append(normalized)
        return ordered_candidates

    def _hydrate_cycle_detail(row: dict) -> dict:
        detail = dict(row.get('detail')) if isinstance(row.get('detail'), dict) else {}
        hydrated = _canonical_report_payload(cfg, detail, allow_remote=True)
        return hydrated

    def _matches_selected(row: dict) -> bool:
        detail = _hydrate_cycle_detail(row)
        task_id = _first_present(detail, ('current_task_id', 'currentTaskId', 'task_id', 'taskId')) or row.get('title')
        hypothesis_id = _first_present(detail, ('selected_hypothesis_id', 'selectedHypothesisId', 'hypothesis_id', 'hypothesisId')) or task_id
        title_candidates = [
            row.get('title'),
            detail.get('selected_hypothesis_title'),
            detail.get('hypothesis_title'),
            detail.get('current_task'),
            detail.get('current_task_title'),
            detail.get('title'),
        ]
        if _has_value(selected_id):
            return str(task_id) == str(selected_id) or str(hypothesis_id) == str(selected_id)
        if _has_value(selected_title):
            return any(_has_value(candidate) and str(candidate) == str(selected_title) for candidate in title_candidates)
        return False

    ordered_cycles = sorted(cycles or [], key=lambda row: _coerce_timestamp(row.get('collected_at')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    matched_cycles = [row for row in ordered_cycles if _matches_selected(row)]
    if matched_cycles:
        reference_dt = _coerce_timestamp(matched_cycles[0].get('collected_at')) or datetime.now(timezone.utc)
    elif ordered_cycles:
        reference_dt = _coerce_timestamp(ordered_cycles[0].get('collected_at')) or datetime.now(timezone.utc)
    else:
        reference_dt = datetime.now(timezone.utc)
    window_start = reference_dt - timedelta(hours=24)

    window_cycles = []
    for row in matched_cycles:
        ts = _coerce_timestamp(row.get('collected_at'))
        if ts is not None and ts >= window_start:
            window_cycles.append(row)

    run_streak = 0
    for row in ordered_cycles:
        ts = _coerce_timestamp(row.get('collected_at'))
        if ts is None or ts < window_start:
            break
        if _matches_selected(row):
            run_streak += 1
        else:
            break

    def _cycle_detail(row: dict) -> dict:
        return _hydrate_cycle_detail(row)

    def _cycle_outcome(row: dict) -> str | None:
        detail = _cycle_detail(row)
        experiment = detail.get('experiment') if isinstance(detail.get('experiment'), dict) else {}
        return experiment.get('outcome') or detail.get('outcome') or row.get('status')

    def _cycle_budget_used(row: dict) -> dict:
        detail = _cycle_detail(row)
        budget_used = detail.get('budget_used') if isinstance(detail.get('budget_used'), dict) else {}
        if not budget_used:
            experiment = detail.get('experiment') if isinstance(detail.get('experiment'), dict) else {}
            budget_used = experiment.get('budget_used') if isinstance(experiment.get('budget_used'), dict) else {}
        if not budget_used and isinstance(detail.get('current_plan'), dict):
            budget_used = detail['current_plan'].get('budget_used') if isinstance(detail['current_plan'].get('budget_used'), dict) else {}
        return budget_used if isinstance(budget_used, dict) else {}

    def _cycle_feedback_decision(row: dict):
        detail = _cycle_detail(row)
        feedback_decision = detail.get('feedback_decision')
        return feedback_decision if isinstance(feedback_decision, dict) else feedback_decision

    outcome_counts = {'discard': 0, 'pass': 0, 'block': 0, 'other': 0}
    budget_sum = {'requests': 0, 'tool_calls': 0, 'subagents': 0, 'elapsed_seconds': 0}
    for row in window_cycles:
        outcome = str(_cycle_outcome(row) or 'other').lower()
        if outcome not in outcome_counts:
            outcome = 'other'
        outcome_counts[outcome] += 1
        budget_used = _cycle_budget_used(row)
        for key in budget_sum:
            try:
                budget_sum[key] += int(budget_used.get(key) or 0)
            except Exception:
                continue

    latest_cycle = window_cycles[0] if window_cycles else (matched_cycles[0] if matched_cycles else None)
    reward_gate = credits_visibility.get('current', {}).get('reward_gate') if isinstance(credits_visibility.get('current'), dict) else {}
    if not isinstance(reward_gate, dict):
        reward_gate = {}
    terminal_issue, terminal_pr = _selected_hypothesis_terminal_evidence(cfg)
    run_count = len(window_cycles)
    selected_hypothesis_repetition = run_count >= 5
    state = 'stagnant' if (
        run_count
        and selected_hypothesis_repetition
        and outcome_counts['discard'] == run_count
        and reward_gate.get('status') == 'suppressed'
        and (terminal_issue or terminal_pr)
    ) else 'healthy'
    reasons: list[str] = []
    if run_count:
        if selected_hypothesis_repetition:
            reasons.append('selected_hypothesis_repetition')
        if outcome_counts['discard'] == run_count:
            reasons.append('discard_only_selected_hypothesis')
        if reward_gate.get('status') == 'suppressed':
            reasons.append('suppressed_reward_gate')
        if terminal_issue or terminal_pr:
            reasons.append('terminal_selfevo_issue_present')
    if state == 'stagnant':
        reasons.insert(0, 'selected_hypothesis_stagnant')

    return {
        'schema_version': 'hypothesis-dynamics-v1',
        'state': state,
        'reasons': reasons,
        'selected_hypothesis_id': str(selected_id) if _has_value(selected_id) else None,
        'selected_hypothesis_title': str(selected_title) if _has_value(selected_title) else None,
        'selected_hypothesis_score': selected_score,
        'selected_hypothesis_score_text': _hypothesis_score_text(selected_score),
        'selected_hypothesis_wsjf': selected_wsjf,
        'selected_hypothesis_wsjf_text': _wsjf_text(selected_wsjf),
        'selected_hypothesis_feedback_decision': _cycle_feedback_decision(latest_cycle) if latest_cycle else None,
        'selected_hypothesis_experiment_outcome': _cycle_outcome(latest_cycle) if latest_cycle else None,
        'run_count': run_count,
        'run_streak': run_streak,
        'window_hours': 24,
        'last_24h': {
            'window_hours': 24,
            'total_runs': run_count,
            'discard_count': outcome_counts['discard'],
            'pass_count': outcome_counts['pass'],
            'block_count': outcome_counts['block'],
            'other_count': outcome_counts['other'],
            'budget_used_sum': budget_sum,
            'reward_gate': {
                'status': reward_gate.get('status'),
                'reason': reward_gate.get('reason'),
            },
            'terminal_selfevo_issue': terminal_issue,
            'terminal_selfevo_pr': terminal_pr,
            'latest_outcome': _cycle_outcome(latest_cycle) if latest_cycle else None,
            'latest_feedback_decision': _cycle_feedback_decision(latest_cycle) if latest_cycle else None,
        },
        'reward_gate': {
            'status': reward_gate.get('status'),
            'reason': reward_gate.get('reason'),
        },
        'terminal_selfevo_issue': terminal_issue,
        'terminal_selfevo_pr': terminal_pr,
        'canonical_runtime_task_id': visibility.get('canonical_runtime_task_id'),
        'canonical_runtime_authority_resolution': visibility.get('canonical_runtime_authority_resolution'),
        'runtime_reconciled_selected_hypothesis': visibility.get('runtime_reconciled_selected_hypothesis'),
        'stale_selected_hypothesis_id': visibility.get('stale_selected_hypothesis_id'),
        'stale_selected_hypothesis_title': visibility.get('stale_selected_hypothesis_title'),
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
    if not _has_value(item.get('current_task_id')):
        item['current_task_id'] = _first_present(item, ('current_task_id', 'currentTaskId', 'task_id', 'taskId'))
    if not _has_value(item.get('current_task')):
        item['current_task'] = _first_present(item, ('current_task', 'currentTask', 'current_task_id', 'currentTaskId', 'selected_task_title', 'selected_task_label'))
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

    if not _has_value(item.get('current_task_id')):
        item['current_task_id'] = _selected_task_id(selected_tasks)
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
        'current_task_id': item.get('current_task_id'),
        'current_task': item.get('current_task'),
        'cycle_id': _first_present(item, ('cycle_id', 'cycleId')),
        'report_source': item.get('report_source'),
        'updated_at': _first_present(item, ('updated_at', 'updatedAt', 'generated_at', 'generatedAt', 'collected_at')),
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


def _snapshot_source_skew(local_snapshot: dict | None, live_snapshot: dict | None) -> dict | None:
    local_snapshot = dict(local_snapshot) if isinstance(local_snapshot, dict) else {}
    live_snapshot = dict(live_snapshot) if isinstance(live_snapshot, dict) else {}
    if not local_snapshot and not live_snapshot:
        return None

    def _side(snapshot: dict, default_source: str) -> tuple[dict, set[str]]:
        raw = _json_loads_dict(snapshot.get('raw_json'))
        nested_plan = None
        if isinstance(raw, dict):
            for key in ('current_plan', 'currentPlan', 'task_plan', 'taskPlan', 'plan'):
                if isinstance(raw.get(key), dict):
                    nested_plan = raw.get(key)
                    break
        nested_plan = nested_plan if isinstance(nested_plan, dict) else {}
        side = {
            'source': snapshot.get('source') or default_source,
            'collected_at': snapshot.get('collected_at'),
            'status': snapshot.get('status'),
            'active_goal': snapshot.get('active_goal'),
            'current_task': snapshot.get('current_task') or nested_plan.get('current_task') or nested_plan.get('currentTask'),
            'current_task_id': snapshot.get('current_task_id') or snapshot.get('current_task') or nested_plan.get('current_task_id') or nested_plan.get('currentTaskId') or nested_plan.get('current_task'),
            'cycle_id': snapshot.get('cycle_id') or snapshot.get('cycleId') or nested_plan.get('cycle_id') or nested_plan.get('cycleId'),
            'updated_at': snapshot.get('updated_at') or snapshot.get('updatedAt') or snapshot.get('generated_at') or snapshot.get('generatedAt') or nested_plan.get('updated_at') or nested_plan.get('updatedAt') or nested_plan.get('generated_at') or nested_plan.get('generatedAt') or snapshot.get('collected_at'),
            'report_source': snapshot.get('report_source'),
            'task_selection_source': snapshot.get('task_selection_source') or nested_plan.get('task_selection_source') or nested_plan.get('selection_source'),
        }
        task_identity = {
            'current_task_id': side.get('current_task_id'),
            'current_task': side.get('current_task'),
            'selected_task_title': snapshot.get('selected_task_title') or nested_plan.get('selected_task_title') or nested_plan.get('selected_task_label'),
        }
        return ({key: value for key, value in side.items() if _has_value(value)}, _task_identity_tokens(task_identity))

    local_ts = _parse_timestamp(local_snapshot.get('collected_at'))
    live_ts = _parse_timestamp(live_snapshot.get('collected_at'))
    collected_at_delta_seconds = None
    direction = None
    if local_ts is not None and live_ts is not None:
        collected_at_delta_seconds = int((live_ts - local_ts).total_seconds())
        if collected_at_delta_seconds > 0:
            direction = 'live_newer'
        elif collected_at_delta_seconds < 0:
            direction = 'local_newer'
        else:
            direction = 'aligned'

    local_side, local_task_identity = _side(local_snapshot, 'repo')
    live_side, live_task_identity = _side(live_snapshot, 'eeepc')
    local_task = local_side.get('current_task_id') or local_side.get('current_task')
    live_task = live_side.get('current_task_id') or live_side.get('current_task')
    current_task_drift = None
    if local_task_identity and live_task_identity:
        current_task_drift = not bool(local_task_identity & live_task_identity)
    elif _has_value(local_task) and _has_value(live_task):
        current_task_drift = str(local_task) != str(live_task)
    local_cycle = local_side.get('cycle_id')
    live_cycle = live_side.get('cycle_id')
    cycle_drift = None
    if _has_value(local_cycle) and _has_value(live_cycle):
        cycle_drift = str(local_cycle) != str(live_cycle)

    if not local_snapshot or not live_snapshot:
        state = 'partial'
    elif collected_at_delta_seconds == 0 and current_task_drift in {False, None} and cycle_drift in {False, None}:
        state = 'aligned'
    else:
        state = 'skewed'

    reasons = []
    if current_task_drift:
        reasons.append('current_task_drift')
    if cycle_drift:
        reasons.append('cycle_drift')
    if collected_at_delta_seconds not in {None, 0}:
        reasons.append('collected_at_delta')

    return {
        'schema_version': 'source-skew-v1',
        'state': state,
        'reasons': reasons,
        'direction': direction,
        'collected_at_delta_seconds': collected_at_delta_seconds,
        'current_task_drift': current_task_drift,
        'cycle_drift': cycle_drift,
        'local': local_side,
        'live': live_side,
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


def _approval_snapshot(row) -> dict | None:
    compact = _compact_collection_row(row)
    if compact is None:
        return None
    plan_snapshot = _plan_snapshot_from_row(row)
    compact['approval_gate'] = row.get('approval_gate') if isinstance(row, dict) else None
    compact['plan_snapshot'] = {
        'collected_at': plan_snapshot.get('collected_at'),
        'source': plan_snapshot.get('source'),
        'status': plan_snapshot.get('status'),
        'current_task_id': plan_snapshot.get('current_task_id'),
        'current_task': plan_snapshot.get('current_task'),
        'task_count': plan_snapshot.get('task_count'),
        'reward_signal': plan_snapshot.get('reward_signal'),
        'reward_signal_text': plan_snapshot.get('reward_signal_text'),
        'feedback_decision': _compact_selfevo_lifecycle_evidence(plan_snapshot.get('feedback_decision')) if isinstance(plan_snapshot.get('feedback_decision'), dict) else plan_snapshot.get('feedback_decision'),
        'selected_tasks_text': plan_snapshot.get('selected_tasks_text'),
        'selected_task_title': plan_snapshot.get('selected_task_title'),
        'task_selection_source': plan_snapshot.get('task_selection_source'),
        'plan_history_count': plan_snapshot.get('plan_history_count'),
        'plan_payload_source': plan_snapshot.get('plan_payload_source'),
    }
    return compact



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



def _dashboard_remote_previews_enabled() -> bool:
    value = os.environ.get('NANOBOT_DASHBOARD_REMOTE_PREVIEWS', '0').strip().lower()
    return value in {'1', 'true', 'yes', 'on'}


def _remote_file_preview(cfg: DashboardConfig, remote_path: str, max_chars: int = 800) -> dict:
    if not _dashboard_remote_previews_enabled():
        return {'path': remote_path, 'exists': False, 'preview': None, 'disabled': True}
    max_chars = min(int(max_chars), 8000)
    cache = getattr(_remote_file_preview, '_cache', None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(_remote_file_preview, '_cache', cache)
    cache_key = (str(cfg.eeepc_ssh_host), str(cfg.eeepc_ssh_key), str(remote_path), int(max_chars))
    if cache_key in cache:
        return dict(cache[cache_key])
    shell_command = f"if [ -f {remote_path!r} ]; then timeout 2s head -c {max_chars} {remote_path!r}; else echo '__MISSING__'; fi"
    ssh_cmd = [
        'ssh',
        '-F', '/home/ozand/.ssh/config',
        '-i', str(cfg.eeepc_ssh_key),
        '-o', 'IdentitiesOnly=yes',
        '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=3',
        '-o', 'ServerAliveInterval=2',
        '-o', 'ServerAliveCountMax=1',
        cfg.eeepc_ssh_host,
        f"bash -lc {json.dumps(shell_command)}",
    ]
    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=3, check=True)
        content = proc.stdout
        if content.strip() == '__MISSING__':
            result = {'path': remote_path, 'exists': False, 'preview': None}
            cache[cache_key] = dict(result)
            return result
        result = {'path': remote_path, 'exists': True, 'preview': content[:max_chars]}
        cache[cache_key] = dict(result)
        return result
    except Exception as exc:
        result = {'path': remote_path, 'exists': False, 'preview': f'<remote preview failed: {exc}>'}
        cache[cache_key] = dict(result)
        return result



def _canonical_report_payload(cfg: DashboardConfig, payload: dict | None, *, allow_remote: bool = False) -> dict:
    hydrated = dict(payload) if isinstance(payload, dict) else {}
    report_sources: list[str] = []
    for key in ('report_source', 'reportSource', 'report_path', 'reportPath', 'source_path', 'sourcePath'):
        value = hydrated.get(key)
        if _has_value(value):
            report_sources.append(str(value))
    seen: set[str] = set()
    for report_source in report_sources:
        normalized = report_source.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        source_payload = _json_file(Path(normalized))
        if not source_payload:
            stripped = normalized.lstrip('/\\')
            for candidate in (
                cfg.nanobot_repo_root / stripped,
                cfg.project_root / stripped,
            ):
                try:
                    if candidate.exists():
                        source_payload = _json_file(candidate)
                        if source_payload:
                            hydrated.setdefault('report_source_path', str(candidate))
                            break
                except Exception:
                    continue
        if not source_payload and allow_remote and cfg.eeepc_ssh_key.exists():
            remote = _remote_file_preview(cfg, normalized, max_chars=50000)
            preview = remote.get('preview')
            if remote.get('exists') and isinstance(preview, str):
                try:
                    parsed_preview = json.loads(preview)
                except Exception:
                    parsed_preview = None
                if isinstance(parsed_preview, dict):
                    source_payload = parsed_preview
                    hydrated.setdefault('report_source_path', remote.get('path'))
        if source_payload:
            hydrated.setdefault('report_source', normalized)
            hydrated.update(source_payload)
            for key in ('current_plan', 'currentPlan', 'task_plan', 'taskPlan', 'plan', 'outbox', 'material_progress'):
                nested = source_payload.get(key)
                if isinstance(nested, dict):
                    hydrated.update(nested)
            break
    return hydrated



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
            _sort_rows_desc(_decorate_rows(fetch_events(cfg.db_path, 'eeepc', 'promotion', limit=100))) +
            _sort_rows_desc(_decorate_rows(fetch_events(cfg.db_path, 'repo', 'promotion', limit=100))),
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
        eeepc_parity_snapshot = eeepc_plan_snapshot or (_plan_snapshot_from_row(eeepc_latest) if eeepc_latest else None)
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
        subagent_visibility = _discover_subagent_requests(cfg)
        runtime_parity = _dashboard_runtime_parity(repo_plan_snapshot or plan_latest, eeepc_parity_snapshot, cfg)
        runtime_authority_resolution = runtime_parity.get('authority_resolution') if isinstance(runtime_parity, dict) else None
        authoritative_plan_latest = eeepc_plan_snapshot if runtime_authority_resolution in {'fresh_live_terminal_selfevo_retire', 'fresh_live_active_lane', 'fresh_live_synthesized_materialization', 'fresh_live_post_materialization_reward', 'fresh_live_synthesis_candidate', 'fresh_live_failure_learning_handoff'} and eeepc_plan_snapshot else plan_latest
        hypotheses_visibility = _reconcile_hypotheses_visibility_with_runtime(hypotheses_visibility, runtime_parity, authoritative_plan_latest or plan_latest)
        eeepc_privileged_rollout_readiness = _eeepc_privileged_rollout_readiness(eeepc_latest, runtime_parity)
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
        promotion_replay_readiness = _promotion_replay_readiness_from_promotions(promotions)
        if isinstance(control_plane, dict):
            control_plane = dict(control_plane)
            control_plane['material_progress'] = _reconcile_material_progress_with_subagent_visibility(
                control_plane.get('material_progress'),
                subagent_visibility,
            )
            if promotion_replay_readiness is not None:
                control_plane['promotion_replay_readiness'] = promotion_replay_readiness
        current_blocker, control_plane = _demote_resolved_source_commit_blocker(current_blocker, control_plane, promotion_replay_readiness)
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
                    'detail': row.get('detail'),
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
        ambition_utilization = _ambition_utilization_verdict(
            analytics=analytics,
            experiment_visibility=experiment_visibility,
            subagent_visibility=subagent_visibility,
        )
        strong_reflection_freshness = _strong_reflection_freshness(cfg, now, eeepc_latest=eeepc_latest)
        analytics['ambition_utilization'] = ambition_utilization
        analytics['strong_reflection_freshness'] = strong_reflection_freshness
        hypothesis_dynamics = _selected_hypothesis_diagnostics(
            cycles=cycles,
            hypotheses_visibility=hypotheses_visibility,
            credits_visibility=credits_visibility,
            cfg=cfg,
        )
        hypotheses_visibility = {**hypotheses_visibility, 'selected_hypothesis_diagnostics': hypothesis_dynamics}
        visible_plan_latest = authoritative_plan_latest or plan_latest
        autonomy_verdict = _autonomy_verdict(
            analytics=analytics,
            plan_latest=visible_plan_latest,
            experiment_visibility=experiment_visibility,
            credits_visibility=credits_visibility,
            cfg=cfg,
            material_progress=control_plane.get('material_progress') if isinstance(control_plane, dict) else None,
            runtime_parity=runtime_parity,
            ambition_utilization=ambition_utilization,
            hypothesis_dynamics=hypothesis_dynamics,
            promotion_replay_readiness=promotion_replay_readiness,
            strong_reflection_freshness=strong_reflection_freshness,
            subagent_visibility=subagent_visibility,
        )
        existing_blocker_summary = control_plane.get('blocker_summary') if isinstance(control_plane, dict) else None
        producer_has_blocker_summary = bool(
            isinstance(control_plane, dict)
            and isinstance(control_plane.get('producer_summary'), dict)
            and control_plane['producer_summary'].get('blocker_summary')
        )
        synthesized_clear_summary = bool(
            isinstance(existing_blocker_summary, dict)
            and existing_blocker_summary.get('state') == 'clear'
            and existing_blocker_summary.get('reason') == 'none'
        )
        control_blocker = control_plane.get('current_blocker') if isinstance(control_plane, dict) else None
        should_hydrate_blocker = bool(
            autonomy_verdict.get('reasons')
            and not producer_has_blocker_summary
            and (
                (isinstance(current_blocker, dict) and current_blocker.get('kind') == 'unknown')
                or control_blocker is None
                or (isinstance(control_blocker, dict) and control_blocker.get('kind') == 'unknown')
            )
        )
        if should_hydrate_blocker:
            current_blocker = dict(current_blocker) if isinstance(current_blocker, dict) else {}
            current_blocker['kind'] = 'diagnostic_gap'
            current_blocker['source'] = 'autonomy_verdict'
            reason_priority = [
                'material_progress_missing',
                'recent_window_discard_only',
                'subagent_evidence_stale',
                'subagent_request_unresolved',
                'ambition_underutilized',
            ]
            selected_reason = next((reason for reason in reason_priority if reason in autonomy_verdict['reasons']), autonomy_verdict['reasons'][0])
            current_blocker['failure_class'] = current_blocker.get('failure_class') or selected_reason
            current_blocker['blocked_next_step'] = (
                ambition_utilization.get('recommended_next_action')
                or 'inspect recent discard-only cycles, refresh subagent proof, and escalate to a bounded materialization lane'
            )
            if isinstance(control_plane, dict):
                control_plane = dict(control_plane)
                control_plane['current_blocker'] = current_blocker
                control_plane['blocker_summary'] = {
                    'schema_version': 'blocker-summary-v1',
                    'state': 'stagnant',
                    'reason': current_blocker['failure_class'],
                    'recommended_next_action': current_blocker['blocked_next_step'],
                    'source': 'autonomy_verdict',
                    'current_task_id': (visible_plan_latest or {}).get('current_task_id'),
                    'current_task_title': (visible_plan_latest or {}).get('current_task'),
                }
        analytics['runtime_parity'] = runtime_parity
        analytics['hypothesis_dynamics'] = hypothesis_dynamics
        analytics['autonomy_verdict'] = autonomy_verdict
        if isinstance(control_plane, dict):
            control_plane = dict(control_plane)
            if isinstance(visible_plan_latest, dict) and runtime_authority_resolution in {'fresh_live_terminal_selfevo_retire', 'fresh_live_active_lane', 'fresh_live_synthesized_materialization', 'fresh_live_post_materialization_reward', 'fresh_live_synthesis_candidate', 'fresh_live_failure_learning_handoff'}:
                canonical_task_id = visible_plan_latest.get('current_task_id')
                canonical_task_title = visible_plan_latest.get('current_task') or canonical_task_id
                if canonical_task_id:
                    control_plane['current_task_id'] = canonical_task_id
                    control_plane['current_task'] = canonical_task_title
                    control_plane['current_task_title'] = canonical_task_title
                    producer_summary = control_plane.get('producer_summary')
                    if isinstance(producer_summary, dict):
                        producer_summary = dict(producer_summary)
                        producer_task_plan = producer_summary.get('task_plan')
                        if isinstance(producer_task_plan, dict):
                            producer_task_plan = dict(producer_task_plan)
                            producer_task_plan['current_task_id'] = canonical_task_id
                            producer_task_plan['current_task'] = canonical_task_title
                            producer_summary['task_plan'] = producer_task_plan
                        control_plane['producer_summary'] = producer_summary
                    blocker_summary = control_plane.get('blocker_summary')
                    if isinstance(blocker_summary, dict):
                        blocker_summary = dict(blocker_summary)
                        blocker_summary['current_task_id'] = canonical_task_id
                        blocker_summary['current_task_title'] = canonical_task_title
                        blocker_summary['authority_source'] = 'canonical_live_plan'
                        control_plane['blocker_summary'] = blocker_summary
                    current_blocker_payload = control_plane.get('current_blocker')
                    if isinstance(current_blocker_payload, dict) and current_blocker_payload.get('kind') in {None, 'unknown', 'diagnostic_gap'}:
                        current_blocker_payload = dict(current_blocker_payload)
                        current_blocker_payload['current_task_id'] = canonical_task_id
                        current_blocker_payload['current_task'] = canonical_task_id
                        current_blocker_payload['current_task_title'] = canonical_task_title
                        current_blocker_payload['authority_source'] = 'canonical_live_plan'
                        control_plane['current_blocker'] = current_blocker_payload
            control_plane['material_progress'] = _material_progress_summary(control_plane.get('material_progress'))
            control_plane['runtime_parity'] = runtime_parity
            control_plane['ambition_utilization'] = ambition_utilization
            control_plane['strong_reflection_freshness'] = strong_reflection_freshness
            control_plane['hypothesis_dynamics'] = hypothesis_dynamics
            control_plane['autonomy_verdict'] = autonomy_verdict

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
            'subagent_visibility': subagent_visibility,
            'subagents_available': bool(all_subagent_events) or bool(subagent_visibility.get('requests')) or bool(subagent_visibility.get('results')),
            'subagent_latest_event': subagent_latest_event,
            'subagent_latest_age': _age_text(subagent_latest_event.get('collected_at') if subagent_latest_event else None, now),
            'experiment_visibility': experiment_visibility,
            'experiments_available': experiment_visibility['available'],
            'current_experiment': experiment_visibility['current_experiment'],
            'current_budget': experiment_visibility['current_budget'],
            'current_reward_signal': experiment_visibility['current_reward_signal'],
            'current_reward_text': experiment_visibility['current_reward_text'],
            'ambition_utilization': ambition_utilization,
            'strong_reflection_freshness': strong_reflection_freshness,
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
            'hypothesis_backlog_path': hypotheses_visibility['canonical_path'],
            'hypothesis_backlog_local_path': hypotheses_visibility['local_path'],
            'hypothesis_backlog_live_path': hypotheses_visibility['live_path'],
            'hypotheses_canonical_source': hypotheses_visibility['canonical_source'],
            'hypotheses_mismatch_reasons': hypotheses_visibility['mismatch_reasons'],
            'hypotheses_source_mismatch': hypotheses_visibility['source_mismatch'],
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
            'autonomy_verdict': autonomy_verdict,
            'runtime_parity': runtime_parity,
            'eeepc_privileged_rollout_readiness': eeepc_privileged_rollout_readiness,
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
        mission_control = _mission_control_summary(
            context=context,
            control_plane=control_plane,
            current_blocker=current_blocker,
            material_progress=control_plane.get('material_progress') if isinstance(control_plane, dict) else None,
            runtime_parity=runtime_parity,
            autonomy_verdict=autonomy_verdict,
            hypotheses_visibility=hypotheses_visibility,
            experiment_visibility=experiment_visibility,
            subagent_visibility=subagent_visibility,
            analytics=analytics,
        )
        context['mission_control'] = mission_control

        if path == '/api/mission-control':
            body = json.dumps(mission_control, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

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
            visible_plan_latest = authoritative_plan_latest or plan_latest
            producer_plan = ((control_plane.get('producer_summary') or {}).get('task_plan') if isinstance(control_plane, dict) and isinstance(control_plane.get('producer_summary'), dict) else None) or {}
            producer_feedback = producer_plan.get('feedback_decision') if isinstance(producer_plan.get('feedback_decision'), dict) else {}
            material_progress = _material_progress_summary(control_plane.get('material_progress') if isinstance(control_plane, dict) else None)
            task_truth = _task_plan_truth(producer_plan)
            canonical_current_task_id = task_truth.get('current_task_id') or (visible_plan_latest.get('current_task_id') if visible_plan_latest else None)
            canonical_current_task = (_first_present(producer_plan, ('current_task', 'currentTask')) if isinstance(producer_plan, dict) else None) or (visible_plan_latest.get('current_task') if visible_plan_latest and visible_plan_latest.get('current_task') else task_truth.get('current_task'))
            runtime_canonical_task_id = runtime_parity.get('canonical_current_task_id') if isinstance(runtime_parity, dict) else None
            runtime_reasons = runtime_parity.get('reasons') if isinstance(runtime_parity, dict) and isinstance(runtime_parity.get('reasons'), list) else []
            runtime_reconciled_current_task_id = False
            if (
                _has_value(runtime_canonical_task_id)
                and runtime_canonical_task_id != canonical_current_task_id
                and 'current_task_drift' not in runtime_reasons
                and (
                    'legacy_live_reward_loop_current_task' in runtime_reasons
                    or runtime_authority_resolution in {'fresh_live_terminal_selfevo_retire', 'fresh_live_active_lane', 'fresh_live_synthesized_materialization', 'fresh_live_post_materialization_reward', 'fresh_live_synthesis_candidate', 'fresh_live_failure_learning_handoff'}
                    or runtime_canonical_task_id in str(canonical_current_task or '')
                    or runtime_canonical_task_id == _selected_task_id(task_truth.get('selected_tasks'))
                )
            ):
                canonical_current_task_id = runtime_canonical_task_id
                canonical_current_task = runtime_canonical_task_id
                runtime_reconciled_current_task_id = True
            canonical_task_plan = dict(task_truth['task_plan'])
            if _has_value(canonical_current_task_id) and (
                not _has_value(canonical_task_plan.get('current_task_id'))
                or runtime_reconciled_current_task_id
                or canonical_current_task_id == _selected_task_id(canonical_task_plan.get('selected_tasks'))
            ):
                canonical_task_plan['current_task_id'] = canonical_current_task_id
            if _has_value(canonical_current_task) and (
                not _has_value(canonical_task_plan.get('current_task'))
                or runtime_reconciled_current_task_id
            ):
                canonical_task_plan['current_task'] = canonical_current_task
            if visible_plan_latest:
                for source_key, target_key in (
                    ('selected_tasks', 'selected_tasks'),
                    ('selected_tasks_text', 'selected_tasks'),
                    ('selected_task_title', 'selected_task_title'),
                    ('task_selection_source', 'task_selection_source'),
                ):
                    value = visible_plan_latest.get(source_key)
                    if _has_value(value) and value != 'unknown':
                        if runtime_authority_resolution in {'fresh_live_terminal_selfevo_retire', 'fresh_live_active_lane', 'fresh_live_synthesized_materialization', 'fresh_live_post_materialization_reward', 'fresh_live_synthesis_candidate', 'fresh_live_failure_learning_handoff'}:
                            canonical_task_plan[target_key] = value
                        else:
                            canonical_task_plan.setdefault(target_key, value)
            feedback_decision = (visible_plan_latest['feedback_decision'] if visible_plan_latest and visible_plan_latest.get('feedback_decision') else producer_plan.get('feedback_decision'))
            if not isinstance(feedback_decision, dict):
                feedback_decision = {}
            next_task_id = feedback_decision.get('selected_task_id') or feedback_decision.get('selectedTaskId')
            next_task_title = feedback_decision.get('selected_task_title') or feedback_decision.get('selectedTaskTitle')
            next_task_label = feedback_decision.get('selected_task_label') or feedback_decision.get('selectedTaskLabel')
            next_task_source = feedback_decision.get('selection_source') or feedback_decision.get('selectionSource')
            payload = {
                'current_plan': visible_plan_latest,
                'current_plan_source': visible_plan_latest['source'] if visible_plan_latest else None,
                'current_task_id': canonical_current_task_id,
                'current_task': canonical_current_task,
                'task_plan': canonical_task_plan,
                'next_task_id': next_task_id,
                'next_task_title': next_task_title,
                'next_task_label': next_task_label,
                'next_task_source': next_task_source,
                'selected_task_title': (visible_plan_latest['selected_task_title'] if visible_plan_latest and visible_plan_latest.get('selected_task_title') else producer_feedback.get('selected_task_title') or producer_feedback.get('selected_task_label')),
                'feedback_decision': feedback_decision,
                'task_selection_source': (visible_plan_latest['task_selection_source'] if visible_plan_latest and visible_plan_latest.get('task_selection_source') else producer_plan.get('task_selection_source') or producer_feedback.get('selection_source')),
                'selected_tasks_text': (visible_plan_latest['selected_tasks_text'] if visible_plan_latest and visible_plan_latest.get('selected_tasks_text') and visible_plan_latest.get('selected_tasks_text') != 'unknown' else _selected_tasks_text(producer_plan.get('selected_tasks') or producer_feedback.get('selected_task_label') or producer_feedback.get('selected_task_title'))),
                'plan_history_count': len(plan_history),
                'recent_plan_history': plan_history[:10],
                'material_progress': material_progress,
                'runtime_parity': runtime_parity,
                'autonomy_verdict': autonomy_verdict,
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
                'latest': experiment_visibility.get('latest'),
                'summary': experiment_visibility.get('summary'),
                'items': experiment_visibility.get('items'),
                'credits': credits_visibility,
                'empty_state_reason': experiment_visibility['empty_state_reason'],
                'material_progress': _material_progress_summary(control_plane.get('material_progress') if isinstance(control_plane, dict) else None),
                'runtime_parity': runtime_parity,
                'autonomy_verdict': autonomy_verdict,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/credits':
            body = json.dumps(credits_visibility, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/hypotheses':
            payload = {**hypotheses_visibility}
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json; charset=utf-8')])
            return [body]

        if path == '/api/subagents':
            payload = {
                **subagent_visibility,
                'latest_request': subagent_visibility.get('latest_request'),
                'latest_result': subagent_visibility.get('latest_result'),
                'latest_telemetry': subagent_latest_event or subagent_visibility.get('latest_telemetry'),
                'events': subagent_events,
                'summary': {
                    **subagent_visibility.get('summary', {}),
                    'total_events': len(all_subagent_events),
                    'filtered_events': len(subagent_events),
                    'sources': subagent_sources,
                    'statuses': subagent_statuses,
                    'origins': subagent_origins,
                },
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
                    snapshot for snapshot in (_approval_snapshot(r) for r in (eeepc_rows + repo_rows)) if snapshot is not None
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
            body = json.dumps({
                'analytics': analytics,
                'current_blocker': current_blocker,
                'autonomy_verdict': autonomy_verdict,
                'material_progress': _material_progress_summary(control_plane.get('material_progress') if isinstance(control_plane, dict) else None),
                'runtime_parity': runtime_parity,
                'hypothesis_dynamics': hypothesis_dynamics,
            }, ensure_ascii=False, indent=2).encode('utf-8')
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
                'blocker_summary': control_plane.get('blocker_summary'),
                'material_progress': _material_progress_summary(control_plane.get('material_progress') if isinstance(control_plane, dict) else None),
                'autonomy_verdict': autonomy_verdict,
                'runtime_parity': runtime_parity,
                'hypothesis_dynamics': hypothesis_dynamics,
                'ambition_utilization': ambition_utilization,
                'strong_reflection_freshness': strong_reflection_freshness,
                'eeepc_privileged_rollout_readiness': eeepc_privileged_rollout_readiness,
                'host_resources': dict(repo_latest).get('host_resources') if repo_latest else None,
                'host_resources': (control_plane.get('host_resources') if isinstance(control_plane, dict) else None),
                'capabilities': control_plane.get('capabilities'),
                'runtime_source': control_plane.get('runtime_source'),
                'subagent_rollup': control_plane.get('subagent_rollup') or (dict(repo_latest).get('subagent_rollup') if repo_latest else None),
                'eeepc_reachability': eeepc_reachability,
                'eeepc_reachability_age': eeepc_reachability_age,
                'selfevo_current_proof': control_plane.get('selfevo_current_proof'),
                'selfevo_remote_freshness': control_plane.get('selfevo_remote_freshness'),
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
