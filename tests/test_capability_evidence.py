from pathlib import Path

from nanobot.runtime import capability_evidence


def _proposal(kind="lesson", operation="change", evidence=None):
    return {
        "schema_version": capability_evidence.SCHEMA_VERSION,
        "capability_change": {
            "kind": kind,
            "subject_id": "LESS-A",
            "operation": operation,
            "retrieval_evidence": evidence,
        },
    }


def test_executor_proposals_are_out_of_scope():
    assert capability_evidence.validate_trainer_capability_proposal({}, trainer_originated=False) == []


def test_source_must_be_explicit_and_closed():
    result = capability_evidence.validate_trainer_capability_proposal(
        _proposal(evidence={"mode": "source", "source": "../../state.json"}),
        state_dir=Path("state"),
    )
    assert any("not an allowed lesson source" in item for item in result)


def test_missing_source_is_not_silent():
    result = capability_evidence.validate_trainer_capability_proposal(
        _proposal(evidence=None), state_dir=Path("state")
    )
    assert "retrieval_evidence must explicitly name a source or no_source" in result


def test_no_source_is_only_for_new_add():
    evidence = {"mode": "no_source", "reason": "new_capability"}
    assert capability_evidence.validate_trainer_capability_proposal(
        _proposal(operation="add", evidence=evidence),
        existing_subject_ids=[], changed_subjects=["LESS-A"],
    ) == []

    result = capability_evidence.validate_trainer_capability_proposal(
        _proposal(operation="retire", evidence=evidence), existing_subject_ids=[]
    )
    assert "no_source is allowed only for a new capability" in result

    result = capability_evidence.validate_trainer_capability_proposal(
        _proposal(operation="add", evidence=evidence), existing_subject_ids=["LESS-A"]
    )
    assert "no_source is not allowed for an existing capability" in result


def test_source_requires_measured_subject_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(
        capability_evidence,
        "correlate_citations_with_outcomes",
        lambda *_a, **_k: {
            "status": "present", "ledger_status": "complete",
            "cited_subject_counts": {"LESS-A": 2},
            "cited_subject_outcome_counts": {"LESS-A": {"success": 2}},
        },
    )
    evidence = {
        "mode": "source",
        "source": "lesson_citation_outcomes_v1",
        "window_start": "2026-09-01T00:00:00Z",
        "window_end": "2026-09-16T00:00:00Z",
    }
    assert capability_evidence.validate_trainer_capability_proposal(
        _proposal(evidence=evidence), state_dir=tmp_path,
        changed_subjects=["LESS-A"],
    ) == []


def test_source_rejects_empty_or_unavailable_reader(monkeypatch, tmp_path):
    monkeypatch.setattr(
        capability_evidence, "correlate_citations_with_outcomes",
        lambda *_a, **_k: {"status": "empty", "ledger_status": "complete"},
    )
    evidence = {
        "mode": "source", "source": "lesson_citation_outcomes_v1",
        "window_start": "2026-09-01T00:00:00Z",
        "window_end": "2026-09-16T00:00:00Z",
    }
    result = capability_evidence.validate_trainer_capability_proposal(
        _proposal(evidence=evidence), state_dir=tmp_path,
        changed_subjects=["LESS-A"],
    )
    assert "retrieval evidence source is missing, empty, or unavailable" in result
