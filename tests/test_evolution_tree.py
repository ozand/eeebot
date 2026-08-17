"""Tests for #877: git-native evolutionary tree.

Covers the pure sidecar-bookkeeping module (nanobot/runtime/evolution_tree.py)
in isolation — no git repo needed here (that lives in tests/test_bridge_cycle_branch.py
for the bridge wiring). Population=branches / generation=commit / fitness=ledger
entry per node is exercised end-to-end at the bridge layer; this file pins the
tree.json read/write contract, node_score, select_switch_target, should_switch,
and tree_indexed_shas.
"""
from __future__ import annotations

import json

import pytest

from nanobot.runtime import evolution_tree as evo

# ─── read_tree / record_node round-trip ─────────────────────────────────────


class TestReadTreeDefaults:
    def test_missing_file_returns_empty_schema(self, tmp_path):
        tree = evo.read_tree(tmp_path)
        assert tree == {
            "schema_version": evo.SCHEMA_VERSION,
            "current_sha": None,
            "nodes": {},
            "switches": [],
        }

    def test_corrupt_json_fails_open_to_empty(self, tmp_path):
        path = tmp_path / "evolution" / "tree.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        tree = evo.read_tree(tmp_path)
        assert tree["nodes"] == {}
        assert tree["current_sha"] is None

    def test_non_dict_top_level_fails_open(self, tmp_path):
        path = tmp_path / "evolution" / "tree.json"
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        tree = evo.read_tree(tmp_path)
        assert tree["nodes"] == {}


class TestRecordNodeRoundTrip:
    def test_record_then_read_back(self, tmp_path):
        evo.record_node(
            tmp_path, sha="sha1", parent_sha=None, branch="selfevo/cycle-1",
            cycle_id="cycle-1", reward=0.9,
        )
        tree = evo.read_tree(tmp_path)
        assert tree["current_sha"] == "sha1"
        assert "sha1" in tree["nodes"]
        node = tree["nodes"]["sha1"]
        assert node["parent_sha"] is None
        assert node["branch"] == "selfevo/cycle-1"
        assert node["cycle_id"] == "cycle-1"
        assert node["fitness"]["reward"] == 0.9
        assert "ts" in node

    def test_record_node_appends_ledger_event(self, tmp_path):
        evo.record_node(
            tmp_path, sha="sha1", parent_sha=None, branch="b", cycle_id="c1",
        )
        ledger_path = tmp_path / "ledger" / "cycles.jsonl"
        assert ledger_path.exists()
        rows = [json.loads(ln) for ln in ledger_path.read_text().splitlines() if ln.strip()]
        matches = [r for r in rows if r.get("phase") == "evolution_tree" and r.get("reason") == "node_recorded"]
        assert len(matches) == 1
        assert matches[0]["sha"] == "sha1"

    def test_falsy_sha_is_a_noop(self, tmp_path):
        evo.record_node(tmp_path, sha="", parent_sha=None, branch="b", cycle_id="c1")
        assert not (tmp_path / "evolution" / "tree.json").exists()

    def test_current_sha_advances_across_generations(self, tmp_path):
        evo.record_node(tmp_path, sha="s1", parent_sha=None, branch="b1", cycle_id="c1")
        evo.record_node(tmp_path, sha="s2", parent_sha="s1", branch="b2", cycle_id="c2")
        tree = evo.read_tree(tmp_path)
        assert tree["current_sha"] == "s2"
        assert set(tree["nodes"].keys()) == {"s1", "s2"}

    def test_fitness_pulled_from_scorecard_latest(self, tmp_path):
        scorecard_dir = tmp_path / "scorecard"
        scorecard_dir.mkdir()
        (scorecard_dir / "latest.json").write_text(json.dumps({
            "loop": {
                "integrations": 5,
                "confirmed_integrations": 3,
                "repeat_failure_rate": 0.1,
            }
        }))
        evo.record_node(tmp_path, sha="s1", parent_sha=None, branch="b", cycle_id="c1", reward=0.8)
        node = evo.read_tree(tmp_path)["nodes"]["s1"]
        assert node["fitness"] == {
            "reward": 0.8,
            "integrations": 5,
            "confirmed_integrations": 3,
            "repeat_failure_rate": 0.1,
        }

    def test_missing_scorecard_fails_open_to_none_fields(self, tmp_path):
        evo.record_node(tmp_path, sha="s1", parent_sha=None, branch="b", cycle_id="c1")
        node = evo.read_tree(tmp_path)["nodes"]["s1"]
        assert node["fitness"] == {
            "reward": None,
            "integrations": None,
            "confirmed_integrations": None,
            "repeat_failure_rate": None,
        }


