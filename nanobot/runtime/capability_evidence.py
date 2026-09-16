"""Harness-owned retrieval evidence contract for capability proposals (#1666).

This module validates the *reference* a trainer proposal makes to retrieval
measurement. It never trusts counts, outcome maps, paths, or prose supplied by
an LLM. A source reference is a closed selector resolved by the harness; the
resolved counts come from the existing read-only citation/ledger reader.

The contract is deliberately preparatory. No current executor or reflector
route calls this validator, and it grants no mutation authority. It gives the
future trainer route one fail-closed gate to call once that route exists.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Mapping

from nanobot.runtime.lesson_v2 import correlate_citations_with_outcomes

SCHEMA_VERSION = "trainer-capability-proposal-v1"
_ALLOWED_KINDS = frozenset({"skill", "lesson"})
_ALLOWED_OPERATIONS = frozenset({"add", "change", "retire"})
_ALLOWED_MODES = frozenset({"source", "no_source"})
_SOURCE_BY_KIND = {"lesson": "lesson_citation_outcomes_v1"}
_SUBJECT_ID_MAX = 200


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _timestamp(value: Any) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _changed_subject_violations(
    subject_id: str,
    changed_subjects: Collection[str],
) -> list[str]:
    if not changed_subjects:
        return []
    subjects = {_text(value) for value in changed_subjects if _text(value)}
    if len(subjects) != 1:
        return ["trainer capability proposal changes multiple capability subjects"]
    if subject_id not in subjects:
        return ["trainer capability proposal subject_id does not match changed capability"]
    return []


def validate_trainer_capability_proposal(
    proposal: Mapping[str, Any] | Any,
    *,
    state_dir: Path | None = None,
    existing_subject_ids: Collection[str] = (),
    changed_subjects: Collection[str] = (),
    trainer_originated: bool = True,
    now: datetime | None = None,
) -> list[str]:
    """Return fail-closed violations for one trainer capability proposal.

    ``trainer_originated`` is supplied by the trusted route, not read from the
    model proposal. Ordinary executor proposals are intentionally out of scope
    and return no violations. For a ``source`` proposal, the only accepted
    source currently is the lesson citation/ledger reader already implemented
    by :func:`correlate_citations_with_outcomes`; its returned per-lesson
    counts/outcomes are authoritative. Skill retrieval has no production
    reader yet and therefore cannot pass this gate.

    ``no_source`` is not a fallback for an unreadable store. It is reserved for
    an explicitly declared creation of a genuinely new capability.
    """
    if not trainer_originated:
        return []
    violations: list[str] = []
    if not isinstance(proposal, Mapping):
        return ["trainer capability proposal is not an object"]
    if _text(proposal.get("schema_version")) != SCHEMA_VERSION:
        violations.append(f"schema_version must be {SCHEMA_VERSION}")
    change = proposal.get("capability_change")
    if not isinstance(change, Mapping):
        return violations + ["capability_change must be an object"]
    kind = _text(change.get("kind"))
    subject_id = _text(change.get("subject_id"))
    operation = _text(change.get("operation"))
    if kind not in _ALLOWED_KINDS:
        violations.append("capability_change.kind must be skill or lesson")
    if not subject_id or len(subject_id) > _SUBJECT_ID_MAX or "/" in subject_id or "\\" in subject_id:
        violations.append("capability_change.subject_id must be a bounded canonical id")
    if operation not in _ALLOWED_OPERATIONS:
        violations.append("capability_change.operation must be add, change, or retire")
    violations.extend(_changed_subject_violations(subject_id, changed_subjects))

    evidence = change.get("retrieval_evidence")
    if not isinstance(evidence, Mapping):
        return violations + ["retrieval_evidence must explicitly name a source or no_source"]
    mode = _text(evidence.get("mode"))
    if mode not in _ALLOWED_MODES:
        return violations + ["retrieval_evidence.mode must be source or no_source"]

    existing = {_text(value) for value in existing_subject_ids if _text(value)}
    if mode == "no_source":
        reason = _text(evidence.get("reason"))
        if operation != "add":
            violations.append("no_source is allowed only for a new capability")
        if subject_id in existing:
            violations.append("no_source is not allowed for an existing capability")
        if reason != "new_capability":
            violations.append("no_source requires reason=new_capability")
        if set(evidence) - {"mode", "reason"}:
            violations.append("no_source may contain only mode and reason")
        return violations

    source = _text(evidence.get("source"))
    expected = _SOURCE_BY_KIND.get(kind)
    if source != expected:
        if kind == "skill":
            violations.append("skill retrieval evidence source is unavailable")
        else:
            violations.append("retrieval_evidence.source is not an allowed lesson source")
        return violations
    if set(evidence) - {"mode", "source", "window_start", "window_end"}:
        violations.append("retrieval_evidence contains unsupported fields")
    start = _timestamp(evidence.get("window_start"))
    end = _timestamp(evidence.get("window_end"))
    if start is None or end is None or start >= end:
        violations.append("retrieval_evidence window must contain UTC window_start < window_end")
    if state_dir is None:
        return violations + ["retrieval evidence source cannot be resolved without state_dir"]
    measured = correlate_citations_with_outcomes(Path(state_dir), now=now)
    if measured.get("status") != "present" or measured.get("ledger_status") != "complete":
        violations.append("retrieval evidence source is missing, empty, or unavailable")
        return violations
    counts = measured.get("cited_subject_counts")
    outcomes = measured.get("cited_subject_outcome_counts")
    if not isinstance(counts, Mapping) or not isinstance(outcomes, Mapping):
        violations.append("retrieval evidence source has no subject-level counts and outcomes")
    elif subject_id not in counts or subject_id not in outcomes:
        violations.append("retrieval evidence has no measured rows for subject_id")
    return violations
