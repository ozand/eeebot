"""Tests for #879: tech-tree of improvement DIRECTIONS.

Covers the pure sidecar module (nanobot/runtime/tech_tree.py) in isolation
— seeding idempotency, marginal-gain sign/window/trust behavior, plateau
detection, epsilon-greedy selection with cooldown/reactivation, hypothesis
minting with domain-mapping/rate-limit/dedup, and portfolio visibility —
plus the three SOFT wiring points: scorecard's control-plane snapshot,
demand's goal-gap direction tag/boost, and goal_review's candidate-
ordering bias. Every LLM/randomness dependency is monkeypatched or
injected — no test reaches a provider or real ``random``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from nanobot.runtime import tech_tree

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _set_node(tmp_path, name: str, **overrides) -> None:
    portfolio = tech_tree.read_portfolio(tmp_path)
    portfolio["nodes"][name].update(overrides)
    tech_tree._write_portfolio(tmp_path, portfolio)


class _FakeRng:
    """Deterministic stand-in for the ``random`` module — tests inject this
    via ``select_current_direction(..., rng=...)`` instead of patching the
    stdlib module globally."""

    def __init__(self, random_value: float, choice_value=None):
        self._random_value = random_value
        self._choice_value = choice_value

    def random(self) -> float:
        return self._random_value

    def choice(self, seq):
        return self._choice_value if self._choice_value is not None else seq[0]


# ─── seeding ─────────────────────────────────────────────────────────────────


class TestEnsureSeeded:
    def test_creates_all_seed_nodes(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        portfolio = tech_tree.read_portfolio(tmp_path)
        assert set(portfolio["nodes"].keys()) == {s["name"] for s in tech_tree.SEED_NODES}
        for spec in tech_tree.SEED_NODES:
            node = portfolio["nodes"][spec["name"]]
            assert node["lever_metric"] == spec["lever_metric"]
            assert node["direction"] == spec["direction"]
            assert node["status"] == "active"
            assert node["gain_history"] == []
            assert node["minted_by"] == "product"
            assert node["last_lever_value"] is None
            assert node["cooldown_until_ts"] is None

    def test_idempotent_never_touches_existing_entry(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        _set_node(tmp_path, "proposer-quality", gain_history=[0.5], status="plateaued")

        tech_tree.ensure_seeded(tmp_path, now=NOW)
        reread = tech_tree.read_portfolio(tmp_path)
        assert reread["nodes"]["proposer-quality"]["gain_history"] == [0.5]
        assert reread["nodes"]["proposer-quality"]["status"] == "plateaued"

    def test_adds_missing_seed_node_without_deleting_instance_minted(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        portfolio = tech_tree.read_portfolio(tmp_path)
        del portfolio["nodes"]["compile-health"]
        portfolio["nodes"]["custom-domain"] = {
            "lever_metric": "loop.confirmed_integration_ratio",
            "direction": "higher",
            "gain_history": [],
            "status": "active",
            "cooldown_until_ts": None,
            "minted_by": "hypothesis",
            "created_ts": _iso(NOW),
            "last_lever_value": None,
        }
        tech_tree._write_portfolio(tmp_path, portfolio)

        tech_tree.ensure_seeded(tmp_path, now=NOW)
        reread = tech_tree.read_portfolio(tmp_path)
        assert "compile-health" in reread["nodes"]  # re-added
        assert "custom-domain" in reread["nodes"]  # never deleted


class TestReadPortfolioFailOpen:
    def test_missing_file_returns_empty_schema(self, tmp_path):
        portfolio = tech_tree.read_portfolio(tmp_path)
        assert portfolio == {
            "schema_version": tech_tree.SCHEMA_VERSION,
            "current": None,
            "nodes": {},
            "switches": [],
            "last_mint_ts": None,
        }

    def test_corrupt_json_fails_open_to_empty(self, tmp_path):
        path = tmp_path / "tech_tree" / "portfolio.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert tech_tree.read_portfolio(tmp_path)["nodes"] == {}

    def test_non_dict_top_level_fails_open(self, tmp_path):
        path = tmp_path / "tech_tree" / "portfolio.json"
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert tech_tree.read_portfolio(tmp_path)["nodes"] == {}


# ─── marginal gain ───────────────────────────────────────────────────────────


class TestRecordGains:
    def test_auto_seeds_when_called_first(self, tmp_path):
        tech_tree.record_gains(tmp_path, {"loop": {"repeat_failure_rate": 0.5}})
        portfolio = tech_tree.read_portfolio(tmp_path)
        assert "proposer-quality" in portfolio["nodes"]

    def test_first_observation_sets_baseline_no_gain(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        tech_tree.record_gains(tmp_path, {"loop": {"repeat_failure_rate": 0.4}})
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["proposer-quality"]
        assert node["gain_history"] == []
        assert node["last_lever_value"] == 0.4

    def test_lower_better_gain_sign(self, tmp_path):
        """proposer-quality (loop.repeat_failure_rate, lower-better)."""
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        tech_tree.record_gains(tmp_path, {"loop": {"repeat_failure_rate": 0.4}})
        tech_tree.record_gains(tmp_path, {"loop": {"repeat_failure_rate": 0.3}})  # improved
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["proposer-quality"]
        assert node["gain_history"] == [pytest.approx(0.1)]
        assert node["last_lever_value"] == 0.3

        tech_tree.record_gains(tmp_path, {"loop": {"repeat_failure_rate": 0.5}})  # worsened
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["proposer-quality"]
        assert node["gain_history"][-1] == pytest.approx(-0.2)

    def test_higher_better_gain_sign(self, tmp_path):
        """tool-reuse (loop.confirmed_integration_ratio, higher-better)."""
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        tech_tree.record_gains(tmp_path, {"loop": {"confirmed_integration_ratio": 0.5}})
        tech_tree.record_gains(tmp_path, {"loop": {"confirmed_integration_ratio": 0.7}})  # improved
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["tool-reuse"]
        assert node["gain_history"] == [pytest.approx(0.2)]

        tech_tree.record_gains(tmp_path, {"loop": {"confirmed_integration_ratio": 0.6}})  # worsened
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["tool-reuse"]
        assert node["gain_history"][-1] == pytest.approx(-0.1)

    def test_missing_or_non_numeric_metric_skipped(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        tech_tree.record_gains(tmp_path, {"loop": {"repeat_failure_rate": "not-a-number"}})
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["proposer-quality"]
        assert node["last_lever_value"] is None
        assert node["gain_history"] == []

        tech_tree.record_gains(tmp_path, {})  # section absent entirely
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["proposer-quality"]
        assert node["last_lever_value"] is None

    def test_window_bounded_to_max(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        value = 1.0
        for _ in range(tech_tree.GAIN_HISTORY_MAX + 3):
            value -= 0.01
            tech_tree.record_gains(tmp_path, {"loop": {"repeat_failure_rate": value}})
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["proposer-quality"]
        assert len(node["gain_history"]) == tech_tree.GAIN_HISTORY_MAX

    def test_forged_gain_history_not_trusted_next_gain_is_honest(self, tmp_path):
        """#879 trust note: even with a forged sidecar (bypassing this
        module's own API entirely), the NEXT record_gains call only ever
        appends a value it computes from the node's own last_lever_value +
        the REAL scorecard result it is handed — never a "claimed" gain."""
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        _set_node(tmp_path, "proposer-quality", last_lever_value=0.5, gain_history=[999.0])

        tech_tree.record_gains(tmp_path, {"loop": {"repeat_failure_rate": 0.3}})
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["proposer-quality"]
        assert node["gain_history"] == [999.0, pytest.approx(0.2)]
        assert node["last_lever_value"] == 0.3


class TestNodeMeanGain:
    def test_empty_history_is_zero(self):
        assert tech_tree.node_mean_gain({"gain_history": []}) == 0.0
        assert tech_tree.node_mean_gain({}) == 0.0

    def test_mean_of_history(self):
        assert tech_tree.node_mean_gain({"gain_history": [0.1, 0.3, -0.2]}) == pytest.approx(0.0666667)


class TestIsPlateaued:
    def test_false_when_window_not_full(self):
        node = {"gain_history": [0.0] * (tech_tree.GAIN_HISTORY_MAX - 1)}
        assert tech_tree.is_plateaued(node) is False

    def test_true_at_floor_with_full_window(self):
        node = {"gain_history": [0.0] * tech_tree.GAIN_HISTORY_MAX}
        assert tech_tree.is_plateaued(node) is True

    def test_false_above_floor(self):
        node = {"gain_history": [0.01] * tech_tree.GAIN_HISTORY_MAX}
        assert tech_tree.is_plateaued(node) is False

    def test_true_below_floor(self):
        node = {"gain_history": [-0.05] * tech_tree.GAIN_HISTORY_MAX}
        assert tech_tree.is_plateaued(node) is True

    def test_custom_floor_argument(self):
        node = {"gain_history": [0.05] * tech_tree.GAIN_HISTORY_MAX}
        assert tech_tree.is_plateaued(node, floor=0.1) is True
        assert tech_tree.is_plateaued(node, floor=0.01) is False


# ─── selection (epsilon-greedy) ──────────────────────────────────────────────


class TestSelectCurrentDirection:
    def test_exploit_picks_highest_mean_gain(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        _set_node(tmp_path, "compile-health", gain_history=[0.5, 0.5])
        _set_node(tmp_path, "cycle-cost", gain_history=[0.1, 0.1])
        picked = tech_tree.select_current_direction(tmp_path, now=NOW, rng=_FakeRng(1.0))
        assert picked == "compile-health"

    def test_exploit_tie_break_by_fewest_attempts(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        _set_node(tmp_path, "compile-health", gain_history=[0.5, 0.5, 0.5])  # mean 0.5, 3 attempts
        _set_node(tmp_path, "cycle-cost", gain_history=[0.5])  # mean 0.5, 1 attempt -> wins tie
        picked = tech_tree.select_current_direction(tmp_path, now=NOW, rng=_FakeRng(1.0))
        assert picked == "cycle-cost"

    def test_explore_takes_forced_choice_over_exploit_winner(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        _set_node(tmp_path, "compile-health", gain_history=[0.9, 0.9])  # clear exploit winner
        picked = tech_tree.select_current_direction(
            tmp_path, epsilon=0.15, now=NOW,
            rng=_FakeRng(random_value=0.0, choice_value="heldout-robustness"),
        )
        assert picked == "heldout-robustness"  # NOT the exploit winner -> explore path taken

    def test_at_or_above_epsilon_never_explores(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        _set_node(tmp_path, "compile-health", gain_history=[0.9, 0.9])
        picked = tech_tree.select_current_direction(
            tmp_path, epsilon=0.15, now=NOW,
            rng=_FakeRng(random_value=0.15, choice_value="heldout-robustness"),
        )
        assert picked == "compile-health"  # 0.15 is NOT < epsilon -> exploit

    def test_plateaued_and_cooldown_nodes_excluded(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        _set_node(
            tmp_path, "compile-health",
            gain_history=[0.9] * tech_tree.GAIN_HISTORY_MAX,
            status="plateaued",
            cooldown_until_ts=_iso(NOW + timedelta(hours=1)),
        )
        picked = tech_tree.select_current_direction(tmp_path, now=NOW, rng=_FakeRng(1.0))
        assert picked != "compile-health"

    def test_previously_current_node_plateaus_and_switch_recorded(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        portfolio = tech_tree.read_portfolio(tmp_path)
        portfolio["current"] = "proposer-quality"
        portfolio["nodes"]["proposer-quality"]["gain_history"] = [0.0] * tech_tree.GAIN_HISTORY_MAX
        tech_tree._write_portfolio(tmp_path, portfolio)

        picked = tech_tree.select_current_direction(tmp_path, now=NOW, rng=_FakeRng(1.0))
        assert picked != "proposer-quality"

        updated = tech_tree.read_portfolio(tmp_path)
        node = updated["nodes"]["proposer-quality"]
        assert node["status"] == "plateaued"
        assert node["cooldown_until_ts"] is not None
        assert len(updated["switches"]) == 1
        assert updated["switches"][0] == {
            "ts": _iso(NOW), "from": "proposer-quality", "to": picked,
            "reason": "plateau_switch", "floor": tech_tree.PLATEAU_FLOOR,
        }

        ledger_path = tmp_path / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
        matches = [r for r in rows if r.get("phase") == "tech_tree" and r.get("reason") == "plateau_switch"]
        assert len(matches) == 1
        assert matches[0]["from"] == "proposer-quality"
        assert matches[0]["to"] == picked

    def test_cooldown_expiry_reactivates_node(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        _set_node(
            tmp_path, "compile-health",
            status="plateaued", cooldown_until_ts=_iso(NOW - timedelta(hours=1)),
        )
        tech_tree.select_current_direction(tmp_path, now=NOW, rng=_FakeRng(1.0))
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["compile-health"]
        assert node["status"] == "active"
        assert node["cooldown_until_ts"] is None

    def test_cooldown_not_yet_expired_stays_plateaued(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        _set_node(
            tmp_path, "compile-health",
            status="plateaued", cooldown_until_ts=_iso(NOW + timedelta(hours=1)),
        )
        tech_tree.select_current_direction(tmp_path, now=NOW, rng=_FakeRng(1.0))
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["compile-health"]
        assert node["status"] == "plateaued"


# ─── minting from a supported hypothesis ────────────────────────────────────


class TestMaybeMintNode:
    def test_nothing_supported_no_mint(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        assert tech_tree.maybe_mint_node(tmp_path, []) is None
        assert tech_tree.maybe_mint_node(tmp_path, None) is None

    def test_unmapped_hypothesis_mints_new_node(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        supported = [{"title": "Totally novel widget caching subsystem", "evidence": {}}]
        minted = tech_tree.maybe_mint_node(tmp_path, supported)
        assert minted is not None
        portfolio = tech_tree.read_portfolio(tmp_path)
        node = portfolio["nodes"][minted]
        assert node["minted_by"] == "hypothesis"
        assert node["lever_metric"] == tech_tree.DEFAULT_MINT_LEVER
        assert node["direction"] == "higher"
        assert node["status"] == "active"
        assert portfolio["last_mint_ts"]

    def test_mapped_hypothesis_does_not_mint_duplicate(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        supported = [{"title": "Reduce proposer repeat failure rate churn", "evidence": {}}]
        minted = tech_tree.maybe_mint_node(tmp_path, supported)
        assert minted is None
        portfolio = tech_tree.read_portfolio(tmp_path)
        assert set(portfolio["nodes"].keys()) == {s["name"] for s in tech_tree.SEED_NODES}

    def test_mapped_hypothesis_reactivates_plateaued_node(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        _set_node(
            tmp_path, "proposer-quality",
            status="plateaued", cooldown_until_ts=_iso(NOW + timedelta(hours=10)),
        )
        supported = [{"title": "Reduce proposer repeat failure rate churn", "evidence": {}}]
        minted = tech_tree.maybe_mint_node(tmp_path, supported)
        assert minted is None
        node = tech_tree.read_portfolio(tmp_path)["nodes"]["proposer-quality"]
        assert node["status"] == "active"
        assert node["cooldown_until_ts"] is None

    def test_rate_limited_second_mint_within_window(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        first = tech_tree.maybe_mint_node(tmp_path, [{"title": "Alpha novel domain one", "evidence": {}}])
        assert first is not None
        second = tech_tree.maybe_mint_node(tmp_path, [{"title": "Beta novel domain two", "evidence": {}}])
        assert second is None
        # Only the first mint's node exists — the rate-limited attempt left
        # nothing behind.
        portfolio = tech_tree.read_portfolio(tmp_path)
        assert len(portfolio["nodes"]) == len(tech_tree.SEED_NODES) + 1

    def test_mint_allowed_again_after_window_elapses(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        portfolio = tech_tree.read_portfolio(tmp_path)
        portfolio["last_mint_ts"] = _iso(NOW - timedelta(hours=tech_tree.MINT_MIN_INTERVAL_HOURS + 1))
        tech_tree._write_portfolio(tmp_path, portfolio)

        minted = tech_tree.maybe_mint_node(tmp_path, [{"title": "Gamma novel domain three", "evidence": {}}])
        assert minted is not None

    def test_name_dedup_on_collision(self, tmp_path, monkeypatch):
        """Directly exercises the slug-collision suffix path: force every
        hypothesis to read as domain-UNMAPPED (monkeypatching the matcher)
        so a pre-existing node occupying the exact target slug forces the
        "-2" fallback, independent of the (structurally hard to
        decouple-from-slug) token-overlap check."""
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        portfolio = tech_tree.read_portfolio(tmp_path)
        portfolio["nodes"]["widget-cache-thing"] = {
            "lever_metric": tech_tree.DEFAULT_MINT_LEVER, "direction": "higher",
            "gain_history": [], "status": "active", "cooldown_until_ts": None,
            "minted_by": "hypothesis", "created_ts": _iso(NOW), "last_lever_value": None,
        }
        tech_tree._write_portfolio(tmp_path, portfolio)
        monkeypatch.setattr(tech_tree, "_match_existing_node", lambda *a, **k: None)

        minted = tech_tree.maybe_mint_node(tmp_path, [{"title": "Widget cache thing", "evidence": {}}])
        assert minted == "widget-cache-thing-2"

    def test_known_metric_hint_used_for_lever(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        minted = tech_tree.maybe_mint_node(
            tmp_path,
            [{"title": "Totally new domain area", "evidence": {"metric": "heldout.heldout_gap"}}],
        )
        node = tech_tree.read_portfolio(tmp_path)["nodes"][minted]
        assert node["lever_metric"] == "heldout.heldout_gap"
        assert node["direction"] == "lower"

    def test_unknown_metric_hint_falls_back_to_default(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        minted = tech_tree.maybe_mint_node(
            tmp_path,
            [{"title": "Totally new domain area", "evidence": {"metric": "made.up.metric"}}],
        )
        node = tech_tree.read_portfolio(tmp_path)["nodes"][minted]
        assert node["lever_metric"] == tech_tree.DEFAULT_MINT_LEVER

    def test_malformed_entries_skipped_fail_open(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        assert tech_tree.maybe_mint_node(tmp_path, ["not-a-dict", {"title": ""}, {}]) is None


# ─── portfolio visibility / read-only accessors ─────────────────────────────


class TestPortfolioSnapshot:
    def test_shape(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        snap = tech_tree.portfolio_snapshot(tmp_path)
        assert set(snap.keys()) == {"current", "nodes", "switches"}
        assert set(snap["nodes"].keys()) == {s["name"] for s in tech_tree.SEED_NODES}
        for node in snap["nodes"].values():
            assert set(node.keys()) == {"status", "mean_gain", "attempts", "lever_metric"}
            assert node["status"] == "active"
            assert node["mean_gain"] == 0.0
            assert node["attempts"] == 0

    def test_fail_open_on_corrupt_sidecar(self, tmp_path):
        path = tmp_path / "tech_tree" / "portfolio.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert tech_tree.portfolio_snapshot(tmp_path) == {"current": None, "nodes": {}, "switches": 0}


class TestCurrentDirection:
    def test_none_when_absent(self, tmp_path):
        assert tech_tree.current_direction(tmp_path) is None

    def test_reads_set_value(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        portfolio = tech_tree.read_portfolio(tmp_path)
        portfolio["current"] = "cycle-cost"
        tech_tree._write_portfolio(tmp_path, portfolio)
        assert tech_tree.current_direction(tmp_path) == "cycle-cost"


class TestDirectionForMetric:
    def test_exact_tail_match(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        assert tech_tree.direction_for_metric(tmp_path, "repeat_failure_rate") == "proposer-quality"
        assert tech_tree.direction_for_metric(tmp_path, "compile_clean_ratio") == "compile-health"

    def test_no_match_returns_none(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        assert tech_tree.direction_for_metric(tmp_path, "totally_unknown_metric") is None


class TestMatchesDirection:
    def test_token_overlap_true(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        assert tech_tree.matches_direction(
            "Cut the proposer repeat failure rate", tmp_path, "proposer-quality"
        )

    def test_no_overlap_false(self, tmp_path):
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        assert not tech_tree.matches_direction("Update the readme wording", tmp_path, "proposer-quality")

    def test_generic_section_word_never_matches(self, tmp_path):
        """'loop' is the SECTION prefix shared by several levers — it must
        never register as a domain match on its own (too generic a word)."""
        tech_tree.ensure_seeded(tmp_path, now=NOW)
        assert not tech_tree.matches_direction(
            "the self-evolving loop keeps running", tmp_path, "proposer-quality"
        )

    def test_unknown_direction_name_false(self, tmp_path):
        assert not tech_tree.matches_direction("anything", tmp_path, "no-such-node")


class TestDenySetAndFitnessSidecar:
    def test_tech_tree_module_is_deny_set(self):
        from nanobot.runtime import runtime_deny

        assert runtime_deny._is_runtime_deny("nanobot/runtime/tech_tree.py")

    def test_fitness_sidecar_membership(self):
        from nanobot.runtime import scorecard

        assert "tech_tree/portfolio.json" in scorecard.FITNESS_SIDECARS


# ─── wiring: scorecard control-plane ─────────────────────────────────────────


class TestScorecardWiring:
    def test_control_plane_tech_tree_present(self, tmp_path):
        from nanobot.runtime import scorecard

        snap = scorecard.compute_scorecard(tmp_path, None, force=True)
        tt = snap["control_plane"]["tech_tree"]
        assert set(tt["nodes"].keys()) == {s["name"] for s in tech_tree.SEED_NODES}
        assert tt["current"] in tt["nodes"]

    def test_tech_tree_exception_never_breaks_scorecard(self, tmp_path, monkeypatch):
        from nanobot.runtime import scorecard

        def _boom(*args, **kwargs):
            raise RuntimeError("tech_tree bug")

        monkeypatch.setattr(tech_tree, "record_gains", _boom)
        snap = scorecard.compute_scorecard(tmp_path, None, force=True)
        assert snap["schema_version"] == scorecard.SCORECARD_SCHEMA
        assert isinstance(snap["loop"], dict)  # sections computed before the
        # tech_tree block are NOT discarded by its failure


# ─── wiring: demand goal-gap direction tag/boost ────────────────────────────


class TestDemandWiring:
    def test_goal_gap_direction_boost_does_not_drop_items(self, tmp_path):
        from nanobot.runtime import demand

        state_dir = tmp_path / "state"
        (state_dir / "goals").mkdir(parents=True)
        tech_tree.ensure_seeded(state_dir, now=NOW)
        portfolio = tech_tree.read_portfolio(state_dir)
        portfolio["current"] = "compile-health"
        tech_tree._write_portfolio(state_dir, portfolio)

        # #765: scorecard.goal_gaps -> compute_scorecard has its own
        # 30-min watermark and calls datetime.now() internally (no `now=`
        # plumbed through demand.collect_demand) — computed_at_utc must be
        # close to REAL wall-clock time for the watermark short-circuit to
        # return this injected snapshot as-is, unlike the fixed NOW used
        # elsewhere in this file for the pure tech_tree unit tests.
        scorecard_dir = state_dir / "scorecard"
        scorecard_dir.mkdir(parents=True)
        real_now = datetime.now(timezone.utc)
        (scorecard_dir / "latest.json").write_text(
            json.dumps({
                "schema_version": "scorecard-v1",
                "computed_at_utc": _iso(real_now - timedelta(minutes=1)),
                "window_days": 7,
                "loop": {}, "cost": {}, "quality": {}, "value": {},
                "gaps": [
                    {"metric": "repeat_failure_rate", "vector": "V1", "current": 0.6, "target": 0.3, "evidence": "e1"},
                    {"metric": "compile_clean_ratio", "vector": "V1", "current": 0.5, "target": 0.95, "evidence": "e2"},
                ],
            }),
            encoding="utf-8",
        )

        items = demand.collect_demand(state_dir, None)
        gap_items = [i for i in items if i["kind"] == "goal-gap"]
        assert len(gap_items) == 2  # nothing dropped
        summaries = {i["summary"] for i in gap_items}
        assert "goal gap: repeat_failure_rate (V1)" in summaries
        assert "goal gap: compile_clean_ratio (V1)" in summaries
        # the compile-health-tagged item (the current direction) leads
        # within this V1-only batch.
        assert gap_items[0]["direction"] == "compile-health"
        assert gap_items[0]["summary"] == "goal gap: compile_clean_ratio (V1)"

    def test_no_current_direction_preserves_original_order(self, tmp_path):
        from nanobot.runtime import demand

        state_dir = tmp_path / "state"
        (state_dir / "goals").mkdir(parents=True)
        # No tech_tree state at all -> current_direction() is None.
        scorecard_dir = state_dir / "scorecard"
        scorecard_dir.mkdir(parents=True)
        real_now = datetime.now(timezone.utc)
        (scorecard_dir / "latest.json").write_text(
            json.dumps({
                "schema_version": "scorecard-v1",
                "computed_at_utc": _iso(real_now - timedelta(minutes=1)),
                "window_days": 7,
                "loop": {}, "cost": {}, "quality": {}, "value": {},
                "gaps": [
                    {"metric": "repeat_failure_rate", "vector": "V1", "current": 0.6, "target": 0.3, "evidence": "e1"},
                    {"metric": "confirmed_ratio", "vector": "V2", "current": 0.1, "target": 0.5, "evidence": "e2"},
                ],
            }),
            encoding="utf-8",
        )
        items = demand.collect_demand(state_dir, None)
        gap_items = [i for i in items if i["kind"] == "goal-gap"]
        assert len(gap_items) == 2
        assert "(V1)" in gap_items[0]["summary"]
        assert "(V2)" in gap_items[1]["summary"]


# ─── wiring: goal_review candidate-ordering bias ────────────────────────────


GOAL_TEXT = (
    "eeebot purpose. Vector 1 (PRIMARY) — Self-Improvement of the Agent "
    "System.\n\nVector 2 (SECONDARY) — Operator Interface.\n\n"
    "Current priority targets:\n"
    "(A) Priority 5 — Existing entry: do the existing thing. Commit.\n"
)

GAP = {
    "metric": "repeat_failure_rate",
    "vector": "V1",
    "current": 0.5,
    "target": 0.3,
    "evidence": "repeat_failure_rate=0.5 is above max target 0.3 over the last 7d window (goal vector V1)",
}


def _write_goal_text(state_dir, text: str = GOAL_TEXT) -> None:
    path = state_dir / "goals" / "goal_text.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"text": text}), encoding="utf-8")


def _write_snapshot(state_dir, gaps: list[dict]) -> None:
    path = state_dir / "scorecard" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": "scorecard-v1",
            "window_days": 7,
            "loop": {"integrations": 1, "repeat_failure_rate": 0.5},
            "gaps": gaps,
        }),
        encoding="utf-8",
    )


ALIGNED_PRIORITY = {
    "label": "Cut proposer repeat failure rate",
    "body": "Add a guard reducing proposer repeat failure rate in scripts/x.py. Commit.",
    "vector": "V1",
    "evidence": "E1",
}


class TestGoalReviewWiring:
    def test_direction_aligned_candidate_wins_capped_slot(self, tmp_path, monkeypatch):
        from nanobot.runtime import goal_review

        state_dir = tmp_path / "state"
        monkeypatch.setenv(goal_review.ENABLED_ENV, "1")
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        tech_tree.ensure_seeded(state_dir, now=NOW)
        portfolio = tech_tree.read_portfolio(state_dir)
        portfolio["current"] = "proposer-quality"
        tech_tree._write_portfolio(state_dir, portfolio)

        unrelated = [
            {
                "label": f"Bounded dashboard tweak {i}",
                "body": "Add a widget to scripts/dashboard_widget.py showing a counter. Commit.",
                "vector": "V1",
                "evidence": "E1",
            }
            for i in range(3)
        ]
        # The aligned candidate is LAST in the raw LLM order — the
        # _MAX_PRIORITIES=3 cap would normally drop it; the direction bias
        # must reorder it to the front instead.
        monkeypatch.setattr(
            goal_review, "_call_llm", lambda ctx: {"priorities": unrelated + [ALIGNED_PRIORITY]}
        )

        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        assert len(titles) == 3
        assert any("Cut proposer repeat failure rate" in t for t in titles)
        derived = goal_review.read_derived_priorities(state_dir)
        aligned_entry = next(d for d in derived if d["label"] == "Cut proposer repeat failure rate")
        assert aligned_entry.get("direction") == "proposer-quality"

    def test_non_aligned_not_starved_when_cap_not_hit(self, tmp_path, monkeypatch):
        from nanobot.runtime import goal_review

        state_dir = tmp_path / "state"
        monkeypatch.setenv(goal_review.ENABLED_ENV, "1")
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        tech_tree.ensure_seeded(state_dir, now=NOW)
        portfolio = tech_tree.read_portfolio(state_dir)
        portfolio["current"] = "proposer-quality"
        tech_tree._write_portfolio(state_dir, portfolio)

        unrelated = {
            "label": "Dashboard usage ping",
            "body": "Add a function to scripts/dashboard_widget.py logging a usage timestamp. Commit.",
            "vector": "V1",
            "evidence": "E1",
        }
        monkeypatch.setattr(
            goal_review, "_call_llm", lambda ctx: {"priorities": [unrelated, ALIGNED_PRIORITY]}
        )

        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        assert len(titles) == 2
        derived = goal_review.read_derived_priorities(state_dir)
        assert {d["label"] for d in derived} == {
            "Cut proposer repeat failure rate", "Dashboard usage ping",
        }
        unrelated_entry = next(d for d in derived if d["label"] == "Dashboard usage ping")
        assert not unrelated_entry.get("direction")  # never tagged — didn't match

    def test_no_current_direction_no_reordering_no_tagging(self, tmp_path, monkeypatch):
        """With no tech-tree state at all, behavior is byte-identical to
        pre-#879: original candidate order, no direction key added."""
        from nanobot.runtime import goal_review

        state_dir = tmp_path / "state"
        monkeypatch.setenv(goal_review.ENABLED_ENV, "1")
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: {"priorities": [ALIGNED_PRIORITY]})

        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        assert titles == ["Priority 6 — Cut proposer repeat failure rate"]
        derived = goal_review.read_derived_priorities(state_dir)
        assert "direction" not in derived[0]
