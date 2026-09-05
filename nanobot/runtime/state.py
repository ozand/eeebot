"""Canonical runtime state helpers for operator-facing summaries."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterator, Tuple

from nanobot.runtime.state_access import latest_file


_DEFAULT_HOST_CONTROL_PLANE_STATE_ROOT = Path("/var/lib/eeepc-agent/self-evolving-agent/state")


def _json_files_sorted_by_mtime(desc: bool, *dirs: Path) -> Iterator[Tuple[Path, float]]:
    """Yield (path, mtime) for all *.json files in *dirs*, sorted by mtime.

    Uses os.scandir() to avoid the double-stat penalty of
    ``path.is_file() + path.stat()`` — scandir caches the stat result
    from the directory entry, cutting syscalls in half for large
    subagent directories (143+ files → 143 stat calls instead of 286).
    """
    pairs: list[Tuple[Path, float]] = []
    for d in dirs:
        if not d.exists():
            continue
        try:
            with os.scandir(str(d)) as it:
                for entry in it:
                    if not entry.name.endswith('.json'):
                        continue
                    try:
                        if entry.is_file():
                            pairs.append((d / entry.name, entry.stat().st_mtime))
                    except OSError:
                        continue
        except OSError:
            continue
    pairs.sort(key=lambda p: p[1], reverse=desc)
    yield from pairs


def _safe_read_json(path: Path | None) -> Any:
    if not path:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_json_file(directory: Path, pattern: str) -> Path | None:
    """Compatibility wrapper around the shared deterministic latest reader."""
    return latest_file(directory, pattern, max_age_s=float("inf")).path


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
        next_action = replay.get('recommended_next_action') or 'replayable governance trail present'
    else:
        state = 'action_required'
        if replay and isinstance(replay, dict):
            next_action = replay.get('recommended_next_action') or replay.get('reason')
        else:
            next_action = 'complete_promotion_decision_record'
    return {
        'state': state,
        'projects_considered': 1,
        'ownership_gaps': ownership_gaps,
        'due_reviews': due_reviews,
        'next_action': next_action,
    }


def _promotion_replay_next_action(reason: str | None, state: str | None = None) -> str:
    if state == 'ready':
        return 'replay_promotion_candidate'
    if state == 'ready_for_policy_review':
        return 'review_promotion_candidate'
    if reason == 'promotion_candidate_not_ready_for_policy_review':
        return 'supply_missing_promotion_readiness_inputs' if state == 'blocked' else 'complete_promotion_readiness_packet'
    if reason == 'patch_bundle_missing':
        return 'generate_promotion_patch_bundle'
    if reason == 'not_accepted':
        return 'review_promotion_candidate'
    if reason and str(reason).startswith('missing_or_placeholder_provenance'):
        return 'complete_promotion_provenance'
    return 'resolve_promotion_replay_blocker'


def _promotion_replay_readiness_payload(
    *,
    state: str,
    reason: str,
    promotion_candidate_id: str | None,
    review_status: str | None,
    decision: str | None,
    promotion_candidate_path: str | None,
    promotion_artifact_path: str | None,
    promotion_decision_record: str | None,
    promotion_accepted_record: str | None,
    promotion_patch_bundle_path: str | None = None,
    promotion_readiness_checks: Any = None,
    promotion_readiness_reasons: Any = None,
    promotion_recommended_next_action: str | None = None,
) -> dict[str, Any]:
    missing_records = [
        name
        for name, status in {
            'decision_record': promotion_decision_record,
            'accepted_record': promotion_accepted_record,
        }.items()
        if status == 'missing'
    ]
    return {
        'schema_version': 'promotion-replay-readiness-v1',
        'state': state,
        'status': review_status or decision or reason,
        'reason': reason,
        'promotion_id': promotion_candidate_id,
        'review_status': review_status,
        'decision': decision,
        'review_packet_status': ('blocked_not_ready' if state == 'blocked' else 'not_ready') if reason == 'promotion_candidate_not_ready_for_policy_review' else state,
        'candidate_path': promotion_candidate_path,
        'artifact_path': promotion_artifact_path,
        'decision_record': promotion_decision_record,
        'accepted_record': promotion_accepted_record,
        'patch_bundle_path': promotion_patch_bundle_path,
        'missing_records': missing_records,
        'readiness_checks': promotion_readiness_checks,
        'readiness_reasons': promotion_readiness_reasons or [],
        'recommended_next_action': promotion_recommended_next_action or _promotion_replay_next_action(reason, state),
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
    subagent_rollup = runtime.get('subagent_rollup') if isinstance(runtime.get('subagent_rollup'), dict) else {}
    governance_schema = runtime.get('governance_schema') if isinstance(runtime.get('governance_schema'), dict) else {}
    promotion_governance_packet = runtime.get('promotion_governance_packet') if isinstance(runtime.get('promotion_governance_packet'), dict) else {}

    accepted_experiment = bool(
        (runtime.get('decision') or experiment.get('decision')) in {'accept', 'accepted', 'keep', 'pass'}
        or (runtime.get('experiment_outcome') or experiment.get('outcome')) in {'keep', 'accept', 'accepted'}
        or (runtime.get('review_status') or experiment.get('review_status')) == 'reviewed' and (runtime.get('decision') or experiment.get('decision')) == 'accept'
    )
    # Only the promotion replay-readiness input remains: the autoevolve
    # self_evolution/current_state.json feed was never written on the host and
    # was retired in #1224.
    merged_selfevo_pr = bool(
        (runtime.get('promotion_replay_readiness') or {}).get('state') == 'ready'
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
    latest_subagent_age = latest_subagent_result.get('age_seconds') if isinstance(latest_subagent_result, dict) else None
    try:
        latest_subagent_age_int = int(latest_subagent_age) if latest_subagent_age is not None else None
    except (TypeError, ValueError):
        latest_subagent_age_int = None
    fresh_subagent_window_seconds = 6 * 60 * 60
    latest_subagent_fresh = latest_subagent_age_int is not None and latest_subagent_age_int <= fresh_subagent_window_seconds
    consumed_subagent_result = bool(
        (subagent_terminal_count or _present(latest_subagent_result))
        and latest_subagent_status not in {'blocked', 'failed', 'error'}
        and not subagent_only_blocked
        and latest_subagent_fresh
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
                'latest_result_age_seconds': latest_subagent_age_int,
                'freshness_state': 'fresh' if latest_subagent_fresh else 'stale',
                'freshness_window_seconds': fresh_subagent_window_seconds,
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
        if subagent_terminal_count and not latest_subagent_fresh:
            non_qualifying_proofs.append('stale_subagent_result')
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


_SUBAGENT_ROLLUP_CACHE: dict[str, Any] = {"loaded_at": 0.0, "state_root": None, "root_mtime_ns": None, "result": None}
_SUBAGENT_ROLLUP_CACHE_TTL_SECONDS = 5.0


def _subagent_rollup_snapshot(
    *,
    state_root: Path,
    current_task_id: str | None = None,
    current_task_title: str | None = None,
    stale_after_seconds: int = 3600,
) -> dict[str, Any] | None:
    now = time.monotonic()
    cached_root = _SUBAGENT_ROLLUP_CACHE.get("state_root")
    cached_result = _SUBAGENT_ROLLUP_CACHE.get("result")
    cached_root_mtime_ns = _SUBAGENT_ROLLUP_CACHE.get("root_mtime_ns")
    loaded_at = float(_SUBAGENT_ROLLUP_CACHE.get("loaded_at", 0.0) or 0.0)
    subagents_dir = state_root / 'subagents'
    try:
        root_mtime_ns = subagents_dir.stat().st_mtime_ns if subagents_dir.exists() else None
    except Exception:
        root_mtime_ns = None
    if (
        cached_result is not None
        and cached_root == str(state_root)
        and cached_root_mtime_ns == root_mtime_ns
        and now - loaded_at < _SUBAGENT_ROLLUP_CACHE_TTL_SECONDS
    ):
        return cached_result

    _subagent_rollup_snapshot_uncached(
        state_root=state_root,
        current_task_id=current_task_id,
        current_task_title=current_task_title,
        stale_after_seconds=stale_after_seconds,
        subagents_dir=subagents_dir,
        root_mtime_ns=root_mtime_ns,
        now=now,
    )
    return _SUBAGENT_ROLLUP_CACHE["result"]


def _subagent_rollup_snapshot_uncached(
    *,
    state_root: Path,
    current_task_id: str | None = None,
    current_task_title: str | None = None,
    stale_after_seconds: int = 3600,
    subagents_dir: Path,
    root_mtime_ns: int | None,
    now: float,
) -> None:
    # Cache wall-clock once for all age calculations; avoids 3 redundant time.time() calls
    _wall_clock = time.time()
    request_dir = subagents_dir / 'requests'
    result_dir = subagents_dir / 'results'

    completed_statuses = {'ok', 'error', 'cancelled', 'canceled', 'completed', 'complete', 'done', 'pass'}
    queued_statuses = {'queued', 'pending'}

    telemetry_records: list[dict[str, Any]] = []
    terminal_telemetry_results: dict[str, dict[str, Any]] = {}
    if subagents_dir.exists():
        telemetry_paths = list(_json_files_sorted_by_mtime(True, subagents_dir))
        for path, path_mtime in telemetry_paths:
            payload = _safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            task_id = payload.get('subagent_id') or payload.get('task_id') or payload.get('id')
            request_id = payload.get('request_id') or payload.get('id')
            if not task_id and not request_id:
                continue
            semantic_task_id = payload.get('semantic_task_id') or task_id
            verification_task_id = payload.get('verification_task_id') or request_id
            status = str(payload.get('status') or 'unknown')
            telemetry_record = {
                'path': str(path),
                'task_id': task_id,
                'semantic_task_id': semantic_task_id,
                'request_id': request_id,
                'verification_task_id': verification_task_id,
                'verification_role': payload.get('verification_role'),
                'status': status,
                'summary': payload.get('summary') or payload.get('result'),
                'started_at': payload.get('started_at'),
                'finished_at': payload.get('finished_at'),
                'origin': payload.get('origin'),
                'runtime_state_source': payload.get('runtime_state_source'),
            }
            telemetry_records.append(telemetry_record)
            if task_id and status.lower() in completed_statuses:
                terminal_result = {
                    'path': str(path),
                    'task_id': task_id,
                    'semantic_task_id': semantic_task_id,
                    'request_id': request_id,
                    'verification_task_id': verification_task_id,
                    'verification_role': payload.get('verification_role'),
                    'task_title': payload.get('title') or payload.get('summary') or task_id,
                    'cycle_id': payload.get('cycle_id') or payload.get('cycleId'),
                    'status': status,
                    'summary': payload.get('summary') or payload.get('result'),
                    'age_seconds': max(0, int(_wall_clock - path_mtime)),
                    'materialized_from': 'telemetry',
                }
                if request_id:
                    terminal_telemetry_results.setdefault(str(request_id), terminal_result)
                terminal_telemetry_results.setdefault(str(task_id), terminal_result)

    request_records: list[dict[str, Any]] = []
    if request_dir.exists():
        request_paths = list(_json_files_sorted_by_mtime(True, request_dir))
        for path, path_mtime in request_paths:
            payload = _safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            task_id = payload.get('task_id') or payload.get('taskId')
            request_id = payload.get('request_id') or payload.get('id')
            semantic_task_id = payload.get('semantic_task_id') or task_id
            verification_task_id = payload.get('verification_task_id') or request_id
            original_status = str(payload.get('request_status') or payload.get('status') or 'queued')
            materialized_result = terminal_telemetry_results.get(str(request_id)) if request_id else None
            if materialized_result is None and not request_id:
                materialized_result = terminal_telemetry_results.get(str(task_id)) if task_id else None
            effective_status = 'completed' if materialized_result else original_status
            age_seconds = max(0, int(_wall_clock - path_mtime))
            request_records.append({
                'path': str(path),
                'task_id': task_id,
                'semantic_task_id': semantic_task_id,
                'request_id': request_id,
                'verification_task_id': verification_task_id,
                'verification_role': payload.get('verification_role'),
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
    results_by_request_id: dict[str, dict[str, Any]] = {}
    results_by_cycle_id: dict[str, dict[str, Any]] = {}
    results_by_task_id: dict[str, dict[str, Any]] = {}
    if result_dir.exists():
        result_paths = list(_json_files_sorted_by_mtime(True, result_dir))
        for path, path_mtime in result_paths:
            payload = _safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            status = str(payload.get('status') or payload.get('result_status') or 'completed')
            result = {
                'path': str(path),
                'request_path': payload.get('request_path'),
                'request_id': payload.get('request_id') or payload.get('id'),
                'semantic_task_id': payload.get('semantic_task_id') or payload.get('task_id') or payload.get('taskId') or payload.get('subagent_id'),
                'verification_task_id': payload.get('verification_task_id') or payload.get('request_id') or payload.get('id'),
                'verification_role': payload.get('verification_role'),
                'task_id': payload.get('task_id') or payload.get('taskId') or payload.get('subagent_id'),
                'task_title': payload.get('task_title') or payload.get('title') or payload.get('summary'),
                'cycle_id': payload.get('cycle_id') or payload.get('cycleId'),
                'status': status,
                'summary': payload.get('summary') or payload.get('result'),
                'key_learnings': payload.get('key_learnings') if isinstance(payload.get('key_learnings'), list) else [],
                'learning_classification': payload.get('learning_classification'),
                'age_seconds': max(0, int(_wall_clock - path_mtime)),
            }
            result_records.append(result)
            if result.get('request_path'):
                results_by_request_path.setdefault(str(result['request_path']), result)
            if result.get('request_id'):
                results_by_request_id.setdefault(str(result['request_id']), result)
            if result.get('cycle_id'):
                results_by_cycle_id.setdefault(str(result['cycle_id']), result)
            if result.get('task_id'):
                results_by_task_id.setdefault(str(result['task_id']), result)
    existing_result_paths = {record.get('path') for record in result_records}
    _seen_telemetry_paths: set[str] = set()
    for result_key, result in terminal_telemetry_results.items():
        result_path = result.get('path')
        if result_path not in _seen_telemetry_paths:
            _seen_telemetry_paths.add(result_path)
            if result_path not in existing_result_paths:
                result_records.append(result)
        results_by_task_id.setdefault(str(result_key), result)
    for request in request_records:
        task_id = request.get('task_id')
        request_id = request.get('request_id')
        cycle_id = request.get('cycle_id')
        materialized_result = (
            (results_by_request_id.get(str(request_id)) if request_id else None)
            or results_by_request_path.get(str(request.get('path')))
            or (results_by_cycle_id.get(str(cycle_id)) if cycle_id else None)
            or (results_by_task_id.get(str(task_id)) if task_id and not request_id else None)
        )
        if isinstance(materialized_result, dict):
            request['materialized_result_path'] = materialized_result.get('path')
            request['materialized_result_status'] = materialized_result.get('status')
            request['status'] = str(materialized_result.get('status') or 'completed').lower()
    result_records = sorted(result_records, key=lambda record: record.get('age_seconds') or 0)

    if not telemetry_records and not request_records and not result_records:
        _SUBAGENT_ROLLUP_CACHE.update({
            "loaded_at": now,
            "state_root": str(state_root),
            "root_mtime_ns": root_mtime_ns,
            "result": None,
        })
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

    # Precompute task_id lookup dicts for O(1) match lookups instead of O(n) linear scans.
    # Use reversed() so that newer records (which appear first in the mtime-sorted list)
    # overwrite older ones, ensuring the dict values contain the newest entries.
    requests_by_task_id: dict[str, dict[str, Any]] = {
        str(r['task_id']): r for r in reversed(request_records) if r.get('task_id')
    }
    telemetry_by_task_id: dict[str, dict[str, Any]] = {
        str(r['task_id']): r for r in reversed(telemetry_records) if r.get('task_id')
    }

    preferred_task_id = current_task_id
    request_match = requests_by_task_id.get(preferred_task_id) if preferred_task_id else None
    telemetry_match = telemetry_by_task_id.get(preferred_task_id) if preferred_task_id else None
    result_match = None
    request_match_id = (request_match or {}).get('request_id')
    if request_match_id:
        result_match = results_by_request_id.get(str(request_match_id))
    elif preferred_task_id:
        result_match = results_by_task_id.get(preferred_task_id)

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
        'semantic_task_id': (request_match or {}).get('semantic_task_id') or (result_match or {}).get('semantic_task_id') or (telemetry_match or {}).get('semantic_task_id') or preferred_task_id,
        'request_id': (request_match or {}).get('request_id') or (result_match or {}).get('request_id') or (telemetry_match or {}).get('request_id'),
        'verification_task_id': (request_match or {}).get('verification_task_id') or (result_match or {}).get('verification_task_id') or (telemetry_match or {}).get('verification_task_id'),
        'verification_role': (request_match or {}).get('verification_role') or (result_match or {}).get('verification_role') or (telemetry_match or {}).get('verification_role'),
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

    result = {
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
    _SUBAGENT_ROLLUP_CACHE.update({
        "loaded_at": now,
        "state_root": str(state_root),
        "root_mtime_ns": root_mtime_ns,
        "result": result,
    })
    return result


# ─── live status surface (#914) ──────────────────────────────────────────
#
# The sections below point the operator status/health surface at sources
# that the CURRENT (post-coordinator-decommission, #900/#910) loop actually
# updates: the operator's goal_text.json, the cycle ledger, and the
# scorecard. #914 introduced them beside the coordinator-era reads
# (``goals/current.json``/``outbox/``/``credits/``/``reports/evolution-*``);
# #1222 removed those reads outright. Every helper here is independently
# fail-open: a missing or corrupt source file yields ``None``/``[]`` for
# just that field, never an exception, so one bad artifact can never blank
# out the rest of the surface (or crash the CLI/health check that reads it).

_LIVE_LEDGER_TAIL_LINES = 200
_LIVE_RECENT_OUTCOMES_LIMIT = 5


def _live_active_goal_id(state_root: Path) -> str | None:
    """Active goal id from the operator canon, ``goals/goal_text.json``.

    #914 read ``goals/registry.json`` here believing the current loop
    rewrote it every cycle; it did not — the coordinator was its only
    writer and the file froze on 2026-08-22 (#1222). ``goal_text.json``
    (seeded by ``deploy_release.sh``) carries ``goal_id``. Fail-open to
    ``None``.
    """
    from nanobot.runtime.goal_review import active_goal_id

    goal_id = active_goal_id(state_root)
    return goal_id or None


def _live_approval_gate_state(state_root: Path, *, now: float | None = None) -> str:
    """``fresh`` / ``expired`` / ``missing`` from ``approvals/apply.ok``.

    Same file and same rule as ``bridge.approval_open()`` (``expires_at_epoch``
    in the future). #1222: the coordinator copied this into ``outbox/`` and the
    status surface read the copy; the copy froze, the file did not. Fail-open
    to ``missing``.
    """
    data = _safe_read_json(state_root / "approvals" / "apply.ok")
    if not isinstance(data, dict):
        return "missing"
    try:
        expires = int(data.get("expires_at_epoch", 0) or 0)
    except (TypeError, ValueError):
        return "missing"
    return "fresh" if expires > int(now if now is not None else time.time()) else "expired"


def _live_recent_outcomes(
    state_root: Path, limit: int = _LIVE_RECENT_OUTCOMES_LIMIT
) -> list[dict[str, Any]]:
    """Last ``limit`` terminal ledger outcomes, newest first.

    Reads ``ledger/cycles.jsonl`` with a bounded tail — lines are streamed
    into a ``deque(maxlen=_LIVE_LEDGER_TAIL_LINES)`` one at a time, capping
    memory even if the ledger is unexpectedly large (same bounded-tail
    pattern the retired backlog snapshot used, #1356). Each ``phase:
    "outcome"`` row is enriched with the ``task_title`` carried by its
    matching ``phase: "proposed"`` row (same ``cycle_id``) when one is
    present in the tail window — the outcome row itself never carries a
    title (see ``cycle_ledger.record_cycle_outcome``). Fail-open to ``[]``.
    """
    path = state_root / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    try:
        tail: 'deque[str]' = deque(maxlen=_LIVE_LEDGER_TAIL_LINES)
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                tail.append(line)
    except Exception:
        return []

    records: list[dict[str, Any]] = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            records.append(rec)

    proposed_titles: dict[str, str] = {}
    for rec in records:
        if rec.get("phase") == "proposed" and rec.get("cycle_id") and rec.get("task_title"):
            proposed_titles[str(rec["cycle_id"])] = str(rec["task_title"])

    outcomes: list[dict[str, Any]] = []
    for rec in reversed(records):
        if rec.get("phase") != "outcome":
            continue
        cycle_id = rec.get("cycle_id")
        outcomes.append(
            {
                "cycle_id": cycle_id or None,
                "outcome": rec.get("outcome"),
                "ts": rec.get("ts"),
                "files_changed": rec.get("files_changed"),
                "task_title": proposed_titles.get(str(cycle_id)) if cycle_id else None,
            }
        )
        if len(outcomes) >= limit:
            break
    return outcomes


def _live_scorecard_snapshot(state_root: Path) -> dict[str, Any] | None:
    """Key fitness metrics + active preset/models from the live scorecard.

    Reads ``scorecard/latest.json`` (persisted by
    ``nanobot.runtime.scorecard.compute_scorecard``) — never a coordinator
    artifact. Fail-open to ``None`` when the file is missing/corrupt or not
    a dict; individual metrics are fail-open to ``None`` within it.
    """
    data = _safe_read_json(state_root / "scorecard" / "latest.json")
    if not isinstance(data, dict):
        return None
    loop = data.get("loop") if isinstance(data.get("loop"), dict) else {}
    quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
    control_plane = data.get("control_plane") if isinstance(data.get("control_plane"), dict) else {}
    models = control_plane.get("models")
    return {
        "computed_at_utc": data.get("computed_at_utc"),
        "confirmed_integration_ratio": loop.get("confirmed_integration_ratio"),
        "repeat_failure_rate": loop.get("repeat_failure_rate"),
        "idle_share": loop.get("idle_share"),
        "compile_clean_ratio": quality.get("compile_clean_ratio"),
        "preset": control_plane.get("SELFEVO_PRESET"),
        "models": models if isinstance(models, dict) else None,
    }


def _live_state_snapshot(state_root: Path) -> dict[str, Any] | None:
    """Assemble the `live` operator-status section (#914).

    Returns ``None`` only when goal registry, ledger, and scorecard all
    yield nothing at all — a clean "nothing live to show" signal rather
    than a dict of all-``None`` fields. Each of the three sources is
    independently fail-open, so one missing/corrupt file never blanks the
    other two.
    """
    try:
        active_goal_id = _live_active_goal_id(state_root)
    except Exception:
        active_goal_id = None
    try:
        recent_outcomes = _live_recent_outcomes(state_root)
    except Exception:
        recent_outcomes = []
    try:
        scorecard = _live_scorecard_snapshot(state_root)
    except Exception:
        scorecard = None

    if active_goal_id is None and not recent_outcomes and scorecard is None:
        return None

    return {
        "active_goal_id": active_goal_id,
        "recent_outcomes": recent_outcomes,
        "scorecard": scorecard,
    }


def load_runtime_state_from_root(state_root: Path, source_kind: str = "workspace_state") -> dict[str, Any]:
    """Load canonical runtime state from an explicit state root if present.

    #1222: this surface reads only files something still writes — the
    operator's ``goals/goal_text.json``, ``promotions/`` (bridge),
    ``subagents/`` telemetry, the host resource sensors and the ``live``
    section (ledger + scorecard). #1356 dropped the ``hypotheses/backlog.json``
    fields with their writer (see ``hypothesis_backlog`` for the history). The
    coordinator's per-cycle artifacts — ``reports/evolution-*.json``,
    ``outbox/``, ``goals/{registry,active,current}.json``, ``credits/`` — froze
    on 2026-08-22 when it was deleted (#916/#923); #914 labelled them
    "decommissioned" and #1205 stopped presenting the stale report, but the
    reads (and ~40 fields derived only from them) stayed. They are gone: a
    field with no live source is not "unknown", it is not a field.
    """
    goals_dir = state_root / "goals"
    promotions_dir = state_root / "promotions"
    subagents_dir = state_root / "subagents"

    goal_text_path = goals_dir / "goal_text.json"
    latest_promotion = _latest_json_file(promotions_dir, "latest.json") or _latest_json_file(promotions_dir, "*.json")
    latest_subagent = _latest_json_file(subagents_dir, "*.json")

    goal_text_data = _safe_read_json(goal_text_path)
    promotion_data = _safe_read_json(latest_promotion)
    subagent_data = _safe_read_json(latest_subagent)

    # The operator canon is the only goal source (goal_review.active_goal_id
    # reads the same file for the bridge and the proposer).
    active_goal = None
    goal_text = None
    if isinstance(goal_text_data, dict):
        goal_id = goal_text_data.get("goal_id")
        active_goal = goal_id.strip() if isinstance(goal_id, str) and goal_id.strip() else None
        text = goal_text_data.get("text")
        goal_text = text if isinstance(text, str) and text.strip() else None

    # The bounded-apply gate is the operator's approvals/apply.ok (the same
    # file bridge.approval_open() consults); the coordinator used to copy its
    # state into outbox/ and this surface read the copy.
    approval_gate_state = _live_approval_gate_state(state_root)

    promotion_candidate_id = None
    review_status = None
    decision = None
    decision_reason = None
    promotion_schema_version = None
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
    promotion_recommended_next_action = None
    promotion_governance_packet = None
    promotion_provenance = None
    subagent_telemetry_count = None  # deferred to _subagent_rollup_snapshot.telemetry_count
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
    # Subagent rollup from the telemetry files themselves (live); nothing
    # pre-selects a task any more, so no current_task_id to correlate on.
    subagent_rollup = _subagent_rollup_snapshot(state_root=state_root, current_task_id=None)
    if isinstance(subagent_rollup, dict):
        subagent_telemetry_count = subagent_rollup.get("telemetry_count")
    if subagent_telemetry_count is None:
        subagent_telemetry_count = len(list(subagents_dir.glob("*.json"))) if subagents_dir.exists() else 0

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
        promotion_recommended_next_action = promotion_data.get("recommended_next_action") or promotion_data.get("recommendedNextAction") or promotion_recommended_next_action
        promotion_governance_packet = promotion_data.get("governance_packet") or promotion_data.get("governancePacket") or promotion_governance_packet
        promotion_decision_record = promotion_data.get("decision_record") or promotion_data.get("decisionRecord") or promotion_decision_record
        promotion_accepted_record = promotion_data.get("accepted_record") or promotion_data.get("acceptedRecord") or promotion_accepted_record
        promotion_provenance = _promotion_provenance_snapshot(promotion_data)

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
        readiness_packet_path = promotions_dir / "readiness_packets" / f"{promotion_candidate_id}.json"
        promotion_decision_record = "present" if decision_record_path.exists() else "missing"
        promotion_accepted_record = "present" if accepted_record_path.exists() else "missing"
        if readiness_packet_path.exists() and not decision_record_path.exists() and not accepted_record_path.exists():
            readiness_packet = _safe_read_json(readiness_packet_path)
            if isinstance(readiness_packet, dict) and readiness_packet.get("schema_version") == "promotion-readiness-packet-v1":
                promotion_decision_record = readiness_packet.get("decision_record") or "blocked_not_ready"
                promotion_accepted_record = readiness_packet.get("accepted_record") or "not_created_not_ready"
                promotion_readiness_reasons = readiness_packet.get("readiness_reasons") or promotion_readiness_reasons
                promotion_readiness_checks = readiness_packet.get("readiness_checks") or promotion_readiness_checks
                promotion_recommended_next_action = readiness_packet.get("recommended_next_action") or promotion_recommended_next_action
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
                promotion_replay_readiness = _promotion_replay_readiness_payload(
                    state='ready',
                    reason='accepted_bundle_present_and_provenance_complete',
                    promotion_candidate_id=promotion_candidate_id,
                    review_status=review_status,
                    decision=decision,
                    promotion_candidate_path=promotion_candidate_path,
                    promotion_artifact_path=promotion_artifact_path,
                    promotion_decision_record=promotion_decision_record,
                    promotion_accepted_record=promotion_accepted_record,
                    promotion_patch_bundle_path=promotion_patch_bundle_path,
                    promotion_readiness_checks=promotion_readiness_checks,
                    promotion_readiness_reasons=promotion_readiness_reasons,
                )
            else:
                reason = (promotion_provenance or {}).get('blocking_reason') or 'provenance_missing_or_placeholder'
                promotion_replay_readiness = _promotion_replay_readiness_payload(
                    state='blocked',
                    reason=reason,
                    promotion_candidate_id=promotion_candidate_id,
                    review_status=review_status,
                    decision=decision,
                    promotion_candidate_path=promotion_candidate_path,
                    promotion_artifact_path=promotion_artifact_path,
                    promotion_decision_record=promotion_decision_record,
                    promotion_accepted_record=promotion_accepted_record,
                    promotion_patch_bundle_path=promotion_patch_bundle_path,
                    promotion_readiness_checks=promotion_readiness_checks,
                    promotion_readiness_reasons=promotion_readiness_reasons,
                )
        elif decision == 'accept' and review_status == 'reviewed':
            promotion_replay_readiness = _promotion_replay_readiness_payload(
                state='blocked',
                reason='patch_bundle_missing',
                promotion_candidate_id=promotion_candidate_id,
                review_status=review_status,
                decision=decision,
                promotion_candidate_path=promotion_candidate_path,
                promotion_artifact_path=promotion_artifact_path,
                promotion_decision_record=promotion_decision_record,
                promotion_accepted_record=promotion_accepted_record,
                promotion_patch_bundle_path=promotion_patch_bundle_path,
                promotion_readiness_checks=promotion_readiness_checks,
                promotion_readiness_reasons=promotion_readiness_reasons,
                promotion_recommended_next_action=promotion_recommended_next_action,
            )
        elif decision == 'ready_for_policy_review' or review_status == 'ready_for_policy_review':
            promotion_replay_readiness = _promotion_replay_readiness_payload(
                state='ready_for_policy_review',
                reason='promotion_candidate_ready_for_policy_review',
                promotion_candidate_id=promotion_candidate_id,
                review_status=review_status,
                decision=decision,
                promotion_candidate_path=promotion_candidate_path,
                promotion_artifact_path=promotion_artifact_path,
                promotion_decision_record=promotion_decision_record,
                promotion_accepted_record=promotion_accepted_record,
                promotion_patch_bundle_path=promotion_patch_bundle_path,
                promotion_readiness_checks=promotion_readiness_checks,
                promotion_readiness_reasons=promotion_readiness_reasons,
                promotion_recommended_next_action=promotion_recommended_next_action,
            )
        elif decision in {'not_ready_for_policy_review', 'pending'} or review_status == 'not_ready_for_policy_review':
            not_ready_state = 'blocked' if promotion_decision_record == 'blocked_not_ready' or promotion_accepted_record == 'not_created_not_ready' else 'not_ready'
            promotion_replay_readiness = _promotion_replay_readiness_payload(
                state=not_ready_state,
                reason='promotion_candidate_not_ready_for_policy_review',
                promotion_candidate_id=promotion_candidate_id,
                review_status=review_status,
                decision=decision,
                promotion_candidate_path=promotion_candidate_path,
                promotion_artifact_path=promotion_artifact_path,
                promotion_decision_record=promotion_decision_record,
                promotion_accepted_record=promotion_accepted_record,
                promotion_patch_bundle_path=promotion_patch_bundle_path,
                promotion_readiness_checks=promotion_readiness_checks,
                promotion_readiness_reasons=promotion_readiness_reasons,
                promotion_recommended_next_action=promotion_recommended_next_action,
            )
        elif decision:
            promotion_replay_readiness = _promotion_replay_readiness_payload(
                state='blocked',
                reason='not_accepted',
                promotion_candidate_id=promotion_candidate_id,
                review_status=review_status,
                decision=decision,
                promotion_candidate_path=promotion_candidate_path,
                promotion_artifact_path=promotion_artifact_path,
                promotion_decision_record=promotion_decision_record,
                promotion_accepted_record=promotion_accepted_record,
                promotion_patch_bundle_path=promotion_patch_bundle_path,
                promotion_readiness_checks=promotion_readiness_checks,
                promotion_readiness_reasons=promotion_readiness_reasons,
                promotion_recommended_next_action=promotion_recommended_next_action,
            )

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
        "goal_text": goal_text,
        "goal_path": str(goal_text_path) if goal_text_path.exists() else None,
        "approval_gate_state": approval_gate_state,
        "subagent_rollup": subagent_rollup,
        "promotion_path": promotion_path,
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
    }
    runtime["capabilities"] = _capability_snapshot(runtime)
    runtime["subagent_correlation"] = _subagent_correlation_snapshot(runtime)
    runtime["operator_boost"] = _safe_runtime_config_operator_boost()
    runtime["governance_coverage"] = _governance_coverage_snapshot(runtime)
    runtime["material_progress"] = _material_progress_snapshot(runtime)
    runtime["live"] = _live_state_snapshot(state_root)
    return runtime



def load_runtime_state(workspace: Path) -> dict[str, Any]:
    """Load canonical runtime state from the workspace if present."""
    return load_runtime_state_from_root(workspace / "state", source_kind="workspace_state")


def format_runtime_state(runtime: dict[str, Any]) -> list[str]:
    """Format the canonical runtime state into stable user-facing lines.

    #914: the `live` section (active goal, recent ledger outcomes, scorecard
    metrics/preset) is rendered FIRST — it is what the current loop keeps
    fresh. #1222: the sections that follow read only live sources too
    (goal_text.json, promotions/, subagents/ — hypotheses/backlog.json left
    with its writer in #1356), so
    the "(decommissioned — frozen data)" suffix #914 added has nothing left
    to label and is gone with the frozen reads.
    """
    lines = ["Runtime:"]

    def _render(label: str, value: Any) -> None:
        if value in (None, ""):
            lines.append(f"  {label}: unknown")
        elif isinstance(value, dict):
            compact = ", ".join(f"{k}={v}" for k, v in value.items())
            lines.append(f"  {label}: {compact or 'unknown'}")
        else:
            lines.append(f"  {label}: {value}")

    live = runtime.get("live") if isinstance(runtime.get("live"), dict) else None
    lines.append("Live status:")
    if live:
        _render("Active goal (live)", live.get("active_goal_id"))
        outcomes = live.get("recent_outcomes") if isinstance(live.get("recent_outcomes"), list) else []
        if outcomes:
            lines.append("  Recent outcomes (live):")
            for item in outcomes:
                if not isinstance(item, dict):
                    continue
                bits = [
                    f"cycle={item.get('cycle_id') or 'unknown'}",
                    f"outcome={item.get('outcome') or 'unknown'}",
                ]
                if item.get("task_title"):
                    bits.append(f"title={item.get('task_title')}")
                if item.get("ts"):
                    bits.append(f"ts={item.get('ts')}")
                lines.append(f"    {' '.join(bits)}")
        else:
            lines.append("  Recent outcomes (live): unknown")
        scorecard = live.get("scorecard") if isinstance(live.get("scorecard"), dict) else None
        if scorecard:
            _render("Scorecard confirmed integration ratio", scorecard.get("confirmed_integration_ratio"))
            _render("Scorecard repeat failure rate", scorecard.get("repeat_failure_rate"))
            _render("Scorecard compile clean ratio", scorecard.get("compile_clean_ratio"))
            _render("Scorecard idle share", scorecard.get("idle_share"))
            _render("Active preset (live)", scorecard.get("preset"))
            _render("Active models (live)", scorecard.get("models"))
        else:
            lines.append("  Scorecard (live): unknown")
    else:
        lines.append("  Active goal (live): unknown")
        lines.append("  Recent outcomes (live): unknown")
        lines.append("  Scorecard (live): unknown")

    lines.append("Legacy runtime detail:")
    _render("Runtime state source", runtime.get("runtime_state_source"))
    _render("Runtime state root", runtime.get("runtime_state_root"))
    _render("Active goal", runtime.get("active_goal"))
    _render("Goal text", runtime.get("goal_text"))
    _render("Goal source", runtime.get("goal_path"))
    _render("Approval gate (apply.ok)", runtime.get("approval_gate_state"))
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

    if isinstance(runtime.get("subagent_rollup"), dict):
        roll = runtime.get("subagent_rollup") or {}
        lines.append(
            "  Subagents: "
            f"enabled={roll.get('enabled')}, total={roll.get('count_total')}, done={roll.get('count_done')}, "
            f"queued={roll.get('count_queued')}, stale={roll.get('count_stale')}"
        )
    _render("Promotion source", runtime.get("promotion_path"))
    return lines