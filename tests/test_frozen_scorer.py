"""Tests for #527: frozen scorer — pure, isolated reward computation.

Verifies score_cycle() is deterministic, pure, covers all outcome branches,
and that coordinator._derive_reward_signal() correctly integrates scorer_version.
"""
from __future__ import annotations

import pytest
from nanobot.runtime.scorer import (
    SCORER_VERSION,
    ScoringResult,
    beats_parent,
    score_cycle,
)


# ─── score_cycle determinism ──────────────────────────────────────────────────

def test_score_cycle_deterministic():
    """Same inputs → identical ScoringResult (ADR-072 reproducibility)."""
    fd = {"mode": "continue_active_lane"}
    r1 = score_cycle(fd=fd, budget={}, commits_pushed=1, result_status="completed")
    r2 = score_cycle(fd=fd, budget={}, commits_pushed=1, result_status="completed")
    assert r1 == r2


# ─── outcome branches ─────────────────────────────────────────────────────────

def test_commits_1_status_completed_value_gte_1():
    """1 commit + completed status → value ≥ 1.0 (keep)."""
    r = score_cycle(
        fd={"mode": "continue_active_lane"},
        budget={}, commits_pushed=1, result_status="completed",
    )
    assert r.value >= 1.0
    assert r.outcome == "keep"


def test_commits_0_status_completed_value_lte_08():
    """0 commits + completed → value ≤ 0.8 (no concrete change)."""
    r = score_cycle(
        fd={"mode": "continue_active_lane"},
        budget={}, commits_pushed=0, result_status="completed",
    )
    assert r.value <= 0.8


def test_commits_0_status_already_done_value_gte_1():
    """already_done is a valid, positive outcome even with 0 commits."""
    r = score_cycle(
        fd={"mode": "record_reward_after_synth"},
        budget={}, commits_pushed=0, result_status="already_done",
    )
    assert r.value >= 1.0
    assert r.outcome == "keep"


def test_commits_0_status_blocked_low_value():
    """blocked status → low value, likely discard."""
    r = score_cycle(
        fd={"mode": "record_reward"},
        budget={}, commits_pushed=0, result_status="blocked",
    )
    assert r.value < 1.0


def test_outcome_keep_when_value_gte_threshold():
    """Explicit: value ≥ 1.0 → outcome=keep."""
    r = score_cycle(
        fd={"mode": "continue_active_lane"},
        budget={}, commits_pushed=1, result_status="completed",
    )
    assert r.outcome == "keep"


def test_outcome_discard_when_value_lt_threshold():
    """0 commits + error → value < 0.6 → outcome=discard."""
    r = score_cycle(
        fd={"mode": "discard"},
        budget={}, commits_pushed=0, result_status="error",
    )
    assert r.outcome == "discard"


def test_revert_required_when_discard_and_commits():
    """discard + commits_pushed > 0 → revert_required=True."""
    r = score_cycle(
        fd={"mode": "discard"},
        budget={}, commits_pushed=1, result_status="error",
    )
    assert r.outcome == "discard"
    assert r.revert_required is True


def test_no_revert_when_keep():
    """keep outcome → revert_required=False."""
    r = score_cycle(
        fd={"mode": "continue_active_lane"},
        budget={}, commits_pushed=1, result_status="completed",
    )
    assert r.outcome == "keep"
    assert r.revert_required is False


# ─── scorer_version ───────────────────────────────────────────────────────────

def test_scorer_version_in_result():
    """ScoringResult must carry scorer_version == SCORER_VERSION."""
    r = score_cycle(fd={}, budget={}, commits_pushed=0, result_status="completed")
    assert r.scorer_version == SCORER_VERSION


def test_scorer_version_is_string():
    assert isinstance(SCORER_VERSION, str)
    assert len(SCORER_VERSION) > 0


# ─── beats_parent ─────────────────────────────────────────────────────────────

def test_beats_parent_true_when_above_delta():
    r = score_cycle(
        fd={"mode": "continue_active_lane"},
        budget={}, commits_pushed=1, result_status="completed",
    )
    assert beats_parent(r, parent_value=0.5) is True


def test_beats_parent_false_when_equal_or_below():
    r = score_cycle(fd={}, budget={}, commits_pushed=0, result_status="blocked")
    parent = r.value + 0.10  # parent is 0.10 higher
    assert beats_parent(r, parent_value=parent) is False


# ─── coordinator integration: scorer_version in _derive_reward_signal ─────────

def test_derive_reward_signal_includes_scorer_version():
    """_derive_reward_signal() result must include scorer_version field."""
    from nanobot.runtime.coordinator import _derive_reward_signal
    result = _derive_reward_signal(
        result_status="PASS",
        improvement_score=1.0,
        current_task_id="some-task",
    )
    assert "scorer_version" in result
    assert result["scorer_version"] == SCORER_VERSION


def test_derive_reward_signal_with_fd_includes_frozen_scorer():
    """When fd is provided, result includes frozen_scorer_value and frozen_scorer_outcome."""
    from nanobot.runtime.coordinator import _derive_reward_signal
    result = _derive_reward_signal(
        result_status="completed",
        improvement_score=None,
        fd={"mode": "continue_active_lane"},
        commits_pushed=1,
    )
    assert "frozen_scorer_value" in result
    assert "frozen_scorer_outcome" in result
    assert "frozen_scorer_rationale" in result
