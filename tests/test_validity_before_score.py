"""Tests for #843: validity-before-score invariant audit.

Pins, per critical scoring/confirm path, that an EMPTY or INVALID input
never yields a passing/perfect metric AND never crashes:
  - scorecard._ratio: a zero denominator never fabricates a ratio (the
    focused unit-pin of the confirmed_integration_ratio gate).
  - heldout: an invalid/empty check status is never treated as "pass".

(The scorer.score_cycle and cycle_feedback._experiment_metric_summary pins
this file used to also cover were removed with the coordinator module web
(#916) — both modules are deleted.)
"""
from __future__ import annotations

from nanobot.runtime import scorecard
from nanobot.runtime.heldout import _VALID_STATUSES, _check_one
from datetime import datetime, timezone


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
