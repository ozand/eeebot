"""#1914: Shadow ranking telemetry tests.

Verify that on every demand rotation choice, shadow ranking scores the same
candidate set, records both choices and counterfactual components to
ranking_shadow/choices.jsonl, and never fails or alters rotation.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from nanobot.runtime import demand_ranking, llm_proposer
from nanobot.runtime.artifact_graph import ArtifactGraph, Node


def _make_demand_item(
    item_id: str,
    kind: str = "priority",
    summary: str = "task",
    affected_path: str = "",
    provenance: str = "",
    vector: str = "",
) -> dict:
    return {
        "id": item_id,
        "kind": kind,
        "summary": summary,
        "evidence": "ev",
        "affected_path": affected_path,
        "provenance": provenance,
        "vector": vector,
    }


def test_shadow_ranking_records_choice_and_candidates(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    items = [
        _make_demand_item("priority-1", "priority", "Priority 1 — Fix memory leak", "scripts/leaf.py"),
        _make_demand_item("defect-2", "defect", "fix: defect crash in reader", "scripts/reader.py"),
    ]

    selected = llm_proposer._select_assigned_demand(state_dir, items)
    assert selected == [items[0]]

    shadow_file = demand_ranking.shadow_choices_path(state_dir)
    assert shadow_file.is_file()

    rows = demand_ranking.read_shadow_choices(state_dir)
    assert len(rows) == 1
    row = rows[0]

    assert row["rotation_choice"] == "priority-1"
    assert "ranking_choice" in row
    assert isinstance(row["coincide"], bool)
    assert row["rotation_item"]["id"] == "priority-1"
    assert row["rotation_item"]["kind"] == "priority"
    assert row["ranking_item"] is not None

    candidates = row["candidates"]
    assert len(candidates) == 2
    c_ids = {c["id"] for c in candidates}
    assert c_ids == {"priority-1", "defect-2"}

    for c in candidates:
        assert "id" in c
        assert "kind" in c
        assert "summary" in c
        assert "ranking_score" in c
        assert "score_components" in c
        sc = c["score_components"]
        assert "value_tier" in sc
        assert "value_score" in sc
        assert "urgency_score" in sc
        assert "size_cost" in sc

        assert "size_term_components" in c
        st = c["size_term_components"]
        assert "shape" in st
        assert "cost" in st
        assert "estimated" in st


def test_shadow_ranking_coincidence_flag_true_and_false(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Candidate A connects a leaf (high score), Candidate B connects nothing
    graph = ArtifactGraph(
        status="complete",
        generated_at="2026-09-20T00:00:00Z",
        nodes={
            "scripts/leaf": Node(id="scripts/leaf", type="script", path="scripts/leaf.py"),
        },
        edges=[],
    )
    with patch("nanobot.runtime.artifact_graph.read_latest_artifact_graph", return_value=graph):
        item_leaf = _make_demand_item("item-leaf", "priority", "connect leaf", "scripts/leaf.py")
        item_other = _make_demand_item("item-other", "priority", "other task", "scripts/unknown.py")

        # 1. Rotation picks item_leaf (unserved first in list) -> coincide is True
        res1 = llm_proposer._select_assigned_demand(state_dir, [item_leaf, item_other])
        assert res1 == [item_leaf]

        rows = demand_ranking.read_shadow_choices(state_dir)
        assert len(rows) == 1
        assert rows[0]["rotation_choice"] == "item-leaf"
        assert rows[0]["ranking_choice"] == "item-leaf"
        assert rows[0]["coincide"] is True

        # 2. Next cycle: item_leaf was served, item_other is unserved!
        # Rotation picks item_other (unserved), but ranking still favors item_leaf -> coincide is False
        res2 = llm_proposer._select_assigned_demand(state_dir, [item_leaf, item_other])
        assert res2 == [item_other]

        rows = demand_ranking.read_shadow_choices(state_dir)
        assert len(rows) == 2
        assert rows[1]["rotation_choice"] == "item-other"
        assert rows[1]["ranking_choice"] == "item-leaf"
        assert rows[1]["coincide"] is False


def test_shadow_ranking_error_never_breaks_rotation_mandatory(tmp_path: Path, monkeypatch):
    """AC 3 (mandatory): an error in the shadow is logged and the cycle continues;
    it never fails or alters the choice.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    items = [
        _make_demand_item("priority-1", "priority", "task 1"),
        _make_demand_item("priority-2", "priority", "task 2"),
    ]

    def _exploding_shadow(*args, **kwargs):
        raise RuntimeError("shadow ranking explosion! must never affect cycle!")

    monkeypatch.setattr(demand_ranking, "record_shadow_choice", _exploding_shadow)

    # Must NOT raise, must return normal rotation pick
    selected = llm_proposer._select_assigned_demand(state_dir, items)
    assert selected == [items[0]]

    # Rotation state still saved
    rot = json.loads((state_dir / "demand" / "rotation.json").read_text(encoding="utf-8"))
    assert "priority-1" in rot.get("served", {})


def test_record_shadow_choice_internal_error_fails_open(tmp_path: Path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    items = [_make_demand_item("priority-1", "priority", "task 1")]

    monkeypatch.setattr(demand_ranking, "rank_candidates", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad rank")))

    # Function itself returns None fail-open, no exception
    res = demand_ranking.record_shadow_choice(state_dir, candidates=items, rotation_choice=items[0])
    assert res is None


def test_ranking_shadow_kill_switch(tmp_path: Path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    items = [_make_demand_item("priority-1", "priority", "task 1")]

    monkeypatch.setenv("SELFEVO_RANKING_SHADOW_ENABLED", "0")

    selected = llm_proposer._select_assigned_demand(state_dir, items)
    assert selected == [items[0]]

    shadow_file = demand_ranking.shadow_choices_path(state_dir)
    assert not shadow_file.exists()
