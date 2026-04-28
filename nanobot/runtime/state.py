"""Canonical runtime state helpers for operator-facing summaries."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any


_DEFAULT_HOST_CONTROL_PLANE_STATE_ROOT = Path("/var/lib/eeepc-agent/self-evolving-agent/state")


def _safe_read_json(path: Path | None) -> Any:
    if not path:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_json_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    matches = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return matches[0] if matches else None


def _workspace_looks_like_eeepc_live_runtime(workspace: Path) -> bool:
    return workspace.parent.name == ".nanobot-eeepc" and workspace.name == "workspace"


def _state_dir_looks_like_eeepc_canonical_root(candidate: Path) -> bool:
    return (
        candidate.name == "state"
        and candidate.parent.name == "self-evolving-agent"
        and candidate.parent.parent.name == "eeepc-agent"
    )


def _safe_runtime_config_operator_boost() -> dict[str, Any] | None:
    try:
        from nanobot.config.loader import load_config
        config = load_config()
        supermind = getattr(config, 'supermind', None)
        if not supermind:
            return None
        return {
            'enabled': bool(supermind.enabled),
            'model': supermind.model,
            'reasoning_effort': supermind.reasoning_effort,
            'max_tokens': supermind.max_tokens,
        }
    except Exception:
        return None


_PROVENANCE_PLACEHOLDER_VALUES = {'unknown', 'not_collected', 'local-build', 'placeholder', 'tbd', 'todo', 'n/a', 'na', 'none', 'null'}


def _governance_coverage_snapshot(runtime: dict[str, Any]) -> dict[str, Any]:
    candidate_path = runtime.get('promotion_candidate_path')
    decision_record = runtime.get('promotion_decision_record')
    accepted_record = runtime.get('promotion_accepted_record')
    replay = runtime.get('promotion_replay_readiness') if isinstance(runtime.get('promotion_replay_readiness'), dict) else None
    if not candidate_path:
        return {
            'state': 'absent',
            'projects_considered': 0,
            'ownership_gaps': 0,
            'due_reviews': 0,
            'next_action': 'no promotion governance candidate present',
        }
    ownership_gaps = 0 if decision_record == 'present' else 1
    due_reviews = 0 if decision_record == 'present' else 1
    if replay and replay.get('state') == 'ready':
        state = 'healthy'
        next_action = 'replayable governance trail present'
    else:
        state = 'action_required'
        next_action = replay.get('reason') if replay else 'complete decision and accepted records'
    return {
        'state': state,
        'projects_considered': 1,
        'ownership_gaps': ownership_gaps,
        'due_reviews': due_reviews,
        'next_action': next_action,
    }


def _promotion_provenance_snapshot(promotion_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(promotion_data, dict):
        return None
    nested = promotion_data.get('promotion_provenance') if isinstance(promotion_data.get('promotion_provenance'), dict) else {}
    deployment_fingerprint = nested.get('deployment_fingerprint') if isinstance(nested.get('deployment_fingerprint'), dict) else {}
    rollback_evidence = nested.get('rollback_evidence') if nested.get('rollback_evidence') is not None else promotion_data.get('rollback_evidence')
    source_commit = nested.get('source_commit') or promotion_data.get('source_commit')
    build_recipe_hash = nested.get('build_recipe_hash') or promotion_data.get('build_recipe_hash')
    artifact_id = nested.get('artifact_id') or promotion_data.get('artifact_id')
    artifact_version = nested.get('artifact_version') or promotion_data.get('artifact_version')
    release_channel = nested.get('release_channel') or promotion_data.get('release_channel')
    target_host_profile = nested.get('target_host_profile') or promotion_data.get('target_host_profile')
    target_authority = nested.get('target_authority') or promotion_data.get('target_authority')
    deployment_fingerprint_id = (
        deployment_fingerprint.get('deployment_fingerprint_id')
        or nested.get('deployment_fingerprint_id')
        or promotion_data.get('deployment_fingerprint_id')
    )

    def _missing(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            normalized = value.strip().lower()
            return not normalized or normalized in _PROVENANCE_PLACEHOLDER_VALUES
        if isinstance(value, (list, tuple, set, dict)):
            return not bool(value)
        return False

    missing_fields = [
        field_name
        for field_name, value in {
            'source_commit': source_commit,
            'build_recipe_hash': build_recipe_hash,
            'artifact_id': artifact_id,
            'artifact_version': artifact_version,
            'release_channel': release_channel,
            'target_host_profile': target_host_profile,
            'target_authority': target_authority,
            'deployment_fingerprint_id': deployment_fingerprint_id,
            'rollback_evidence': rollback_evidence,
        }.items()
        if _missing(value)
    ]
    status = 'ready' if not missing_fields else 'blocked'
    blocking_reason = None if not missing_fields else f"missing_or_placeholder_provenance:{','.join(missing_fields)}"
    return {
        'status': status,
        'blocking_reason': blocking_reason,
        'source_commit': source_commit,
        'build_recipe_hash': build_recipe_hash,
        'artifact_id': artifact_id,
        'artifact_version': artifact_version,
        'release_channel': release_channel,
        'target_host_profile': target_host_profile,
        'target_authority': target_authority,
        'deployment_fingerprint': {
            **deployment_fingerprint,
            'deployment_fingerprint_id': deployment_fingerprint_id,
            'artifact_id': artifact_id,
            'artifact_version': artifact_version,
            'release_channel': release_channel,
            'target_host_profile': target_host_profile,
            'target_authority': target_authority,
        },
        'deployment_fingerprint_id': deployment_fingerprint_id,
        'rollback_evidence': rollback_evidence,
    }


def _material_progress_snapshot(runtime: dict[str, Any]) -> dict[str, Any]:
    def _present(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict, tuple, set)):
            return bool(value)
        return True

    experiment = runtime.get('experiment') if isinstance(runtime.get('experiment'), dict) else {}
    selfevo_state = runtime.get('selfevo_current_state') if isinstance(runtime.get('selfevo_current_state'), dict) else {}
    subagent_rollup = runtime.get('subagent_rollup') if isinstance(runtime.get('subagent_rollup'), dict) else {}
    governance_schema = runtime.get('governance_schema') if isinstance(runtime.get('governance_schema'), dict) else {}
    promotion_governance_packet = runtime.get('promotion_governance_packet') if isinstance(runtime.get('promotion_governance_packet'), dict) else {}

    accepted_experiment = bool(
        (runtime.get('decision') or experiment.get('decision')) in {'accept', 'accepted', 'keep', 'pass'}
        or (runtime.get('experiment_outcome') or experiment.get('outcome')) in {'keep', 'accept', 'accepted'}
        or (runtime.get('review_status') or experiment.get('review_status')) == 'reviewed' and (runtime.get('decision') or experiment.get('decision')) == 'accept'
    )
    merged_selfevo_pr = bool(
        (selfevo_state.get('last_merge') if isinstance(selfevo_state, dict) else None)
        or (selfevo_state.get('last_issue_lifecycle') if isinstance(selfevo_state, dict) else None)
        or (runtime.get('promotion_replay_readiness') or {}).get('state') == 'ready'
    )
    latest_subagent_result = subagent_rollup.get('latest_result') if isinstance(subagent_rollup.get('latest_result'), dict) else {}
    latest_subagent_status = latest_subagent_result.get('status') if isinstance(latest_subagent_result, dict) else None
    subagent_terminal_count = int(subagent_rollup.get('count_completed', 0) or subagent_rollup.get('completed_result_count', 0) or 0)
    subagent_blocked_count = int(subagent_rollup.get('blocked_result_count', 0) or 0)
    subagent_only_blocked = bool(
        latest_subagent_status == 'blocked'
        and subagent_blocked_count >= subagent_terminal_count
        and subagent_terminal_count > 0
    )
    consumed_subagent_result = bool(
        (subagent_terminal_count or _present(latest_subagent_result))
        and latest_subagent_status not in {'blocked', 'failed', 'error'}
        and not subagent_only_blocked
    )
    promotion_evidence_artifact = bool(
        _present(runtime.get('promotion_artifact_path'))
        or _present(runtime.get('evidence_ref'))
        or _present(runtime.get('artifact_paths'))
        or _present((promotion_governance_packet or {}).get('source_artifact'))
        or _present((governance_schema or {}).get('accepted_record'))
    )

    proofs = [
        {
            'kind': 'accepted_experiment',
            'present': accepted_experiment,
            'reason': 'experiment_accepted' if accepted_experiment else 'experiment_not_accepted',
            'evidence': {
                'decision': runtime.get('decision') or experiment.get('decision'),
                'outcome': runtime.get('experiment_outcome') or experiment.get('outcome'),
                'review_status': runtime.get('review_status') or experiment.get('review_status'),
                'experiment_path': runtime.get('experiment_path'),
            },
        },
        {
            'kind': 'merged_selfevo_pr_closure',
            'present': merged_selfevo_pr,
            'reason': 'selfevo_pr_merged' if merged_selfevo_pr else 'selfevo_pr_not_merged',
            'evidence': {
                'last_merge': selfevo_state.get('last_merge') if isinstance(selfevo_state, dict) else None,
                'last_issue_lifecycle': selfevo_state.get('last_issue_lifecycle') if isinstance(selfevo_state, dict) else None,
                'promotion_replay_readiness': runtime.get('promotion_replay_readiness'),
            },
        },
        {
            'kind': 'consumed_subagent_result',
            'present': consumed_subagent_result,
            'reason': (
                'subagent_result_consumed'
                if consumed_subagent_result
                else ('subagent_result_blocked' if subagent_only_blocked else 'subagent_result_missing')
            ),
            'evidence': {
                'subagent_rollup_state': subagent_rollup.get('state'),
                'completed_result_count': subagent_rollup.get('completed_result_count') or subagent_rollup.get('count_completed'),
                'latest_result_path': (subagent_rollup.get('latest_result') or {}).get('path') if isinstance(subagent_rollup.get('latest_result'), dict) else None,
                'active_task_id': subagent_rollup.get('active_task_id'),
            },
        },
        {
            'kind': 'promotion_or_evidence_artifact',
            'present': promotion_evidence_artifact,
            'reason': 'promotion_evidence_artifact_present' if promotion_evidence_artifact else 'promotion_evidence_artifact_missing',
            'evidence': {
                'promotion_artifact_path': runtime.get('promotion_artifact_path'),
                'evidence_ref': runtime.get('evidence_ref'),
                'artifact_paths': runtime.get('artifact_paths'),
                'source_artifact': (promotion_governance_packet or {}).get('source_artifact'),
            },
        },
    ]
    qualifying_proofs = [proof['kind'] for proof in proofs if proof['present']]
    non_qualifying_proofs: list[str] = []
    current_discarded_no_material_change = bool(
        (runtime.get('experiment_outcome') or experiment.get('outcome')) == 'discard'
        and (runtime.get('revert_status') or experiment.get('revert_status')) in {None, 'skipped_no_material_change', 'terminal_no_material_change'}
    )
    current_cycle_material = bool(accepted_experiment or consumed_subagent_result)
    if current_discarded_no_material_change and not current_cycle_material:
        if merged_selfevo_pr:
            non_qualifying_proofs.append('historic_or_unlinked_selfevo_pr')
        if promotion_evidence_artifact:
            non_qualifying_proofs.append('historic_or_unaccepted_promotion_artifact')
        state = 'blocked'
        healthy_allowed = False
        blocking_reason = 'missing_current_material_progress'
        qualifying_proofs = []
    else:
        state = 'proven' if qualifying_proofs else 'missing'
        healthy_allowed = bool(qualifying_proofs)
        blocking_reason = None if qualifying_proofs else 'material_progress_proof_missing'
    return {
        'schema_version': 'material-progress-v1',
        'state': state,
        'healthy_autonomy_allowed': healthy_allowed,
        'proof_count': len(qualifying_proofs),
        'proofs': proofs,
        'qualifying_proofs': qualifying_proofs,
        'non_qualifying_proofs': non_qualifying_proofs,
        'blocking_reason': blocking_reason,
    }


def _read_meminfo_available_bytes() -> int | None:
    try:
        meminfo = Path('/proc/meminfo')
        if not meminfo.exists():
            return None
        for line in meminfo.read_text(encoding='utf-8').splitlines():
            if line.startswith('MemAvailable:'):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) * 1024
    except Exception:
        return None
    return None


def _host_resource_snapshot(state_root: Path) -> dict[str, Any]:
    try:
        load1, load5, load15 = os.getloadavg()
        loadavg = {'1m': round(load1, 3), '5m': round(load5, 3), '15m': round(load15, 3)}
    except Exception:
        loadavg = {'1m': None, '5m': None, '15m': None}
    try:
        usage = shutil.disk_usage(state_root)
        disk_free = int(usage.free)
        disk_total = int(usage.total)
    except Exception:
        disk_free = None
        disk_total = None
    mem_available = _read_meminfo_available_bytes()
    weak_host_signals: list[str] = []
    if isinstance(loadavg.get('1m'), (int, float)) and loadavg['1m'] is not None and loadavg['1m'] > 2.0:
        weak_host_signals.append('high_load')
    if isinstance(mem_available, int) and mem_available < 512 * 1024 * 1024:
        weak_host_signals.append('low_memory')
    if isinstance(disk_free, int) and disk_free < 2 * 1024 * 1024 * 1024:
        weak_host_signals.append('low_disk')
    return {
        'loadavg': loadavg,
        'memory_available_bytes': mem_available,
        'disk_free_bytes': disk_free,
        'disk_total_bytes': disk_total,
        'weak_host_signals': weak_host_signals,
    }


def resolve_runtime_state_location(workspace: Path) -> tuple[Path, str]:
    """Return the canonical runtime state root and its source kind for a workspace."""
    source_kind = os.getenv("NANOBOT_RUNTIME_STATE_SOURCE")
    override = os.getenv("NANOBOT_RUNTIME_STATE_ROOT")
    bridge_state_dir = os.getenv("STATE_DIR")

    if source_kind is None:
        if override:
            source_kind = "host_control_plane"
        elif bridge_state_dir:
            candidate = Path(bridge_state_dir).expanduser()
            if _state_dir_looks_like_eeepc_canonical_root(candidate):
                source_kind = "host_control_plane"
            else:
                source_kind = "host_control_plane" if _workspace_looks_like_eeepc_live_runtime(workspace) else "workspace_state"
        else:
            source_kind = "host_control_plane" if _workspace_looks_like_eeepc_live_runtime(workspace) else "workspace_state"

    if source_kind == "host_control_plane":
        if override:
            return (Path(override).expanduser(), source_kind)
        if bridge_state_dir:
            candidate = Path(bridge_state_dir).expanduser()
            if _state_dir_looks_like_eeepc_canonical_root(candidate):
                return (candidate, source_kind)
        return (_DEFAULT_HOST_CONTROL_PLANE_STATE_ROOT, source_kind)
    return (workspace / "state", source_kind)


def resolve_runtime_state_root(workspace: Path) -> Path:
    return resolve_runtime_state_location(workspace)[0]


def load_runtime_state_for_workspace(workspace: Path) -> dict[str, Any]:
    """Load canonical runtime state using the resolved state root for a workspace."""
    state_root, source_kind = resolve_runtime_state_location(workspace)
    return load_runtime_state_from_root(state_root, source_kind=source_kind)


def _cycle_budget_snapshot(runtime: dict[str, Any]) -> dict[str, Any]:
    budget = runtime.get('experiment_budget') if isinstance(runtime.get('experiment_budget'), dict) else {}
    used = runtime.get('experiment_budget_used') if isinstance(runtime.get('experiment_budget_used'), dict) else {}
    max_requests = budget.get('max_requests')
    max_tool_calls = budget.get('max_tool_calls')
    max_timeout_seconds = budget.get('max_timeout_seconds')
    requests_used = used.get('requests')
    tool_calls_used = used.get('tool_calls')
    elapsed_seconds = used.get('elapsed_seconds')
    blocked_reasons: list[str] = []
    degraded_reasons: list[str] = []
    if isinstance(max_requests, int) and isinstance(requests_used, int):
        if requests_used > max_requests:
            blocked_reasons.append('requests_exceeded')
        elif requests_used == max_requests:
            degraded_reasons.append('requests_at_limit')
    if isinstance(max_tool_calls, int) and isinstance(tool_calls_used, int):
        if tool_calls_used > max_tool_calls:
            blocked_reasons.append('tool_calls_exceeded')
        elif tool_calls_used == max_tool_calls:
            degraded_reasons.append('tool_calls_at_limit')
    if isinstance(max_timeout_seconds, (int, float)) and isinstance(elapsed_seconds, (int, float)):
        if elapsed_seconds > max_timeout_seconds:
            blocked_reasons.append('timeout_exceeded')
        elif elapsed_seconds == max_timeout_seconds:
            degraded_reasons.append('timeout_at_limit')
    if blocked_reasons:
        state = 'blocked'
        reason = ','.join(blocked_reasons)
    elif degraded_reasons:
        state = 'degraded'
        reason = ','.join(degraded_reasons)
    else:
        state = 'available'
        reason = 'within_limits'
    return {
        'state': state,
        'reason': reason,
        'limit': budget,
        'used': used,
    }


def _capability_snapshot(runtime: dict[str, Any]) -> dict[str, Any]:
    approval_state = runtime.get('approval_gate_state')
    next_hint = runtime.get('next_hint')
    if approval_state in {'fresh', 'active', 'valid', 'ok'}:
        bounded_apply = {'state': 'available', 'reason': 'approval_gate_valid'}
    elif approval_state in {'missing'} or (isinstance(next_hint, str) and 'approval gate missing' in next_hint):
        bounded_apply = {'state': 'blocked', 'reason': 'approval_gate_missing'}
    elif approval_state in {'expired', 'stale'}:
        bounded_apply = {'state': 'blocked', 'reason': 'approval_gate_expired'}
    else:
        bounded_apply = {'state': 'blocked', 'reason': approval_state or 'approval_gate_unavailable'}
    host_resources = runtime.get('host_resources') if isinstance(runtime.get('host_resources'), dict) else None
    weak_host = bool(host_resources and host_resources.get('weak_host_signals'))
    cycle_budget = _cycle_budget_snapshot(runtime)
    memory_discipline = runtime.get('memory_discipline') if isinstance(runtime.get('memory_discipline'), dict) else None
    return {
        'runtime_state': {'state': 'available', 'reason': 'loaded'},
        'bounded_apply': bounded_apply,
        'host_budget_headroom': {'state': 'degraded' if weak_host else 'available', 'reason': 'weak_host_signals' if weak_host else 'normal'},
        'cycle_budget': cycle_budget,
        'memory_discipline': memory_discipline or {'state': 'active', 'reason': 'system_prompt_cap_and_media_guard'},
    }


def _subagent_correlation_snapshot(runtime: dict[str, Any]) -> dict[str, Any] | None:
    telemetry_path = runtime.get('subagent_telemetry_path')
    if not telemetry_path:
        return None
    return {
        'telemetry_path': telemetry_path,
        'goal_id': runtime.get('subagent_goal_id') or runtime.get('subagent_telemetry_latest_goal_id'),
        'cycle_id': runtime.get('subagent_cycle_id') or runtime.get('subagent_telemetry_latest_cycle_id'),
        'current_task_id': runtime.get('subagent_task_id') or runtime.get('subagent_telemetry_latest_current_task_id'),
        'report_path': runtime.get('subagent_report_path') or runtime.get('subagent_telemetry_latest_report_path'),
        'status': runtime.get('subagent_status') or runtime.get('subagent_telemetry_latest_status'),
        'reward_signal': runtime.get('subagent_reward_signal') or runtime.get('subagent_telemetry_latest_reward_signal'),
        'feedback_decision': runtime.get('subagent_feedback_decision') or runtime.get('subagent_telemetry_latest_feedback_decision'),
    }


def _subagent_rollup_snapshot(
    *,
    state_root: Path,
    current_task_id: str | None = None,
    current_task_title: str | None = None,
    stale_after_seconds: int = 3600,
) -> dict[str, Any] | None:
    subagents_dir = state_root / 'subagents'
    request_dir = subagents_dir / 'requests'
    result_dir = subagents_dir / 'results'

    completed_statuses = {'ok', 'error', 'cancelled', 'canceled', 'completed', 'complete', 'done', 'pass'}
    queued_statuses = {'queued', 'pending'}

    telemetry_records: list[dict[str, Any]] = []
    terminal_telemetry_results: dict[str, dict[str, Any]] = {}
    if subagents_dir.exists():
        telemetry_paths = sorted(
            [path for path in subagents_dir.glob('*.json') if path.is_file()],
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        for path in telemetry_paths:
            payload = _safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            task_id = payload.get('subagent_id') or payload.get('task_id') or payload.get('id')
            status = str(payload.get('status') or 'unknown')
            telemetry_record = {
                'path': str(path),
                'task_id': task_id,
                'status': status,
                'summary': payload.get('summary') or payload.get('result'),
                'started_at': payload.get('started_at'),
                'finished_at': payload.get('finished_at'),
                'origin': payload.get('origin'),
                'runtime_state_source': payload.get('runtime_state_source'),
            }
            telemetry_records.append(telemetry_record)
            if task_id and status.lower() in completed_statuses:
                result_key = str(task_id)
                terminal_telemetry_results.setdefault(result_key, {
                    'path': str(path),
                    'task_id': task_id,
                    'task_title': payload.get('title') or payload.get('summary') or task_id,
                    'cycle_id': payload.get('cycle_id') or payload.get('cycleId'),
                    'status': status,
                    'summary': payload.get('summary') or payload.get('result'),
                    'age_seconds': max(0, int(time.time() - path.stat().st_mtime)),
                    'materialized_from': 'telemetry',
                })

    request_records: list[dict[str, Any]] = []
    if request_dir.exists():
        request_paths = sorted(
            [path for path in request_dir.glob('*.json') if path.is_file()],
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        for path in request_paths:
            payload = _safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            task_id = payload.get('task_id') or payload.get('taskId')
            original_status = str(payload.get('request_status') or payload.get('status') or 'queued')
            materialized_result = terminal_telemetry_results.get(str(task_id)) if task_id else None
            effective_status = 'completed' if materialized_result else original_status
            age_seconds = max(0, int(time.time() - path.stat().st_mtime))
            request_records.append({
                'path': str(path),
                'task_id': task_id,
                'task_title': payload.get('task_title') or payload.get('title') or payload.get('summary'),
                'cycle_id': payload.get('cycle_id') or payload.get('cycleId'),
                'status': effective_status,
                'request_status': original_status,
                'age_seconds': age_seconds,
                'source_artifact': payload.get('source_artifact'),
                'feedback_decision': payload.get('feedback_decision'),
                'materialized_result_path': materialized_result.get('path') if isinstance(materialized_result, dict) else None,
                'materialized_result_status': materialized_result.get('status') if isinstance(materialized_result, dict) else None,
            })

    result_records: list[dict[str, Any]] = []
    results_by_request_path: dict[str, dict[str, Any]] = {}
    results_by_cycle_id: dict[str, dict[str, Any]] = {}
    results_by_task_id: dict[str, dict[str, Any]] = {}
    if result_dir.exists():
        result_paths = sorted(
            [path for path in result_dir.glob('*.json') if path.is_file()],
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        for path in result_paths:
            payload = _safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            status = str(payload.get('status') or payload.get('result_status') or 'completed')
            result = {
                'path': str(path),
                'request_path': payload.get('request_path'),
                'task_id': payload.get('task_id') or payload.get('taskId') or payload.get('subagent_id'),
                'task_title': payload.get('task_title') or payload.get('title') or payload.get('summary'),
                'cycle_id': payload.get('cycle_id') or payload.get('cycleId'),
                'status': status,
                'summary': payload.get('summary') or payload.get('result'),
                'age_seconds': max(0, int(time.time() - path.stat().st_mtime)),
            }
            result_records.append(result)
            if result.get('request_path'):
                results_by_request_path.setdefault(str(result['request_path']), result)
            if result.get('cycle_id'):
                results_by_cycle_id.setdefault(str(result['cycle_id']), result)
            if result.get('task_id'):
                results_by_task_id.setdefault(str(result['task_id']), result)
    for result_key, result in terminal_telemetry_results.items():
        if not any(record.get('path') == result.get('path') for record in result_records):
            result_records.append(result)
        results_by_task_id.setdefault(str(result_key), result)
    for request in request_records:
        task_id = request.get('task_id')
        cycle_id = request.get('cycle_id')
        materialized_result = (
            results_by_request_path.get(str(request.get('path')))
            or (results_by_cycle_id.get(str(cycle_id)) if cycle_id else None)
            or (results_by_task_id.get(str(task_id)) if task_id else None)
        )
        if isinstance(materialized_result, dict):
            request['materialized_result_path'] = materialized_result.get('path')
            request['materialized_result_status'] = materialized_result.get('status')
            request['status'] = str(materialized_result.get('status') or 'completed').lower()
    result_records = sorted(result_records, key=lambda record: record.get('age_seconds') or 0)

    if not telemetry_records and not request_records and not result_records:
        return None

    completed_task_ids = {str(record['task_id']) for record in result_records if record.get('task_id')}
    blocked_result_count = sum(1 for record in result_records if str(record.get('status') or '').lower() in {'blocked', 'terminal_blocked'})

    queued_count = sum(1 for record in request_records if record['status'] in queued_statuses)
    queued_count += sum(
        1
        for record in telemetry_records
        if record['status'] in {'running', 'queued', 'pending', 'in_progress', 'dispatching'}
        and str(record.get('task_id')) not in completed_task_ids
    )
    completed_count = len(result_records)
    nonblocked_result_count = max(0, completed_count - blocked_result_count)
    stale_count = sum(
        1
        for record in request_records
        if record['request_status'] in queued_statuses
        and not record.get('materialized_result_path')
        and record['age_seconds'] >= stale_after_seconds
    )

    blocked_results_dominant = bool(blocked_result_count and blocked_result_count > nonblocked_result_count)
    if blocked_results_dominant:
        rollup_state = 'blocked' if nonblocked_result_count == 0 else 'degraded'
        rollup_reason = 'blocked_results_dominant'
    elif completed_count and (queued_count or stale_count):
        rollup_state = 'mixed'
        rollup_reason = 'mixed_requests_and_results'
    elif stale_count:
        rollup_state = 'stale'
        rollup_reason = 'stale_requests_present'
    elif queued_count:
        rollup_state = 'queued'
        rollup_reason = 'queued_requests_present'
    elif completed_count:
        rollup_state = 'completed'
        rollup_reason = 'completed_results_only'
    else:
        rollup_state = 'missing'
        rollup_reason = 'no_subagent_activity'

    def _match_record(records: list[dict[str, Any]], task_id: str | None) -> dict[str, Any] | None:
        if not task_id:
            return None
        for record in records:
            if record.get('task_id') == task_id:
                return record
        return None

    preferred_task_id = current_task_id
    request_match = _match_record(request_records, preferred_task_id) if preferred_task_id else None
    telemetry_match = _match_record(telemetry_records, preferred_task_id) if preferred_task_id else None
    result_match = _match_record(result_records, preferred_task_id) if preferred_task_id else None

    linkage_source = 'task_plan' if preferred_task_id else None
    if preferred_task_id is None:
        for source_name, record in (
            ('request', request_records[0] if request_records else None),
            ('telemetry', telemetry_records[0] if telemetry_records else None),
            ('result', result_records[0] if result_records else None),
        ):
            if record is not None:
                preferred_task_id = record.get('task_id') or preferred_task_id
                linkage_source = source_name
                if source_name == 'request':
                    request_match = record
                elif source_name == 'telemetry':
                    telemetry_match = record
                else:
                    result_match = record
                break

    active_task_linkage = {
        'task_id': preferred_task_id,
        'title': current_task_title
        or (request_match or {}).get('task_title')
        or (telemetry_match or {}).get('summary')
        or (result_match or {}).get('task_title')
        or preferred_task_id,
        'request_path': (request_match or {}).get('path'),
        'result_path': (result_match or {}).get('path'),
        'telemetry_path': (telemetry_match or {}).get('path'),
        'request_status': (request_match or {}).get('status'),
        'result_status': (result_match or {}).get('status'),
        'telemetry_status': (telemetry_match or {}).get('status'),
        'source': linkage_source,
    }

    return {
        'schema_version': 'subagent-rollup-v1',
        'enabled': True,
        'state': rollup_state,
        'reason': rollup_reason,
        'count_total': queued_count + completed_count + stale_count,
        'count_done': completed_count,
        'count_queued': queued_count,
        'count_completed': completed_count,
        'count_stale': stale_count,
        'queued_request_count': queued_count,
        'completed_result_count': completed_count,
        'blocked_result_count': blocked_result_count,
        'stale_request_count': stale_count,
        'telemetry_count': len(telemetry_records),
        'request_count': len(request_records),
        'result_count': len(result_records),
        'active_task_id': preferred_task_id,
        'active_task_title': active_task_linkage.get('title'),
        'active_task_linkage': active_task_linkage,
        'latest_request': request_records[0] if request_records else None,
        'latest_result': result_records[0] if result_records else None,
        'latest_telemetry': telemetry_records[0] if telemetry_records else None,
    }


def load_runtime_state_from_root(state_root: Path, source_kind: str = "workspace_state") -> dict[str, Any]:
    """Load canonical runtime state from an explicit state root if present."""
    reports_dir = state_root / "reports"
    outbox_dir = state_root / "outbox"
    goals_dir = state_root / "goals"
    goal_history_dir = goals_dir / "history"
    promotions_dir = state_root / "promotions"
    experiments_dir = state_root / "experiments"
    hypotheses_dir = state_root / "hypotheses"
    subagents_dir = state_root / "subagents"
    credits_dir = state_root / "credits"

    latest_report = _latest_json_file(reports_dir, "evolution-*.json") or _latest_json_file(reports_dir, "*.json")
    current_goal_path = goals_dir / "current.json"
    active_goal_path = goals_dir / "active.json"
    latest_goal = current_goal_path if current_goal_path.exists() else active_goal_path if active_goal_path.exists() else _latest_json_file(goals_dir, "*.json")
    latest_goal_history = _latest_json_file(goal_history_dir, "cycle-*.json")
    if source_kind == "host_control_plane":
        latest_outbox = (
            _latest_json_file(outbox_dir, "report.index.json")
            or _latest_json_file(outbox_dir, "latest.json")
            or _latest_json_file(outbox_dir, "*.json")
        )
    else:
        latest_outbox = _latest_json_file(outbox_dir, "latest.json") or _latest_json_file(outbox_dir, "*.json")
    latest_promotion = _latest_json_file(promotions_dir, "latest.json") or _latest_json_file(promotions_dir, "*.json")
    latest_experiment = _latest_json_file(experiments_dir, "latest.json") or _latest_json_file(experiments_dir, "*.json")
    latest_hypothesis_backlog = _latest_json_file(hypotheses_dir, "backlog.json") or _latest_json_file(hypotheses_dir, "*.json")
    latest_subagent = _latest_json_file(subagents_dir, "*.json")
    latest_credits = _latest_json_file(credits_dir, "latest.json") or _latest_json_file(credits_dir, "*.json")

    report_data = _safe_read_json(latest_report)
    current_goal_data = _safe_read_json(current_goal_path)
    active_goal_data = _safe_read_json(active_goal_path)
    goal_history_data = _safe_read_json(latest_goal_history)
    goal_data = current_goal_data or active_goal_data or goal_history_data or _safe_read_json(latest_goal)
    outbox_data = _safe_read_json(latest_outbox)
    promotion_data = _safe_read_json(latest_promotion)
    experiment_data = _safe_read_json(latest_experiment)
    hypothesis_backlog_data = _safe_read_json(latest_hypothesis_backlog)
    subagent_data = _safe_read_json(latest_subagent)
    credits_data = _safe_read_json(latest_credits)

    hypothesis_backlog_schema_version = None
    hypothesis_backlog_entry_count = None
    hypothesis_backlog_selected_id = None
    hypothesis_backlog_selected_title = None
    hypothesis_backlog_best_score = None
    hypothesis_backlog_model = None
    hypothesis_backlog_selected_wsjf = None
    if isinstance(hypothesis_backlog_data, dict):
        hypothesis_backlog_schema_version = (
            hypothesis_backlog_data.get("schema_version") or hypothesis_backlog_data.get("schemaVersion")
        )
        hypothesis_backlog_model = hypothesis_backlog_data.get("model")
        backlog_entries = hypothesis_backlog_data.get("entries") if isinstance(hypothesis_backlog_data.get("entries"), list) else []
        hypothesis_backlog_entry_count = len(backlog_entries)
        hypothesis_backlog_selected_id = (
            hypothesis_backlog_data.get("selected_hypothesis_id")
            or hypothesis_backlog_data.get("selectedHypothesisId")
        )
        hypothesis_backlog_selected_title = (
            hypothesis_backlog_data.get("selected_hypothesis_title")
            or hypothesis_backlog_data.get("selectedHypothesisTitle")
        )
        hypothesis_backlog_selected_wsjf = hypothesis_backlog_data.get("selected_hypothesis_wsjf")
        scores = [
            entry.get("bounded_priority_score")
            for entry in backlog_entries
            if isinstance(entry, dict) and isinstance(entry.get("bounded_priority_score"), (int, float))
        ]
        if scores:
            hypothesis_backlog_best_score = max(scores)

    approval_gate = None
    explicit_next_hint = None
    if isinstance(outbox_data, dict):
        approval_gate = outbox_data.get("approval_gate") or outbox_data.get("approvalGate")
        if approval_gate is None:
            capability_gate = outbox_data.get("capability_gate") if isinstance(outbox_data.get("capability_gate"), dict) else None
            if isinstance(capability_gate, dict):
                approval_gate = capability_gate.get("approval") if isinstance(capability_gate.get("approval"), dict) else None
        explicit_next_hint = outbox_data.get("next_hint") or outbox_data.get("nextHint")
        if explicit_next_hint is None:
            explicit_next_hint = (
                ((outbox_data.get("goal") or {}).get("follow_through") or {}).get("blocked_next_step")
                if isinstance(outbox_data.get("goal"), dict)
                else None
            )

    approval_gate_state = None
    approval_gate_ttl_minutes = None
    next_hint = explicit_next_hint
    if isinstance(approval_gate, dict):
        approval_gate_state = (
            approval_gate.get("state")
            or approval_gate.get("status")
            or approval_gate.get("reason")
            or ("ok" if approval_gate.get("ok") else None)
        )
        approval_gate_ttl_minutes = approval_gate.get("ttl_minutes") or approval_gate.get("ttlMinutes")
        if next_hint is None:
            if approval_gate_state in {"fresh", "active", "valid", "ok"}:
                next_hint = "none"
            else:
                next_hint = "refresh approval gate manually"
    elif approval_gate:
        approval_gate_state = str(approval_gate)
        if next_hint is None:
            next_hint = "refresh approval gate manually"
    elif next_hint is None:
        next_hint = "approval gate missing; refresh manually"

    active_goal = None
    if isinstance(goal_data, dict):
        active_goal = (
            goal_data.get("active_goal")
            or goal_data.get("activeGoal")
            or goal_data.get("active_goal_id")
            or goal_data.get("activeGoalId")
            or goal_data.get("goal_id")
            or goal_data.get("goalId")
        )
    if not active_goal and isinstance(report_data, dict):
        active_goal = (
            report_data.get("goal_id")
            or report_data.get("goalId")
            or ((report_data.get("goal") or {}).get("goal_id") if isinstance(report_data.get("goal"), dict) else None)
            or ((report_data.get("goal") or {}).get("goalId") if isinstance(report_data.get("goal"), dict) else None)
        )

    goal_rotation_reason = None
    goal_rotation_streak = None
    goal_rotation_trigger_goal = None
    goal_rotation_trigger_artifact_paths = None
    if isinstance(goal_data, dict):
        goal_rotation_reason = goal_data.get("rotation_reason") or goal_data.get("rotationReason")
        goal_rotation_streak = goal_data.get("rotation_streak") or goal_data.get("rotationStreak")
        goal_rotation_trigger_goal = goal_data.get("rotation_trigger_goal") or goal_data.get("rotationTriggerGoal")
        goal_rotation_trigger_artifact_paths = goal_data.get("rotation_trigger_artifact_paths") or goal_data.get("rotationTriggerArtifactPaths")
    if goal_rotation_reason is None and isinstance(active_goal_data, dict):
        goal_rotation_reason = active_goal_data.get("rotation_reason") or active_goal_data.get("rotationReason")
        goal_rotation_streak = goal_rotation_streak or active_goal_data.get("rotation_streak") or active_goal_data.get("rotationStreak")
        goal_rotation_trigger_goal = goal_rotation_trigger_goal or active_goal_data.get("rotation_trigger_goal") or active_goal_data.get("rotationTriggerGoal")
        goal_rotation_trigger_artifact_paths = goal_rotation_trigger_artifact_paths or active_goal_data.get("rotation_trigger_artifact_paths") or active_goal_data.get("rotationTriggerArtifactPaths")

    current_task_id = None
    task_counts = None
    task_reward_signal = None
    task_plan = None
    task_history = None
    task_plan_schema_version = None
    task_feedback_decision = None
    task_plan_path = str(current_goal_path) if current_goal_path.exists() else (str(active_goal_path) if active_goal_path.exists() else str(latest_goal) if latest_goal else None)
    task_history_path = str(latest_goal_history) if latest_goal_history else None
    if isinstance(current_goal_data, dict):
        task_plan = current_goal_data
    elif isinstance(goal_history_data, dict):
        task_plan = goal_history_data
    if isinstance(goal_history_data, dict):
        task_history = goal_history_data
    elif isinstance(current_goal_data, dict):
        task_history = current_goal_data
    if isinstance(task_plan, dict):
        current_task_id = task_plan.get("current_task_id") or task_plan.get("currentTaskId")
        task_counts = task_plan.get("task_counts") or task_plan.get("taskCounts")
        task_reward_signal = task_plan.get("reward_signal") or task_plan.get("rewardSignal")
        task_plan_schema_version = task_plan.get("schema_version") or task_plan.get("schemaVersion")
        task_feedback_decision = task_plan.get("feedback_decision") or task_plan.get("feedbackDecision")
        task_history_path = task_plan.get("history_path") or task_history_path
    if current_task_id is None and isinstance(task_history, dict):
        current_task_id = task_history.get("current_task_id") or task_history.get("currentTaskId")
    if task_counts is None and isinstance(task_history, dict):
        task_counts = task_history.get("task_counts") or task_history.get("taskCounts")
    if task_reward_signal is None and isinstance(task_history, dict):
        task_reward_signal = task_history.get("reward_signal") or task_history.get("rewardSignal")
    if task_plan_schema_version is None and isinstance(task_history, dict):
        task_plan_schema_version = task_history.get("schema_version") or task_history.get("schemaVersion")
    if task_feedback_decision is None and isinstance(task_history, dict):
        task_feedback_decision = task_history.get("feedback_decision") or task_history.get("feedbackDecision")

    cycle_id = None
    cycle_started = None
    cycle_ended = None
    evidence_ref = None
    promotion_candidate_id = None
    review_status = None
    decision = None
    decision_reason = None
    runtime_status = None
    artifact_paths = None
    follow_through_status = None
    goal_text = None
    improvement_score = None
    subagent_rollup = None
    subagent_rollup_from_files = None
    selfevo_current_state = None
    experiment = None
    experiment_path = str(latest_experiment) if latest_experiment else None
    experiment_budget = None
    experiment_budget_used = None
    experiment_reward_signal = None
    experiment_outcome = None
    experiment_metric_name = None
    experiment_metric_baseline = None
    experiment_metric_current = None
    experiment_metric_frontier = None
    experiment_complexity_delta = None
    experiment_simplicity_judgment = None
    promotion_schema_version = None
    experiment_contract_path = None
    promotion_path = str(latest_promotion) if latest_promotion else None
    promotion_candidate_path = None
    promotion_decision_record = None
    promotion_accepted_record = None
    promotion_reviewed_at = None
    promotion_accepted_at = None
    promotion_patch_bundle_path = None
    promotion_replay_readiness = None
    promotion_artifact_path = None
    promotion_readiness_checks = None
    promotion_readiness_reasons = None
    promotion_governance_packet = None
    promotion_provenance = None
    credits_balance = None
    credits_delta = None
    credits_path = str(latest_credits) if latest_credits else None
    subagent_telemetry_count = len(list(subagents_dir.glob("*.json"))) if subagents_dir.exists() else 0
    subagent_telemetry_latest_path = str(latest_subagent) if latest_subagent else None
    subagent_telemetry_latest_status = None
    subagent_telemetry_latest_summary = None
    subagent_telemetry_latest_id = None
    subagent_telemetry_latest_current_task_id = None
    subagent_telemetry_latest_reward_signal = None
    subagent_telemetry_latest_feedback_decision = None
    if isinstance(subagent_data, dict):
        subagent_telemetry_latest_id = subagent_data.get("subagent_id") or subagent_data.get("task_id") or subagent_data.get("id")
        subagent_telemetry_latest_status = subagent_data.get("status")
        subagent_telemetry_latest_summary = subagent_data.get("summary") or subagent_data.get("result")
        subagent_telemetry_latest_current_task_id = subagent_data.get("current_task_id")
        subagent_telemetry_latest_reward_signal = subagent_data.get("task_reward_signal")
        subagent_telemetry_latest_feedback_decision = subagent_data.get("task_feedback_decision")
    selfevo_current_state_path = state_root / 'self_evolution' / 'current_state.json'
    if selfevo_current_state_path.exists():
        selfevo_current_state = _safe_read_json(selfevo_current_state_path)
    if isinstance(credits_data, dict):
        credits_balance = credits_data.get("balance")
        credits_delta = credits_data.get("delta")
    if isinstance(report_data, dict):
        cycle_id = report_data.get("cycle_id") or report_data.get("cycleId")
        cycle_started = report_data.get("cycle_started_utc") or report_data.get("cycleStartedUtc")
        cycle_ended = report_data.get("cycle_ended_utc") or report_data.get("cycleEndedUtc")
        evidence_ref = report_data.get("evidence_ref_id") or report_data.get("evidenceRefId")
        promotion_candidate_id = report_data.get("promotion_candidate_id") or report_data.get("promotionCandidateId")
        review_status = report_data.get("review_status") or report_data.get("reviewStatus")
        decision = report_data.get("decision")
        runtime_status = (
            report_data.get("result_status")
            or report_data.get("resultStatus")
            or ((report_data.get("result") or {}).get("status") if isinstance(report_data.get("result"), dict) else None)
            or ((report_data.get("process_reflection") or {}).get("status") if isinstance(report_data.get("process_reflection"), dict) else None)
            or (outbox_data.get("status") if isinstance(outbox_data, dict) else None)
        )
        goal_text = (
            report_data.get("goal_text")
            or report_data.get("goalText")
            or ((report_data.get("goal") or {}).get("text") if isinstance(report_data.get("goal"), dict) else None)
        )
        improvement_score = report_data.get("improvement_score") or report_data.get("improvementScore")
        follow_through = report_data.get("follow_through") if isinstance(report_data.get("follow_through"), dict) else None
        if isinstance(follow_through, dict):
            follow_through_status = follow_through.get("status") or follow_through.get("follow_through_status") or follow_through.get("followThroughStatus")
            artifact_paths = follow_through.get("artifact_paths") or follow_through.get("artifactPaths")
        if isinstance(outbox_data, dict) and isinstance(outbox_data.get("goal"), dict):
            outbox_follow_through = ((outbox_data.get("goal") or {}).get("follow_through") or {})
            if artifact_paths is None:
                artifact_paths = outbox_follow_through.get("artifact_paths")
            follow_through_status = follow_through_status or outbox_follow_through.get("status")
        if goal_text is None and isinstance(outbox_data, dict) and isinstance(outbox_data.get("goal"), dict):
            goal_text = (outbox_data.get("goal") or {}).get("text")
        if improvement_score is None and isinstance(outbox_data, dict):
            improvement_score = outbox_data.get("improvement_score") or outbox_data.get("improvementScore")
        if subagent_rollup is None and isinstance(outbox_data, dict):
            subagent_rollup = ((outbox_data.get("goal_context") or {}).get("subagent_rollup")) if isinstance(outbox_data.get("goal_context"), dict) else None
        if subagent_rollup is None:
            result_obj = report_data.get("result") if isinstance(report_data.get("result"), dict) else None
            task_obj = (result_obj or {}).get("task") if isinstance((result_obj or {}).get("task"), dict) else None
            goal_context = (task_obj or {}).get("goal_context") if isinstance((task_obj or {}).get("goal_context"), dict) else None
            subagent_rollup = (goal_context or {}).get("subagent_rollup") if isinstance(goal_context, dict) else None
        subagent_rollup_from_files = _subagent_rollup_snapshot(
            state_root=state_root,
            current_task_id=current_task_id,
            current_task_title=(task_plan.get("current_task") if isinstance(task_plan, dict) else None),
        )
        if subagent_rollup is None:
            subagent_rollup = subagent_rollup_from_files
        elif isinstance(subagent_rollup_from_files, dict) and (
            subagent_rollup_from_files.get("result_count")
            or subagent_rollup.get("state") in {"stale", "missing"}
        ):
            subagent_rollup = subagent_rollup_from_files
        capability_gate = report_data.get("capability_gate") if isinstance(report_data.get("capability_gate"), dict) else None
        if approval_gate is None and isinstance(capability_gate, dict):
            approval_gate = capability_gate.get("approval") if isinstance(capability_gate.get("approval"), dict) else None
            if isinstance(approval_gate, dict):
                approval_gate_state = approval_gate.get("reason") or ("ok" if approval_gate.get("ok") else "blocked")

    if isinstance(experiment_data, dict):
        experiment = experiment_data
    elif isinstance(report_data, dict):
        experiment = report_data.get("experiment") if isinstance(report_data.get("experiment"), dict) else experiment
    if isinstance(experiment, dict):
        experiment_path = experiment.get("experiment_path") or experiment.get("experimentPath") or experiment_path
        experiment_budget = experiment.get("budget") or experiment.get("budgetBudget")
        experiment_budget_used = experiment.get("budget_used") or experiment.get("budgetUsed")
        experiment_reward_signal = experiment.get("reward_signal") or experiment.get("rewardSignal")
        experiment_outcome = experiment.get("outcome")
        experiment_metric_name = experiment.get("metric_name")
        experiment_metric_baseline = experiment.get("metric_baseline")
        experiment_metric_current = experiment.get("metric_current")
        experiment_metric_frontier = experiment.get("metric_frontier")
        experiment_complexity_delta = experiment.get("complexity_delta")
        experiment_simplicity_judgment = experiment.get("simplicity_judgment")
        experiment_contract_path = experiment.get("contract_path") or experiment.get("contractPath")
        if experiment_reward_signal is None and isinstance(task_reward_signal, dict):
            experiment_reward_signal = task_reward_signal
        if task_feedback_decision is None:
            task_feedback_decision = experiment.get("feedback_decision") or experiment.get("feedbackDecision")

    if isinstance(promotion_data, dict):
        promotion_schema_version = promotion_data.get("schema_version") or promotion_data.get("schemaVersion") or promotion_schema_version
        promotion_candidate_id = (
            promotion_data.get("promotion_candidate_id")
            or promotion_data.get("promotionCandidateId")
            or promotion_candidate_id
        )
        review_status = promotion_data.get("review_status") or promotion_data.get("reviewStatus") or review_status
        decision = promotion_data.get("decision") or decision
        decision_reason = promotion_data.get("decision_reason") or promotion_data.get("decisionReason") or decision_reason
        promotion_candidate_path = promotion_data.get("candidate_path") or promotion_data.get("candidatePath") or promotion_candidate_path
        promotion_artifact_path = promotion_data.get("artifact_path") or promotion_data.get("artifactPath") or promotion_artifact_path
        promotion_readiness_checks = promotion_data.get("readiness_checks") or promotion_data.get("readinessChecks") or promotion_readiness_checks
        promotion_readiness_reasons = promotion_data.get("readiness_reasons") or promotion_data.get("readinessReasons") or promotion_readiness_reasons
        promotion_governance_packet = promotion_data.get("governance_packet") or promotion_data.get("governancePacket") or promotion_governance_packet
        promotion_decision_record = promotion_data.get("decision_record") or promotion_data.get("decisionRecord") or promotion_decision_record
        promotion_accepted_record = promotion_data.get("accepted_record") or promotion_data.get("acceptedRecord") or promotion_accepted_record
        promotion_provenance = _promotion_provenance_snapshot(promotion_data)
    elif isinstance(outbox_data, dict):
        promotion = outbox_data.get("promotion") if isinstance(outbox_data.get("promotion"), dict) else None
        if isinstance(promotion, dict):
            promotion_candidate_path = promotion.get("candidate_path") or promotion.get("candidatePath")
            promotion_candidate_id = promotion.get("promotion_candidate_id") or promotion.get("promotionCandidateId") or promotion_candidate_id
            review_status = promotion.get("review_status") or promotion.get("reviewStatus") or review_status
            decision = promotion.get("decision") or decision

    promotion_summary = None
    governance_schema = None
    if promotion_candidate_id or review_status or decision:
        promotion_summary = " | ".join(
            str(value)
            for value in [
                promotion_candidate_id or "unknown",
                review_status or "unknown",
                decision or "unknown",
            ]
        )

    promotions_dir = state_root / "promotions"
    if promotion_candidate_id:
        decision_record_path = promotions_dir / "decisions" / f"{promotion_candidate_id}.json"
        accepted_record_path = promotions_dir / "accepted" / f"{promotion_candidate_id}.json"
        promotion_decision_record = "present" if decision_record_path.exists() else "missing"
        promotion_accepted_record = "present" if accepted_record_path.exists() else "missing"
        if decision_record_path.exists():
            decision_record = _safe_read_json(decision_record_path)
            if isinstance(decision_record, dict):
                promotion_reviewed_at = decision_record.get("reviewed_at_utc") or decision_record.get("reviewedAtUtc")
                decision_reason = decision_record.get("decision_reason") or decision_record.get("decisionReason") or decision_reason
                promotion_schema_version = promotion_schema_version or decision_record.get("schema_version") or decision_record.get("schemaVersion")
        if accepted_record_path.exists():
            accepted_record = _safe_read_json(accepted_record_path)
            if isinstance(accepted_record, dict):
                promotion_accepted_at = accepted_record.get("accepted_at_utc") or accepted_record.get("acceptedAtUtc")
                promotion_patch_bundle_path = accepted_record.get("patch_bundle_path") or accepted_record.get("patchBundlePath")
                promotion_schema_version = promotion_schema_version or accepted_record.get("schema_version") or accepted_record.get("schemaVersion")
        governance_schema = {
            'promotion_schema_version': promotion_schema_version,
            'decision_record': promotion_decision_record,
            'accepted_record': promotion_accepted_record,
        }
        if (
            decision == 'accept'
            and review_status == 'reviewed'
            and promotion_accepted_record == 'present'
            and promotion_patch_bundle_path
            and Path(promotion_patch_bundle_path).exists()
        ):
            if promotion_provenance and promotion_provenance.get('status') == 'ready':
                promotion_replay_readiness = {'state': 'ready', 'reason': 'accepted_bundle_present_and_provenance_complete'}
            else:
                promotion_replay_readiness = {
                    'state': 'blocked',
                    'reason': (promotion_provenance or {}).get('blocking_reason') or 'provenance_missing_or_placeholder',
                }
        elif decision == 'accept' and review_status == 'reviewed':
            promotion_replay_readiness = {'state': 'blocked', 'reason': 'patch_bundle_missing'}
        elif decision:
            promotion_replay_readiness = {'state': 'blocked', 'reason': 'not_accepted'}

    subagent_telemetry_latest_goal_id = None
    subagent_telemetry_latest_cycle_id = None
    subagent_telemetry_latest_report_path = None
    if isinstance(subagent_data, dict):
        subagent_telemetry_latest_goal_id = subagent_data.get("goal_id") or subagent_data.get("goalId")
        subagent_telemetry_latest_cycle_id = subagent_data.get("cycle_id") or subagent_data.get("cycleId")
        subagent_telemetry_latest_report_path = subagent_data.get("report_path") or subagent_data.get("reportPath")
    runtime = {
        "runtime_state_source": source_kind,
        "runtime_state_root": str(state_root),
        "active_goal": active_goal,
        "cycle_id": cycle_id,
        "cycle_started_utc": cycle_started,
        "cycle_ended_utc": cycle_ended,
        "evidence_ref": evidence_ref,
        "promotion_candidate_id": promotion_candidate_id,
        "review_status": review_status,
        "decision": decision,
        "decision_reason": decision_reason,
        "promotion_summary": promotion_summary,
        "promotion_schema_version": promotion_schema_version,
        "governance_schema": governance_schema,
        "promotion_candidate_path": promotion_candidate_path,
        "promotion_decision_record": promotion_decision_record,
        "promotion_accepted_record": promotion_accepted_record,
        "promotion_reviewed_at": promotion_reviewed_at,
        "promotion_accepted_at": promotion_accepted_at,
        "promotion_patch_bundle_path": promotion_patch_bundle_path,
        "promotion_artifact_path": promotion_artifact_path,
        "promotion_readiness_checks": promotion_readiness_checks,
        "promotion_readiness_reasons": promotion_readiness_reasons,
        "promotion_governance_packet": promotion_governance_packet,
        "promotion_provenance": promotion_provenance,
        "promotion_replay_readiness": promotion_replay_readiness,
        "hypothesis_backlog_schema_version": hypothesis_backlog_schema_version,
        "runtime_status": runtime_status,
        "artifact_paths": artifact_paths,
        "follow_through_status": follow_through_status,
        "goal_text": goal_text,
        "improvement_score": improvement_score,
        "subagent_rollup": subagent_rollup,
        "selfevo_current_state": selfevo_current_state,
        "promotion_path": promotion_path,
        "approval_gate": approval_gate,
        "approval_gate_state": approval_gate_state,
        "approval_gate_ttl_minutes": approval_gate_ttl_minutes,
        "next_hint": next_hint,
        "goal_rotation_reason": goal_rotation_reason,
        "goal_rotation_streak": goal_rotation_streak,
        "goal_rotation_trigger_goal": goal_rotation_trigger_goal,
        "goal_rotation_trigger_artifact_paths": goal_rotation_trigger_artifact_paths,
        "task_plan": task_plan,
        "task_history": task_history,
        "task_plan_path": task_plan_path,
        "task_history_path": task_history_path,
        "hypothesis_backlog_path": str(latest_hypothesis_backlog) if latest_hypothesis_backlog else None,
        "task_plan_schema_version": task_plan_schema_version,
        "task_feedback_decision": task_feedback_decision,
        "hypothesis_backlog_schema_version": hypothesis_backlog_schema_version,
        "hypothesis_backlog_entry_count": hypothesis_backlog_entry_count,
        "hypothesis_backlog_selected_id": hypothesis_backlog_selected_id,
        "hypothesis_backlog_selected_title": hypothesis_backlog_selected_title,
        "hypothesis_backlog_best_score": hypothesis_backlog_best_score,
        "hypothesis_backlog_model": hypothesis_backlog_model,
        "hypothesis_backlog_selected_wsjf": hypothesis_backlog_selected_wsjf,
        "current_task_id": current_task_id,
        "task_counts": task_counts,
        "task_reward_signal": task_reward_signal,
        "task_reward_value": task_reward_signal.get("value") if isinstance(task_reward_signal, dict) else None,
        "experiment": experiment,
        "experiment_path": experiment_path,
        "experiment_budget": experiment_budget,
        "experiment_budget_used": experiment_budget_used,
        "experiment_reward_signal": experiment_reward_signal,
        "experiment_outcome": experiment_outcome,
        "experiment_metric_name": experiment_metric_name,
        "experiment_metric_baseline": experiment_metric_baseline,
        "experiment_metric_current": experiment_metric_current,
        "experiment_metric_frontier": experiment_metric_frontier,
        "experiment_complexity_delta": experiment_complexity_delta,
        "experiment_simplicity_judgment": experiment_simplicity_judgment,
        "experiment_contract_path": experiment_contract_path,
        "credits_balance": credits_balance,
        "credits_delta": credits_delta,
        "credits_path": credits_path,
        "subagent_telemetry_root": str(subagents_dir) if subagents_dir.exists() else None,
        "subagent_telemetry_count": subagent_telemetry_count,
        "subagent_telemetry_path": subagent_telemetry_latest_path,
        "subagent_telemetry_latest_id": subagent_telemetry_latest_id,
        "subagent_telemetry_latest_status": subagent_telemetry_latest_status,
        "subagent_telemetry_latest_goal_id": subagent_telemetry_latest_goal_id,
        "subagent_telemetry_latest_cycle_id": subagent_telemetry_latest_cycle_id,
        "subagent_telemetry_latest_report_path": subagent_telemetry_latest_report_path,
        "subagent_telemetry_latest_summary": subagent_telemetry_latest_summary,
        "subagent_telemetry_latest_current_task_id": subagent_telemetry_latest_current_task_id,
        "subagent_telemetry_latest_reward_signal": subagent_telemetry_latest_reward_signal,
        "subagent_telemetry_latest_feedback_decision": subagent_telemetry_latest_feedback_decision,
        "host_resources": _host_resource_snapshot(state_root),
        "report_path": str(latest_report) if latest_report else None,
        "goal_path": str(active_goal_path) if active_goal_path.exists() else (str(current_goal_path) if current_goal_path.exists() else str(latest_goal) if latest_goal else None),
        "outbox_path": str(latest_outbox) if latest_outbox else None,
    }
    runtime["capabilities"] = _capability_snapshot(runtime)
    runtime["subagent_correlation"] = _subagent_correlation_snapshot(runtime)
    runtime["operator_boost"] = _safe_runtime_config_operator_boost()
    runtime["governance_coverage"] = _governance_coverage_snapshot(runtime)
    runtime["material_progress"] = _material_progress_snapshot(runtime)
    runtime["task_boundary"] = {
        'task_id': runtime.get('current_task_id'),
        'title': runtime.get('selected_task_title') or runtime.get('current_task'),
        'selection_source': runtime.get('task_selection_source'),
        'selected_tasks': runtime.get('selected_tasks'),
        'mutation_lane': (runtime.get('task_plan') or {}).get('mutation_lane') if isinstance(runtime.get('task_plan'), dict) else None,
        'budget': runtime.get('selected_hypothesis_execution_spec_budget'),
        'acceptance': runtime.get('selected_hypothesis_execution_spec_acceptance'),
    }
    try:
        from nanobot.runtime.action_registry import build_action_registry_snapshot
        runtime["action_registry"] = build_action_registry_snapshot(workspace)
    except Exception:
        runtime["action_registry"] = None
    return runtime



def load_runtime_state(workspace: Path) -> dict[str, Any]:
    """Load canonical runtime state from the workspace if present."""
    return load_runtime_state_from_root(workspace / "state", source_kind="workspace_state")


def format_runtime_state(runtime: dict[str, Any]) -> list[str]:
    """Format the canonical runtime state into stable user-facing lines."""
    lines = ["Runtime:"]

    def _render(label: str, value: Any) -> None:
        if value in (None, ""):
            lines.append(f"  {label}: unknown")
        elif isinstance(value, dict):
            compact = ", ".join(f"{k}={v}" for k, v in value.items())
            lines.append(f"  {label}: {compact or 'unknown'}")
        else:
            lines.append(f"  {label}: {value}")

    _render("Runtime state source", runtime.get("runtime_state_source"))
    _render("Runtime state root", runtime.get("runtime_state_root"))
    _render("Runtime status", runtime.get("runtime_status"))
    _render("Active goal", runtime.get("active_goal"))
    _render("Goal text", runtime.get("goal_text"))
    _render("Current task", runtime.get("current_task_id"))
    _render("Task counts", runtime.get("task_counts"))
    _render("Task reward", runtime.get("task_reward_signal") or runtime.get("task_reward_value"))
    _render("Experiment outcome", runtime.get("experiment_outcome"))
    _render("Experiment metric", runtime.get("experiment_metric_name"))
    _render("Experiment baseline", runtime.get("experiment_metric_baseline"))
    _render("Experiment current", runtime.get("experiment_metric_current"))
    _render("Experiment frontier", runtime.get("experiment_metric_frontier"))
    _render("Experiment contract", runtime.get("experiment_contract_path"))
    _render("Credits balance", runtime.get("credits_balance"))
    _render("Credits delta", runtime.get("credits_delta"))
    _render("Credits source", runtime.get("credits_path"))
    _render("Subagent telemetry root", runtime.get("subagent_telemetry_root"))
    _render("Subagent telemetry path", runtime.get("subagent_telemetry_path"))
    _render("Subagent telemetry count", runtime.get("subagent_telemetry_count"))
    if runtime.get("subagent_telemetry_latest_id") or runtime.get("subagent_telemetry_latest_status") or runtime.get("subagent_telemetry_latest_summary"):
        latest_bits = []
        if runtime.get("subagent_telemetry_latest_id"):
            latest_bits.append(f"id={runtime.get('subagent_telemetry_latest_id')}")
        if runtime.get("subagent_telemetry_latest_status"):
            latest_bits.append(f"status={runtime.get('subagent_telemetry_latest_status')}")
        if runtime.get("subagent_telemetry_latest_summary"):
            latest_bits.append(f"summary={runtime.get('subagent_telemetry_latest_summary')}")
        if runtime.get("subagent_telemetry_latest_current_task_id"):
            latest_bits.append(f"current_task_id={runtime.get('subagent_telemetry_latest_current_task_id')}")
        if runtime.get("subagent_telemetry_latest_reward_signal"):
            latest_bits.append(f"reward={runtime.get('subagent_telemetry_latest_reward_signal')}")
        if runtime.get("subagent_telemetry_latest_feedback_decision"):
            latest_bits.append(f"feedback={runtime.get('subagent_telemetry_latest_feedback_decision')}")
        _render("Subagent telemetry latest", " | ".join(latest_bits))
    if isinstance(runtime.get("experiment"), dict):
        experiment = runtime.get("experiment") or {}
        lines.append(
            "  Experiment: "
            f"id={experiment.get('experiment_id')}, budget={experiment.get('budget')}, used={experiment.get('budget_used')}"
        )
    _render("Plan source", runtime.get("task_plan_path"))
    _render("History source", runtime.get("task_history_path"))
    _render("Hypothesis backlog source", runtime.get("hypothesis_backlog_path"))
    _render("Task plan schema", runtime.get("task_plan_schema_version"))
    _render("Feedback", runtime.get("task_feedback_decision"))
    _render("Hypothesis backlog schema", runtime.get("hypothesis_backlog_schema_version"))
    _render("Hypothesis backlog model", runtime.get("hypothesis_backlog_model"))
    _render("Hypothesis backlog selected", runtime.get("hypothesis_backlog_selected_id"))
    _render("Hypothesis backlog title", runtime.get("hypothesis_backlog_selected_title"))
    _render("Hypothesis backlog entries", runtime.get("hypothesis_backlog_entry_count"))
    _render("Hypothesis backlog best score", runtime.get("hypothesis_backlog_best_score"))
    _render("Hypothesis backlog WSJF", runtime.get("hypothesis_backlog_selected_wsjf"))
    _render("Cycle", runtime.get("cycle_id"))
    _render("Cycle started", runtime.get("cycle_started_utc"))
    _render("Cycle ended", runtime.get("cycle_ended_utc"))
    _render("Evidence", runtime.get("evidence_ref"))
    _render("Promotion candidate", runtime.get("promotion_candidate_id"))
    _render("Promotion review", runtime.get("review_status"))
    _render("Promotion decision", runtime.get("decision"))
    _render("Promotion reason", runtime.get("decision_reason"))
    _render("Promotion summary", runtime.get("promotion_summary"))
    _render("Promotion schema", runtime.get("promotion_schema_version"))
    _render("Governance schema", runtime.get("governance_schema"))
    _render("Governance coverage", runtime.get("governance_coverage"))
    _render("Promotion provenance", runtime.get("promotion_provenance"))
    _render("Promotion candidate path", runtime.get("promotion_candidate_path"))
    _render("Promotion decision record", runtime.get("promotion_decision_record"))
    _render("Promotion accepted record", runtime.get("promotion_accepted_record"))
    _render("Promotion reviewed at", runtime.get("promotion_reviewed_at"))
    _render("Promotion accepted at", runtime.get("promotion_accepted_at"))
    _render("Patch bundle", runtime.get("promotion_patch_bundle_path"))
    _render("Promotion replay readiness", runtime.get("promotion_replay_readiness"))
    _render("Hypothesis backlog schema", runtime.get("hypothesis_backlog_schema_version"))
    _render("Follow-through", runtime.get("follow_through_status"))
    _render("Improvement score", runtime.get("improvement_score"))

    if isinstance(runtime.get("subagent_rollup"), dict):
        roll = runtime.get("subagent_rollup") or {}
        lines.append(
            "  Subagents: "
            f"enabled={roll.get('enabled')}, total={roll.get('count_total')}, done={roll.get('count_done')}, "
            f"queued={roll.get('count_queued')}, stale={roll.get('count_stale')}"
        )
    if runtime.get("artifact_paths"):
        artifacts = runtime.get("artifact_paths")
        if isinstance(artifacts, list):
            _render("Artifacts", ", ".join(str(item) for item in artifacts))
        else:
            _render("Artifacts", artifacts)
    _render("Promotion source", runtime.get("promotion_path"))
    _render("Approval gate", runtime.get("approval_gate"))
    _render("Gate state", runtime.get("approval_gate_state"))
    if runtime.get("approval_gate_ttl_minutes") is not None:
        _render("Gate TTL (min)", runtime.get("approval_gate_ttl_minutes"))
    _render("Next", runtime.get("next_hint"))
    _render("Goal rotation reason", runtime.get("goal_rotation_reason"))
    _render("Goal rotation streak", runtime.get("goal_rotation_streak"))
    _render("Goal rotation trigger", runtime.get("goal_rotation_trigger_goal"))
    if runtime.get("goal_rotation_trigger_artifact_paths"):
        trigger_artifacts = runtime.get("goal_rotation_trigger_artifact_paths")
        if isinstance(trigger_artifacts, list):
            _render("Goal rotation artifacts", ", ".join(str(item) for item in trigger_artifacts))
        else:
            _render("Goal rotation artifacts", trigger_artifacts)
    _render("Report source", runtime.get("report_path"))
    _render("Goal source", runtime.get("goal_path"))
    _render("Outbox source", runtime.get("outbox_path"))
    return lines