"""Tests for #843: validity-before-score invariant audit.

Pins, per critical scoring/confirm path, that an EMPTY or INVALID input
never yields a passing/perfect metric AND never crashes:
  - scorer.score_cycle: null/empty feedback dict never scores "keep".
  - cycle_feedback._experiment_metric_summary: null reward_signal never
    crashes and falls through to metric_current == 0.0.
  - scorecard._ratio: a zero denominator never fabricates a ratio (the
    focused unit-pin of the confirmed_integration_ratio gate).
  - heldout: an invalid/empty check status is never treated as "pass".
"""
from __future__ import annotations

from nanobot.runtime import cycle_feedback, scorecard, scorer
from nanobot.runtime.heldout import _VALID_STATUSES, _check_one
from datetime import datetime, timezone


class TestScorerValidityBeforeScore:
    def test_none_feedback_dict_does_not_raise_and_never_keeps(self):
        result = scorer.score_cycle(
            fd=None, budget={}, commits_pushed=0, result_status=""
        )
        assert result.outcome != "keep"
        assert result.value < scorer.THRESHOLD_KEEP

    def test_empty_feedback_dict_never_keeps(self):
        result = scorer.score_cycle(
            fd={}, budget={}, commits_pushed=0, result_status=""
        )
        assert result.outcome != "keep"

    def test_empty_submission_cannot_claim_already_done_keep(self):
        """The already_done 1.0 bonus must not lift an empty/no-op submission
        (no feedback decision, no commits) to a passing 'keep' verdict (#843)."""
        result = scorer.score_cycle(
            fd={}, budget={}, commits_pushed=0, result_status="already_done"
        )
        assert result.outcome != "keep"
        assert result.value < scorer.THRESHOLD_KEEP


class TestExperimentMetricSummaryValidityBeforeScore:
    def test_none_reward_signal_does_not_raise_and_metric_is_zero(self):
        summary = cycle_feedback._experiment_metric_summary(
            result_status="", reward_signal=None, previous_experiment=None
        )
        assert summary["metric_current"] == 0.0


class TestScorecardRatioValidityBeforeScore:
    def test_zero_denominator_is_none_not_a_fabricated_ratio(self):
        assert scorecard._ratio(0, 0) is None
        assert scorecard._ratio(5, 0) is None


class TestHeldoutValidityBeforeScore:
    def test_valid_statuses_excludes_empty_string(self):
        assert "" not in _VALID_STATUSES

    def test_fabricated_empty_status_is_not_counted_as_pass(self):
        """A checker returning an invalid/empty status must be coerced to
        'skip', never silently accepted as a passing verdict (#843)."""
        def _checker(_ctx):
            return "", "no real verdict"

        entry = _check_one(
            "scripts/x.py", "print('x')\n", _checker, datetime.now(timezone.utc)
        )
        assert entry["status"] != "pass"
        assert entry["status"] == "skip"
