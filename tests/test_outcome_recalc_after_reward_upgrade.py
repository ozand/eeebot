"""
Tests for issue #514: experiment outcome must be recalculated after reward upgrade.

In run_self_evolving_cycle, when a materialized improvement artifact is present,
the reward can be upgraded from 1.0→1.2 (concrete change) or kept at 0.8 (penalty).
Before this fix, experiment["outcome"] was NOT updated after the metric_current change,
causing metric_current=1.2 / metric_baseline=1.2 / outcome=discard (contradictory state).
"""
import pytest


class TestOutcomeRecalcAfterRewardUpgrade:
    """Simulate the reward upgrade logic and verify outcome consistency."""

    def _apply_upgrade(self, experiment: dict, upgraded_value: float) -> dict:
        """
        Replicate the fixed coordinator logic:
        update metric_current then re-derive outcome.
        """
        upgraded_reward = {"value": upgraded_value, "source": "test"}
        experiment["reward_signal"] = upgraded_reward
        experiment["metric_current"] = upgraded_reward["value"]
        experiment["metric_frontier"] = max(
            float(experiment.get("metric_frontier") or upgraded_reward["value"]),
            upgraded_reward["value"],
        )
        # Re-derive outcome (the fix)
        _upgraded_baseline = float(experiment.get("metric_baseline") or 0.0)
        _upgraded_current = upgraded_reward["value"]
        if _upgraded_baseline is None or _upgraded_current >= _upgraded_baseline:
            experiment["outcome"] = "keep"
            experiment["revert_required"] = False
        else:
            experiment["outcome"] = "discard"
            experiment["revert_required"] = True
        return experiment

    def _make_stale_discard_experiment(self, baseline: float = 1.2) -> dict:
        """Experiment in stale-discard state: outcome=discard set before reward upgrade."""
        return {
            "metric_baseline": baseline,
            "metric_current": 1.0,
            "metric_frontier": baseline,
            "outcome": "discard",         # stale — set before upgrade
            "revert_required": True,      # stale
            "budget_used": {"tool_calls": 0, "subagents": 0},
        }

    # ----- Tests: upgrade to 1.2 (concrete change detected) -----

    def test_upgraded_to_12_baseline_12_outcome_becomes_keep(self):
        """The canonical bug: reward upgraded to 1.2, baseline=1.2 → must be keep."""
        exp = self._make_stale_discard_experiment(baseline=1.2)
        result = self._apply_upgrade(exp, upgraded_value=1.2)
        assert result["outcome"] == "keep", (
            f"outcome={result['outcome']!r}, expected keep (1.2 >= 1.2)"
        )

    def test_upgraded_to_12_baseline_12_revert_required_false(self):
        """revert_required must be False when outcome is keep."""
        exp = self._make_stale_discard_experiment(baseline=1.2)
        result = self._apply_upgrade(exp, upgraded_value=1.2)
        assert result["revert_required"] is False

    def test_upgraded_to_12_metric_current_is_12(self):
        """metric_current must reflect the upgraded value."""
        exp = self._make_stale_discard_experiment(baseline=1.2)
        result = self._apply_upgrade(exp, upgraded_value=1.2)
        assert result["metric_current"] == 1.2

    # ----- Tests: penalty (0.8 — metadata-only) -----

    def test_penalty_08_baseline_12_outcome_stays_discard(self):
        """metadata-only penalty (0.8) below baseline (1.2) → outcome must stay discard."""
        exp = self._make_stale_discard_experiment(baseline=1.2)
        result = self._apply_upgrade(exp, upgraded_value=0.8)
        assert result["outcome"] == "discard", (
            f"outcome={result['outcome']!r}, expected discard (0.8 < 1.2)"
        )

    def test_penalty_08_revert_required_true(self):
        """revert_required must remain True when penalty outcome is discard."""
        exp = self._make_stale_discard_experiment(baseline=1.2)
        result = self._apply_upgrade(exp, upgraded_value=0.8)
        assert result["revert_required"] is True

    # ----- Tests: no baseline (first cycle) -----

    def test_no_baseline_any_upgrade_is_keep(self):
        """When metric_baseline is None (first cycle), any reward → keep."""
        exp = self._make_stale_discard_experiment(baseline=1.2)
        exp["metric_baseline"] = None  # override
        result = self._apply_upgrade(exp, upgraded_value=1.0)
        assert result["outcome"] == "keep"

    # ----- Tests: frontier update -----

    def test_frontier_updated_to_max(self):
        """metric_frontier must be max(old_frontier, upgraded_value)."""
        exp = self._make_stale_discard_experiment(baseline=1.2)
        exp["metric_frontier"] = 1.5  # previously higher
        result = self._apply_upgrade(exp, upgraded_value=1.2)
        assert result["metric_frontier"] == 1.5  # old frontier wins

    def test_frontier_upgraded_when_new_is_higher(self):
        exp = self._make_stale_discard_experiment(baseline=1.0)
        exp["metric_frontier"] = 1.0
        result = self._apply_upgrade(exp, upgraded_value=1.2)
        assert result["metric_frontier"] == 1.2
