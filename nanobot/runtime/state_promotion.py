"""Promotion and governance snapshot logic extraction."""

from __future__ import annotations

from typing import Any

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
