"""Formalized minimal schemas for runtime state."""

from __future__ import annotations

from typing import Any, TypedDict

CONTROLLED_LESSON_TAGS: frozenset[str] = frozenset({
    "architecture", "config", "curator", "docs", "gate", "git", "infra",
    "lint", "perf", "prompt", "reflector", "refactor", "rotation", "runtime",
    "security", "sidecar", "state", "subagent", "test", "tooling",
})
LESSON_SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")


class CycleReport(TypedDict, total=False):
    schema_version: str
    cycle_id: str
    cycle_started_utc: str | None
    cycle_ended_utc: str | None
    goal_id: str | None
    goal_text: str | None
    current_task_id: str | None
    result_status: str | None
    decision: str | None
    improvement_score: int | float | None
    promotion_candidate_id: str | None
    review_status: str | None
    evidence_ref_id: str | None
    experiment: dict[str, Any] | None
    follow_through: dict[str, Any] | None
    result: dict[str, Any] | None

class PromotionCandidate(TypedDict, total=False):
    schema_version: str
    promotion_candidate_id: str
    review_status: str | None
    decision: str | None
    decision_reason: str | None
    candidate_path: str | None
    artifact_path: str | None
    readiness_checks: Any
    readiness_reasons: Any
    recommended_next_action: str | None
    governance_packet: dict[str, Any] | None
    decision_record: str | None
    accepted_record: str | None
    promotion_provenance: dict[str, Any] | None

class CycleHealth(TypedDict, total=False):
    schema_version: str
    runtime_state_source: str | None
    runtime_state_root: str | None
    latest_cycle_id: str | None
    latest_report_path: str | None
    latest_subagent_telemetry_id: str | None
    latest_subagent_telemetry_path: str | None
    service_status: dict[str, Any]
    failed_units_count: int | None
    promotion_readiness: dict[str, Any]
    severity: str
    exit_code: int
    next_recommended_action: str
    success_signals: dict[str, Any]


class LessonV2(TypedDict, total=False):
    schema_version: int
    id: str
    title: str
    problem: str
    solution: str
    tags: list[str]
    severity: str
    seen_count: int
    first_seen: str
    last_seen: str
    evidence: list[str] | dict[str, Any]
