from __future__ import annotations

from nanobot.runtime.trainer_evidence import validate_trainer_citation

GOOD_SKILL_CITATION = {
    "kind": "skill",
    "target_id": "composite-context-inspection",
    "source": "skill_fitness.census",
    "retrieval_count": 0,
    "offered_or_shown": True,
}

GOOD_LESSON_CITATION = {
    "kind": "lesson",
    "target_id": "LESS-REF-2782e96c24a8",
    "source": "lesson_v2.lesson_zero_citation_census",
    "retrieval_count": 4,
}

GOOD_NO_EVIDENCE_CITATION = {
    "kind": "lesson",
    "target_id": "lessons/git_checkout_recovery.md",
    "source": "lesson_v2.lesson_zero_citation_census",
    "retrieval_count": None,
    "no_evidence_reason": "never offered into context in the retained scan window",
}


def test_good_skill_citation_has_no_violations():
    assert validate_trainer_citation(GOOD_SKILL_CITATION) == []


def test_good_lesson_citation_has_no_violations():
    assert validate_trainer_citation(GOOD_LESSON_CITATION) == []


def test_good_no_evidence_citation_has_no_violations():
    assert validate_trainer_citation(GOOD_NO_EVIDENCE_CITATION) == []


def test_not_a_dict_is_a_violation():
    assert validate_trainer_citation(["not", "a", "dict"]) == ["citation is not a JSON object"]
    assert validate_trainer_citation(None) == ["citation is not a JSON object"]


def test_invalid_kind_is_rejected():
    citation = {**GOOD_LESSON_CITATION, "kind": "artifact"}
    violations = validate_trainer_citation(citation)
    assert any("kind must be one of" in v for v in violations)


def test_missing_target_id_is_rejected():
    citation = dict(GOOD_LESSON_CITATION)
    citation.pop("target_id")
    violations = validate_trainer_citation(citation)
    assert any("target_id" in v for v in violations)


def test_empty_target_id_is_rejected():
    citation = {**GOOD_LESSON_CITATION, "target_id": "   "}
    violations = validate_trainer_citation(citation)
    assert any("target_id" in v for v in violations)


def test_missing_source_is_rejected():
    citation = dict(GOOD_LESSON_CITATION)
    citation.pop("source")
    violations = validate_trainer_citation(citation)
    assert any("source must name" in v for v in violations)


def test_retrieval_count_none_without_reason_is_rejected():
    citation = {**GOOD_LESSON_CITATION, "retrieval_count": None}
    violations = validate_trainer_citation(citation)
    assert any("no_evidence_reason is missing" in v for v in violations)


def test_retrieval_count_none_with_empty_reason_is_rejected():
    citation = {**GOOD_LESSON_CITATION, "retrieval_count": None, "no_evidence_reason": "   "}
    violations = validate_trainer_citation(citation)
    assert any("no_evidence_reason is missing" in v for v in violations)


def test_retrieval_count_bool_is_rejected():
    """bool is a subclass of int in Python -- must be caught explicitly."""
    citation = {**GOOD_LESSON_CITATION, "retrieval_count": True}
    violations = validate_trainer_citation(citation)
    assert any("must be an integer or None" in v for v in violations)


def test_retrieval_count_non_integer_is_rejected():
    citation = {**GOOD_LESSON_CITATION, "retrieval_count": 4.5}
    violations = validate_trainer_citation(citation)
    assert any("must be an integer or None" in v for v in violations)


def test_negative_retrieval_count_is_rejected():
    citation = {**GOOD_LESSON_CITATION, "retrieval_count": -1}
    violations = validate_trainer_citation(citation)
    assert any("must not be negative" in v for v in violations)


def test_zero_retrieval_count_without_offered_flag_is_rejected():
    """#1672: 'never offered' is not evidence of 'not useful'. A citation
    claiming a zero count must also assert the thing was actually shown."""
    citation = {
        "kind": "lesson",
        "target_id": "lessons/never_shown.md",
        "source": "lesson_v2.lesson_zero_citation_census",
        "retrieval_count": 0,
    }
    violations = validate_trainer_citation(citation)
    assert any("never offered is not evidence of not useful" in v for v in violations)


def test_zero_retrieval_count_with_offered_flag_is_valid():
    citation = {
        "kind": "lesson",
        "target_id": "lessons/git_checkout_recovery.md",
        "source": "lesson_v2.lesson_zero_citation_census",
        "retrieval_count": 0,
        "offered_or_shown": True,
    }
    assert validate_trainer_citation(citation) == []


def test_positive_retrieval_count_does_not_require_offered_flag():
    """A nonzero count is itself proof of exposure -- offered_or_shown is
    only load-bearing for the zero case."""
    citation = {**GOOD_LESSON_CITATION, "retrieval_count": 4}
    assert "offered_or_shown" not in citation
    assert validate_trainer_citation(citation) == []


def test_never_raises_on_pathological_input():
    class Weird:
        def get(self, *a, **k):
            raise RuntimeError("boom")

    assert validate_trainer_citation(Weird()) == ["citation is not a JSON object"]