class TestRecordNodeCapEviction:
    def test_cap_evicts_lowest_score_oldest_never_current_or_recent_ancestors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(evo, "MAX_NODES", 3)
        monkeypatch.setattr(evo, "_KEEP_ANCESTOR_HOPS", 1)
        # Chain: s1 (low fitness, oldest) -> s2 (mid) -> s3 (immediate parent of
        # the eventual current tip) -> s4 (current).
        evo.record_node(tmp_path, sha="s1", parent_sha=None, branch="b1", cycle_id="c1", reward=0.0)
        evo.record_node(tmp_path, sha="s2", parent_sha="s1", branch="b2", cycle_id="c2", reward=0.5)
        evo.record_node(tmp_path, sha="s3", parent_sha="s2", branch="b3", cycle_id="c3", reward=0.9)
        assert set(evo.read_tree(tmp_path)["nodes"].keys()) == {"s1", "s2", "s3"}

        # Fourth node pushes the tree to 4 (over the cap of 3) -> exactly one
        # eviction happens: the lowest-score node among the evictable set
        # (s1, s2 — s3/s4 are protected as current + its 1-hop parent).
        evo.record_node(tmp_path, sha="s4", parent_sha="s3", branch="b4", cycle_id="c4", reward=0.7)
        tree = evo.read_tree(tmp_path)
        assert len(tree["nodes"]) == 3
        assert "s1" not in tree["nodes"]  # lowest score (0.0) evicted
        assert "s2" in tree["nodes"]      # only one eviction needed
        assert tree["current_sha"] == "s4"
        assert "s4" in tree["nodes"] and "s3" in tree["nodes"]

    def test_current_sha_itself_is_never_evicted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(evo, "MAX_NODES", 1)
        monkeypatch.setattr(evo, "_KEEP_ANCESTOR_HOPS", 0)  # isolate: only current itself is protected
        evo.record_node(tmp_path, sha="s1", parent_sha=None, branch="b1", cycle_id="c1", reward=5.0)
        evo.record_node(tmp_path, sha="s2", parent_sha="s1", branch="b2", cycle_id="c2", reward=0.0)
        tree = evo.read_tree(tmp_path)
        # s2 is current_sha, protected (and evicted-from-eligibility) even
        # though its own reward is far lower than the deleted s1's.
        assert tree["current_sha"] == "s2"
        assert "s2" in tree["nodes"]
        assert "s1" not in tree["nodes"]
        assert len(tree["nodes"]) == 1

    def test_ancestors_within_hop_window_protected_despite_low_score(self, tmp_path, monkeypatch):
        """An ancestor within _KEEP_ANCESTOR_HOPS survives eviction even when
        it scores lower than an unprotected node further back in history —
        proving the eviction picks by protection-window membership first,
        score only among the evictable remainder."""
        monkeypatch.setattr(evo, "MAX_NODES", 3)
        monkeypatch.setattr(evo, "_KEEP_ANCESTOR_HOPS", 2)
        # Chain: root -> p2 -> p1 -> cur (cur's parent is 1 hop away, p2 is 2
        # hops away — both within the window; root is 3 hops away, outside it).
        evo.record_node(tmp_path, sha="root", parent_sha=None, branch="br", cycle_id="c0", reward=10.0)
        evo.record_node(tmp_path, sha="p2", parent_sha="root", branch="b2", cycle_id="c1", reward=-5.0)
        evo.record_node(tmp_path, sha="p1", parent_sha="p2", branch="b1", cycle_id="c2", reward=-3.0)
        evo.record_node(tmp_path, sha="cur", parent_sha="p1", branch="bc", cycle_id="c3", reward=0.0)
        tree = evo.read_tree(tmp_path)
        # "root" had the HIGHEST raw reward of all four but is evicted anyway
        # because it falls outside the protection window; p1/p2 survive
        # purely via protection despite scoring far lower.
        assert set(tree["nodes"].keys()) == {"p2", "p1", "cur"}
        assert "root" not in tree["nodes"]


