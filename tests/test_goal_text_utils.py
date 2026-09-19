"""Tests for nanobot.runtime.goal_text_utils.normalize_candidate_title (#1785)."""
from __future__ import annotations

from nanobot.runtime.goal_text_utils import normalize_candidate_title


def test_version_tag_is_stripped():
    assert (
        normalize_candidate_title("Filter fallback candidates dedup (V1)")
        == normalize_candidate_title("Filter fallback candidates dedup (V2)")
    )


def test_priority_prefix_is_stripped():
    assert (
        normalize_candidate_title("Filter fallback candidates dedup (V1)")
        == normalize_candidate_title("Priority 43 — Filter fallback candidates dedup (V1)")
    )


def test_case_and_whitespace_are_folded():
    assert (
        normalize_candidate_title("  Add   Fail-Fast Option  ")
        == normalize_candidate_title("add fail-fast option")
    )


def test_genuinely_different_titles_stay_different():
    assert normalize_candidate_title("Filter fallback candidates dedup (V1)") != normalize_candidate_title(
        "Filter fallback candidate paths (V1)"
    )


def test_trailing_colon_is_stripped():
    assert normalize_candidate_title("Check feed staleness:") == normalize_candidate_title("Check feed staleness")


def test_empty_and_none_are_empty_string():
    assert normalize_candidate_title("") == ""
    assert normalize_candidate_title(None) == ""  # type: ignore[arg-type]


def test_a_version_tag_mid_string_is_not_mistaken_for_a_priority_prefix():
    """The two patterns strip independently -- a title with only a version
    tag (no Priority-N- label) is untouched by the prefix pattern."""
    assert normalize_candidate_title("Ship the (V1) rollout plan") == "ship the rollout plan"


def test_real_corpus_pair_measured_2026_09_19():
    """Exact pair from the real 7-day ledger sample (118 of 342 rejects):
    a Priority-43 label minted on an otherwise byte-identical title."""
    a = normalize_candidate_title("Filter fallback candidates dedup (V1)")
    b = normalize_candidate_title("Priority 43 — Filter fallback candidates dedup (V1)")
    assert a == b == "filter fallback candidates dedup"
