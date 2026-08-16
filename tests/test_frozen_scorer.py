"""Tests for #527: frozen scorer — pure, isolated reward computation.

Verifies score_cycle() is deterministic, pure, covers all outcome branches,
and that coordinator._derive_reward_signal() correctly integrates scorer_version.
"""
from __future__ import annotations

import pytest
from nanobot.runtime.scorer import (
    SCORER_VERSION,
    ScoringResult,
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


# ── Priority 18: _load_weights and weights_path param ─────────────────────────

def test_load_weights_hardcoded_when_no_path():
    """`_load_weights(None)` returns hardcoded defaults."""
    from nanobot.runtime.scorer import _load_weights, WEIGHT_COMMITS, WEIGHT_MODE, WEIGHT_STATUS
    wc, wm, ws, source = _load_weights(None)
    assert wc == WEIGHT_COMMITS
    assert wm == WEIGHT_MODE
    assert ws == WEIGHT_STATUS
    assert source == 'hardcoded'


def test_load_weights_from_file(tmp_path):
    """Valid score_weights.json overrides defaults."""
    import json
    from nanobot.runtime.scorer import _load_weights
    f = tmp_path / 'score_weights.json'
    f.write_text(json.dumps({'WEIGHT_COMMITS': 0.50, 'WEIGHT_MODE': 0.25, 'WEIGHT_STATUS': 0.25}))
    wc, wm, ws, source = _load_weights(f)
    assert wc == pytest.approx(0.50)
    assert wm == pytest.approx(0.25)
    assert ws == pytest.approx(0.25)
    assert source == 'surfaces'


def test_load_weights_rejects_bad_sum(tmp_path):
    """Weights summing to 0.5 → hardcoded defaults returned."""
    import json
    from nanobot.runtime.scorer import _load_weights, WEIGHT_COMMITS, WEIGHT_MODE, WEIGHT_STATUS
    f = tmp_path / 'score_weights.json'
    f.write_text(json.dumps({'WEIGHT_COMMITS': 0.20, 'WEIGHT_MODE': 0.15, 'WEIGHT_STATUS': 0.15}))
    _, _, _, source = _load_weights(f)
    assert source == 'hardcoded'


def test_load_weights_rejects_out_of_range(tmp_path):
    """Weight of 1.5 → out of [0.01, 0.99] → hardcoded defaults."""
    import json
    from nanobot.runtime.scorer import _load_weights
    f = tmp_path / 'score_weights.json'
    f.write_text(json.dumps({'WEIGHT_COMMITS': 1.50, 'WEIGHT_MODE': -0.30, 'WEIGHT_STATUS': -0.20}))
    _, _, _, source = _load_weights(f)
    assert source == 'hardcoded'


def test_score_cycle_weights_source_field_hardcoded():
    """`score_cycle(..., weights_path=None).weights_source == 'hardcoded'`."""
    from nanobot.runtime.scorer import score_cycle
    result = score_cycle(
        fd={'mode': 'continue_active_lane'},
        budget={},
        commits_pushed=1,
        result_status='completed',
        weights_path=None,
    )
    assert result.weights_source == 'hardcoded'


def test_score_cycle_with_surfaces_weights(tmp_path):
    """Valid weights file → weights_source == 'surfaces', value differs from default."""
    import json
    from nanobot.runtime.scorer import score_cycle
    f = tmp_path / 'score_weights.json'
    # Commit weight = 0.80, much higher than default 0.40
    f.write_text(json.dumps({'WEIGHT_COMMITS': 0.80, 'WEIGHT_MODE': 0.10, 'WEIGHT_STATUS': 0.10}))
    result = score_cycle(
        fd={'mode': 'continue_active_lane'},
        budget={},
        commits_pushed=1,
        result_status='completed',
        weights_path=f,
    )
    assert result.weights_source == 'surfaces'
    # With WEIGHT_COMMITS=0.80: 0.80*1.0 + 0.10*1.0 + 0.10*1.0 = 1.0
    assert result.value == pytest.approx(1.0, abs=0.01)
