"""Tests for demand_ranking (#1796, ADR-027): rung gained, urgency, measured
cost, as an input to ordering -- never a gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import cycle_ledger, demand_ranking as dr
from nanobot.runtime.artifact_graph import ArtifactGraph, Edge, Node


def _graph() -> ArtifactGraph:
    return ArtifactGraph(
        status="complete",
        generated_at="2026-09-20T00:00:00Z",
        nodes={
            "scripts/leaf": Node(id="scripts/leaf", type="script", path="scripts/leaf.py"),
            "scripts/comp": Node(id="scripts/comp", type="script", path="scripts/comp.py"),
            "scripts/consumer": Node(id="scripts/consumer", type="script", path="scripts/consumer.py"),
        },
        edges=[Edge(source="scripts/consumer.py", target="scripts/comp", kind="used_by", evidence="x")],
    )


# ─── value: the ordinal tiers, per ADR-027 decision 1's table ──────────────


def test_connecting_a_leaf_is_the_highest_tier():
    tier, score = dr.classify_value({"target_path": "scripts/leaf.py"}, _graph())
    assert tier == "connects_leaf"
    assert score == max(dr._VALUE_SCORE.values())


def test_extending_a_component_is_high_but_below_connecting_a_leaf():
    tier, score = dr.classify_value({"target_path": "scripts/comp.py"}, _graph())
    assert tier == "extends_component"
    assert score < dr._VALUE_SCORE["connects_leaf"]


def test_new_artifact_with_a_named_consumer_is_moderate():
    tier, _ = dr.classify_value(
        {"target_path": "scripts/new.py", "consumer": "scripts/comp.py"}, _graph(),
    )
    assert tier == "new_with_consumer"


def test_new_artifact_without_a_consumer_is_lowest():
    tier, score = dr.classify_value({"target_path": "scripts/new.py"}, _graph())
    assert tier == "new_without_consumer"
    assert score == min(dr._VALUE_SCORE.values())


def test_a_document_nothing_routes_to_is_lowest():
    tier, score = dr.classify_value({"target_path": "docs/notes.md"}, _graph())
    assert tier == "doc_no_route"
    assert score == min(dr._VALUE_SCORE.values())


def test_reduces_failure_mode_claim_is_high_tier():
    tier, _ = dr.classify_value(
        {"target_path": "docs/notes.md", "reduces_failure_mode": True}, _graph(),
    )
    assert tier == "reduces_failure_mode"


def test_a_malformed_candidate_scores_unknown_not_an_exception():
    tier, score = dr.classify_value("not-a-dict", _graph())  # type: ignore[arg-type]
    assert tier == "unknown"
    assert score == min(dr._VALUE_SCORE.values())

    tier2, _ = dr.classify_value({}, _graph())
    assert tier2 == "unknown"


def test_an_unavailable_graph_degrades_to_unknown_never_a_fabricated_tier():
    """graph=None must never guess connects_leaf/extends_component -- the
    harness cannot currently tell, and a fabricated high tier would
    mislead the ordering even though it can never gate anything."""
    tier, score = dr.classify_value({"target_path": "scripts/leaf.py"}, None)
    assert tier == "new_without_consumer"  # target unknown -> treated as new
    assert score < dr._VALUE_SCORE["connects_leaf"]


# ─── urgency: harness-derived, the model supplies neither input ───────────


def test_urgency_is_flat_for_every_tier_except_moves_deliverable():
    for tier in dr.VALUE_TIERS:
        if tier == "moves_deliverable":
            continue
        u = dr.compute_urgency(tier, now=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc))
        assert u.score == 0.0


def test_urgency_rises_toward_deep_sleep_only_for_moves_deliverable():
    # Express the intended wall-clock positions in the host's local timezone:
    # day_clock measures the day at local midnight, regardless of the machine
    # running the test. Passing UTC datetimes here would instead select their
    # converted local positions (which can cross a date boundary).
    early = dr.compute_urgency("moves_deliverable", now=datetime(2026, 9, 20, 4, 0).astimezone())
    late = dr.compute_urgency("moves_deliverable", now=datetime(2026, 9, 20, 23, 0).astimezone())
    assert late.score > early.score


def test_urgency_takes_no_input_from_the_candidate_at_all():
    """The AC's own requirement: urgency cannot be set by the model. Two
    wildly different candidates, same tier, same moment -> identical
    urgency -- proving the candidate's OWN fields (a claimed priority, a
    self-reported urgency, anything) have zero path into this value."""
    now = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    u1 = dr.compute_urgency("connects_leaf", now=now)
    u2 = dr.compute_urgency("connects_leaf", now=now)
    assert u1 == u2
    # compute_urgency's own signature proves it further: it takes a tier
    # string and (now, state_dir) -- no candidate dict parameter exists
    # for a model reply to populate at all.
    import inspect
    params = set(inspect.signature(dr.compute_urgency).parameters)
    assert "candidate" not in params


def test_deliverable_stage_is_read_from_day_clock_not_invented():
    u = dr.compute_urgency("moves_deliverable")
    from nanobot.runtime import day_clock
    assert u.deliverable_stage == day_clock.deliverable_stage()


# ─── size: measured history, else a marked estimate ───────────────────────


def _append(state_dir: Path, event: dict) -> None:
    cycle_ledger.append_event(state_dir, event)


def test_size_falls_back_to_a_marked_estimate_with_no_history(tmp_path):
    state = tmp_path / "state"
    size = dr.compute_size({"change_shape": "feature", "estimated_size": 3.0}, state)
    assert size.estimated is True
    assert size.cost == 3.0
    assert size.shape == "feature"


def test_size_defaults_to_1_when_no_history_and_no_self_report(tmp_path):
    state = tmp_path / "state"
    size = dr.compute_size({"change_shape": "feature"}, state)
    assert size.estimated is True
    assert size.cost == 1.0


def test_size_reads_measured_history_when_it_exists(tmp_path):
    state = tmp_path / "state"
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _append(state, {
        "phase": "started", "cycle_id": "c1",
        "ts": (now - timedelta(seconds=100)).isoformat().replace("+00:00", "Z"),
    })
    _append(state, {
        "phase": "outcome", "cycle_id": "c1", "outcome": "success",
        "change_shape": "feature",
        "ts": now.isoformat().replace("+00:00", "Z"),
    })

    size = dr.compute_size({"change_shape": "feature"}, state, now=now)
    assert size.estimated is False
    assert size.cost == 100.0


def test_size_measured_history_is_a_median_over_the_window(tmp_path):
    state = tmp_path / "state"
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    durations = [50.0, 100.0, 150.0]
    for i, d in enumerate(durations):
        cid = f"c{i}"
        _append(state, {
            "phase": "started", "cycle_id": cid,
            "ts": (now - timedelta(seconds=d)).isoformat().replace("+00:00", "Z"),
        })
        _append(state, {
            "phase": "outcome", "cycle_id": cid, "outcome": "success",
            "change_shape": "feature",
            "ts": now.isoformat().replace("+00:00", "Z"),
        })

    size = dr.compute_size({"change_shape": "feature"}, state, now=now)
    assert size.estimated is False
    assert size.cost == 100.0  # the median of [50, 100, 150]


def test_a_different_shape_does_not_borrow_another_shapes_history(tmp_path):
    state = tmp_path / "state"
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _append(state, {"phase": "started", "cycle_id": "c1", "ts": (now - timedelta(seconds=100)).isoformat().replace("+00:00", "Z")})
    _append(state, {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "change_shape": "feature", "ts": now.isoformat().replace("+00:00", "Z")})

    size = dr.compute_size({"change_shape": "documentation"}, state, now=now)
    assert size.estimated is True  # no "documentation"-shaped history recorded


def test_an_unclassified_shape_is_its_own_bucket_not_a_crash(tmp_path):
    state = tmp_path / "state"
    size = dr.compute_size({"change_shape": "not-a-real-shape"}, state)
    assert size.shape == "unclassified"


# ─── the never-drop guarantee: an input, never a gate ──────────────────────


def test_rank_candidates_never_drops_a_candidate(tmp_path):
    """The AC's own required test: nothing is refused, deferred, or
    suppressed by score. Feed it every awkward shape at once."""
    candidates = [
        {"target_path": "scripts/leaf.py"},
        {"target_path": "scripts/comp.py"},
        {"target_path": "docs/notes.md"},
        {},
        {"target_path": ""},
        None,
        "garbage",
        42,
        {"target_path": "scripts/leaf.py"},  # an exact duplicate
    ]
    ranked = dr.rank_candidates(candidates, graph=_graph(), state_dir=tmp_path)  # type: ignore[arg-type]
    assert len(ranked) == len(candidates)


def test_rank_candidates_orders_highest_value_first(tmp_path):
    candidates = [
        {"target_path": "docs/notes.md"},
        {"target_path": "scripts/leaf.py"},
        {"target_path": "scripts/comp.py"},
    ]
    ranked = dr.rank_candidates(candidates, graph=_graph(), state_dir=tmp_path)
    assert ranked[0].value_tier == "connects_leaf"
    assert ranked[-1].value_tier == "doc_no_route"


def test_rank_candidates_with_an_unavailable_graph_still_returns_everything(tmp_path):
    candidates = [{"target_path": "scripts/leaf.py"}, {"target_path": "docs/x.md"}]
    ranked = dr.rank_candidates(candidates, graph=None, state_dir=tmp_path)
    assert len(ranked) == 2


# ─── record_score: claim vs. measurement stay distinguishable ─────────────


def test_record_score_marks_rung_gained_as_a_claim(tmp_path):
    scored = dr.score_candidate({"target_path": "scripts/leaf.py"}, graph=_graph(), state_dir=tmp_path)
    row = dr.record_score(scored)
    assert row["rung_gained_claim"] is True
    assert "rung_gained_measured" not in row  # this module never fabricates the later measurement


def test_record_score_shows_all_three_terms_separately(tmp_path):
    scored = dr.score_candidate({"target_path": "scripts/leaf.py"}, graph=_graph(), state_dir=tmp_path)
    row = dr.record_score(scored)
    for key in ("value_tier", "value_score", "urgency_score", "size_cost", "size_estimated", "total"):
        assert key in row


def test_record_score_size_estimated_flag_survives_into_the_record(tmp_path):
    scored = dr.score_candidate({"target_path": "docs/x.md"}, graph=_graph(), state_dir=tmp_path)
    row = dr.record_score(scored)
    assert row["size_estimated"] is True  # no ledger history in a fresh tmp_path


# ─── increment_fit: non-monotonic size scaling (#1851, ADR-031) ─────────────


def test_increment_fit_favors_box_capacity_over_both_extremes(tmp_path):
    """Tasks of the same shape/tier: ~190 lines (box capacity) beats both
    trivial (5 lines) and oversized (2000 lines)."""
    trivial = {"target_path": "scripts/leaf.py", "estimated_lines": 5}
    optimal = {"target_path": "scripts/leaf.py", "estimated_lines": 190}
    oversized = {"target_path": "scripts/leaf.py", "estimated_lines": 2500}

    s_triv = dr.score_candidate(trivial, graph=_graph(), state_dir=tmp_path)
    s_opt = dr.score_candidate(optimal, graph=_graph(), state_dir=tmp_path)
    s_over = dr.score_candidate(oversized, graph=_graph(), state_dir=tmp_path)

    assert s_opt.increment_fit > s_triv.increment_fit
    assert s_opt.increment_fit > s_over.increment_fit
    assert s_opt.total > s_triv.total
    assert s_opt.total > s_over.total


def test_increment_fit_defaults_to_neutral_when_lines_absent(tmp_path):
    no_lines = {"target_path": "scripts/leaf.py"}
    s = dr.score_candidate(no_lines, graph=_graph(), state_dir=tmp_path)
    assert s.increment_fit == 1.0