# ─── node_score ──────────────────────────────────────────────────────────────


class TestNodeScore:
    def test_basic_formula(self):
        node = {"fitness": {"reward": 0.5, "confirmed_integrations": 2, "repeat_failure_rate": 0.1}}
        assert evo.node_score(node) == pytest.approx(0.5 + 0.2 - 0.02)

    def test_missing_fields_default_to_zero(self):
        assert evo.node_score({"fitness": {}}) == 0.0
        assert evo.node_score({}) == 0.0

    def test_bad_shape_fails_open_to_zero(self):
        assert evo.node_score({"fitness": {"reward": "not-a-number"}}) == 0.0
        assert evo.node_score(None) == 0.0  # type: ignore[arg-type]


# ─── select_switch_target ───────────────────────────────────────────────────


class TestSelectSwitchTarget:
    def test_fewer_than_two_nodes_returns_none(self, tmp_path):
        assert evo.select_switch_target(tmp_path, None) is None
        evo.record_node(tmp_path, sha="s1", parent_sha=None, branch="b1", cycle_id="c1", reward=1.0)
        assert evo.select_switch_target(tmp_path, "s1") is None

    def test_best_by_score_excludes_current(self, tmp_path):
        evo.record_node(tmp_path, sha="low", parent_sha=None, branch="b-low", cycle_id="c1", reward=0.1)
        evo.record_node(tmp_path, sha="high", parent_sha=None, branch="b-high", cycle_id="c2", reward=0.9)
        evo.record_node(tmp_path, sha="current", parent_sha="high", branch="b-cur", cycle_id="c3", reward=0.05)
        result = evo.select_switch_target(tmp_path, "current")
        assert result == ("high", "b-high")

    def test_tie_breaks_newest_ts(self, tmp_path, monkeypatch):
        times = iter(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"])
        monkeypatch.setattr(evo, "_iso", lambda dt=None: next(times))
        evo.record_node(tmp_path, sha="a", parent_sha=None, branch="ba", cycle_id="c1", reward=0.5)
        evo.record_node(tmp_path, sha="b", parent_sha=None, branch="bb", cycle_id="c2", reward=0.5)
        evo.record_node(tmp_path, sha="cur", parent_sha=None, branch="bc", cycle_id="c3", reward=0.0)
        result = evo.select_switch_target(tmp_path, "cur")
        # a and b tie on score (0.5); b has the newer ts -> wins.
        assert result == ("b", "bb")

    def test_error_fails_open_to_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(evo, "read_tree", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert evo.select_switch_target(tmp_path, "x") is None

    def test_blocked_node_never_offered_even_if_best_score(self, tmp_path):
        """RED-1 fix: a node flagged blocked (poisoned base, discovered by
        the bridge's post-switch surface gate) is never re-selected, even
        though it would otherwise be the top-scoring candidate."""
        evo.record_node(tmp_path, sha="poisoned", parent_sha=None, branch="b-poison", cycle_id="c1", reward=0.99)
        evo.record_node(tmp_path, sha="ok", parent_sha=None, branch="b-ok", cycle_id="c2", reward=0.4)
        evo.record_node(tmp_path, sha="current", parent_sha="ok", branch="b-cur", cycle_id="c3", reward=0.05)
        evo.mark_switch_blocked(tmp_path, "poisoned", reason="switch_base_gate_blocked")

        result = evo.select_switch_target(tmp_path, "current")

        assert result == ("ok", "b-ok")

    def test_all_other_nodes_blocked_returns_none(self, tmp_path):
        evo.record_node(tmp_path, sha="poisoned", parent_sha=None, branch="b1", cycle_id="c1", reward=0.9)
        evo.record_node(tmp_path, sha="current", parent_sha="poisoned", branch="b2", cycle_id="c2")
        evo.mark_switch_blocked(tmp_path, "poisoned", reason="switch_base_gate_blocked")

        assert evo.select_switch_target(tmp_path, "current") is None

    def test_cooldown_skips_recent_switch_target(self, tmp_path):
        """YELLOW-1 fix: a sha that was the to_sha of a recent switch is not
        re-offered — dampens back-to-back thrash while stalled() stays True."""
        evo.record_node(tmp_path, sha="recent", parent_sha=None, branch="b-recent", cycle_id="c1", reward=0.9)
        evo.record_node(tmp_path, sha="second", parent_sha=None, branch="b-second", cycle_id="c2", reward=0.5)
        evo.record_node(tmp_path, sha="current", parent_sha="recent", branch="b-cur", cycle_id="c3", reward=0.05)
        evo.record_switch(tmp_path, from_sha="current", to_sha="recent", reason="stalled")

        result = evo.select_switch_target(tmp_path, "current")

        assert result == ("second", "b-second")

    def test_cooldown_expires_after_window(self, tmp_path):
        """Once a sha ages out of the last _SWITCH_COOLDOWN switches entries,
        it becomes selectable again."""
        evo.record_node(tmp_path, sha="recent", parent_sha=None, branch="b-recent", cycle_id="c1", reward=0.9)
        evo.record_node(tmp_path, sha="current", parent_sha="recent", branch="b-cur", cycle_id="c2")
        evo.record_switch(tmp_path, from_sha="x0", to_sha="recent", reason="stalled")
        # Push _SWITCH_COOLDOWN more unrelated switches so "recent" ages out.
        for i in range(evo._SWITCH_COOLDOWN):
            evo.record_switch(tmp_path, from_sha=f"x{i}", to_sha=f"other-{i}", reason="stalled")

        result = evo.select_switch_target(tmp_path, "current")

        assert result == ("recent", "b-recent")

    def test_cooldown_still_offers_a_different_candidate(self, tmp_path):
        """The cooldown only suppresses the specific recent target — a
        different good candidate is still offered immediately."""
        evo.record_node(tmp_path, sha="recent", parent_sha=None, branch="b-recent", cycle_id="c1", reward=0.9)
        evo.record_node(tmp_path, sha="other", parent_sha=None, branch="b-other", cycle_id="c2", reward=0.8)
        evo.record_node(tmp_path, sha="current", parent_sha="recent", branch="b-cur", cycle_id="c3", reward=0.05)
        evo.record_switch(tmp_path, from_sha="current", to_sha="recent", reason="stalled")

        result = evo.select_switch_target(tmp_path, "current")

        assert result == ("other", "b-other")


# ─── should_switch ───────────────────────────────────────────────────────────


class TestShouldSwitch:
    def test_not_stalled_returns_none_even_with_candidates(self, tmp_path):
        evo.record_node(tmp_path, sha="low", parent_sha=None, branch="b1", cycle_id="c1", reward=0.1)
        evo.record_node(tmp_path, sha="high", parent_sha=None, branch="b2", cycle_id="c2", reward=0.9)
        assert evo.should_switch(tmp_path, False, "low") is None

    def test_stalled_delegates_to_select_switch_target(self, tmp_path):
        evo.record_node(tmp_path, sha="low", parent_sha=None, branch="b1", cycle_id="c1", reward=0.1)
        evo.record_node(tmp_path, sha="high", parent_sha=None, branch="b2", cycle_id="c2", reward=0.9)
        assert evo.should_switch(tmp_path, True, "low") == ("high", "b2")

    def test_stalled_but_no_candidate_returns_none(self, tmp_path):
        evo.record_node(tmp_path, sha="only", parent_sha=None, branch="b1", cycle_id="c1")
        assert evo.should_switch(tmp_path, True, "only") is None


# ─── tree_indexed_shas / current_sha ─────────────────────────────────────────


class TestTreeIndexedShas:
    def test_empty_tree_returns_empty_set(self, tmp_path):
        assert evo.tree_indexed_shas(tmp_path) == set()

    def test_returns_all_node_shas(self, tmp_path):
        evo.record_node(tmp_path, sha="s1", parent_sha=None, branch="b1", cycle_id="c1")
        evo.record_node(tmp_path, sha="s2", parent_sha="s1", branch="b2", cycle_id="c2")
        assert evo.tree_indexed_shas(tmp_path) == {"s1", "s2"}

    def test_fails_open_on_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(evo, "read_tree", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert evo.tree_indexed_shas(tmp_path) == set()


class TestCurrentSha:
    def test_none_when_empty(self, tmp_path):
        assert evo.current_sha(tmp_path) is None

    def test_reflects_last_recorded_node(self, tmp_path):
        evo.record_node(tmp_path, sha="s1", parent_sha=None, branch="b1", cycle_id="c1")
        assert evo.current_sha(tmp_path) == "s1"


# ─── record_switch ────────────────────────────────────────────────────────────


class TestRecordSwitch:
    def test_appends_bounded_switch_record(self, tmp_path):
        evo.record_switch(tmp_path, from_sha="a", to_sha="b", reason="stalled")
        tree = evo.read_tree(tmp_path)
        assert len(tree["switches"]) == 1
        assert tree["switches"][0]["from_sha"] == "a"
        assert tree["switches"][0]["to_sha"] == "b"
        assert tree["switches"][0]["reason"] == "stalled"


# ─── mark_switch_blocked (RED-1 fix) ─────────────────────────────────────────


class TestMarkSwitchBlocked:
    def test_flags_existing_node(self, tmp_path):
        evo.record_node(tmp_path, sha="s1", parent_sha=None, branch="b1", cycle_id="c1")
        evo.mark_switch_blocked(tmp_path, "s1", reason="switch_base_gate_blocked")
        node = evo.read_tree(tmp_path)["nodes"]["s1"]
        assert node["blocked"] is True
        assert node["blocked_reason"] == "switch_base_gate_blocked"

    def test_noop_for_falsy_sha(self, tmp_path):
        evo.mark_switch_blocked(tmp_path, "", reason="x")
        assert evo.read_tree(tmp_path) == evo._empty_tree()

    def test_noop_when_sha_has_no_node(self, tmp_path):
        evo.record_node(tmp_path, sha="s1", parent_sha=None, branch="b1", cycle_id="c1")
        evo.mark_switch_blocked(tmp_path, "does-not-exist", reason="x")
        tree = evo.read_tree(tmp_path)
        assert "does-not-exist" not in tree["nodes"]
        assert "blocked" not in tree["nodes"]["s1"]

    def test_fails_open_on_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(evo, "read_tree", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
        evo.mark_switch_blocked(tmp_path, "s1", reason="x")  # must not raise

    def test_capped_at_max_switches_oldest_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(evo, "MAX_SWITCHES", 2)
        evo.record_switch(tmp_path, from_sha="a", to_sha="b", reason="stalled")
        evo.record_switch(tmp_path, from_sha="b", to_sha="c", reason="stalled")
        evo.record_switch(tmp_path, from_sha="c", to_sha="d", reason="stalled")
        switches = evo.read_tree(tmp_path)["switches"]
        assert len(switches) == 2
        assert [s["to_sha"] for s in switches] == ["c", "d"]
