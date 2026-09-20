"""Tests for the several-candidates-one-call proposal path (#1796, ADR-027):
propose_multi, select_ranked_candidate, and maybe_propose_ranked.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.runtime import artifact_graph, llm_proposer

ENV_VAR = llm_proposer.ENABLED_ENV
RANKED_ENV = llm_proposer._RANKED_PROPOSAL_ENABLED_ENV


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    (state_dir / "goals").mkdir(parents=True)
    return state_dir


def _write_goal_text(state_dir: Path, text: str) -> None:
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"text": text}), encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("SELFEVO_DEMAND_DRIVEN_ENABLED", "0")


# ─── _extract_json_array ────────────────────────────────────────────────────


def test_extract_json_array_from_a_fence():
    text = '```json\n[{"a": 1}, {"a": 2}]\n```'
    assert llm_proposer._extract_json_array(text) == [{"a": 1}, {"a": 2}]


def test_extract_json_array_bare():
    text = 'here you go: [{"a": 1}]'
    assert llm_proposer._extract_json_array(text) == [{"a": 1}]


def test_extract_json_array_drops_non_object_elements():
    text = '[{"a": 1}, "not an object", 42, {"a": 2}]'
    assert llm_proposer._extract_json_array(text) == [{"a": 1}, {"a": 2}]


def test_extract_json_array_returns_none_on_garbage():
    assert llm_proposer._extract_json_array("no json here") is None
    assert llm_proposer._extract_json_array("") is None


def test_extract_json_array_returns_none_for_a_bare_object():
    """A single object (propose()'s own shape) is not an array -- must not
    be silently coerced into a one-element list."""
    assert llm_proposer._extract_json_array('{"a": 1}') is None


# ─── select_ranked_candidate ────────────────────────────────────────────────


def test_select_ranked_candidate_never_drops_one(tmp_path):
    state_dir = _state_dir(tmp_path)
    candidates = [
        {"target_path": "docs/x.md"},
        {},
        "garbage",
        {"target_path": "scripts/y.py"},
    ]
    winner, ranked = llm_proposer.select_ranked_candidate(candidates, state_dir=state_dir)
    assert len(ranked) == len(candidates)
    assert winner is ranked[0]


def test_select_ranked_candidate_with_no_published_graph_still_ranks(tmp_path):
    """An unavailable graph (nothing published yet) must degrade the value
    term, not the ranking call itself."""
    state_dir = _state_dir(tmp_path)
    candidates = [{"target_path": "scripts/anything.py"}]
    winner, ranked = llm_proposer.select_ranked_candidate(candidates, state_dir=state_dir)
    assert winner is not None
    assert len(ranked) == 1


def test_select_ranked_candidate_reads_the_published_graph(tmp_path):
    state_dir = _state_dir(tmp_path)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "leaf.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "scripts" / "consumer.py").write_text("import leaf\n", encoding="utf-8")
    artifact_graph.publish_artifact_graph(state_dir, repo)

    candidates = [{"target_path": "scripts/leaf.py"}, {"target_path": "docs/x.md"}]
    winner, ranked = llm_proposer.select_ranked_candidate(candidates, state_dir=state_dir)
    # scripts/leaf.py is used_by scripts/consumer.py -> extends_component,
    # not connects_leaf, but still ranks above a doc nothing routes to.
    assert winner.candidate["target_path"] == "scripts/leaf.py"


# ─── maybe_propose_ranked: switch off is a no-op passthrough ──────────────


def test_ranked_mode_off_delegates_to_maybe_propose_unchanged(tmp_path, monkeypatch):
    """The default (switch off) must be byte-for-byte maybe_propose's own
    behavior -- this is the "zero behavior change" half of the rollout."""
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")

    calls = {"single": 0, "multi": 0}

    def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
        calls["single"] += 1
        return {
            "task_title": "Document the deployment runbook",
            "rationale": "Fills a documentation gap.",
            "target_path": "docs/deploy.md",
            "serves": "priority 4",
        }

    def _fake_propose_multi(*a, **kw):
        calls["multi"] += 1
        return None

    monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
    monkeypatch.setattr(llm_proposer, "propose_multi", _fake_propose_multi)
    # RANKED_ENV left unset -> off by default.
    result = llm_proposer.maybe_propose_ranked(state_dir, None)

    assert result is not None
    assert calls["single"] == 1
    assert calls["multi"] == 0


# ─── maybe_propose_ranked: switch on, the real flow ────────────────────────


def test_ranked_mode_on_makes_exactly_one_llm_call_for_n_candidates(tmp_path, monkeypatch):
    """ADR-027 decision 7's own cost discipline: N candidates, ONE call."""
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")
    monkeypatch.setenv(RANKED_ENV, "1")

    calls = []

    def _fake_propose_multi(context, **kw):
        calls.append(context)
        return [
            {"task_title": "Document a thing nothing routes to", "rationale": "r",
             "target_path": "docs/x.md", "serves": "priority 1"},
            {"task_title": "Add a helper", "rationale": "r",
             "target_path": "scripts/new_thing.py", "serves": "priority 1"},
        ]

    monkeypatch.setattr(llm_proposer, "propose_multi", _fake_propose_multi)
    result = llm_proposer.maybe_propose_ranked(state_dir, None)

    assert len(calls) == 1  # exactly one LLM call produced every candidate
    assert result is not None


def test_ranked_mode_writes_the_highest_scored_candidate_first(tmp_path, monkeypatch):
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")
    monkeypatch.setenv(RANKED_ENV, "1")

    def _fake_propose_multi(context, **kw):
        return [
            {"task_title": "A doc nothing routes to", "rationale": "r",
             "target_path": "docs/x.md", "serves": "priority 1"},
            {"task_title": "A new script with a named consumer", "rationale": "r",
             "target_path": "scripts/new_thing.py", "serves": "priority 1",
             "consumer": "scripts/other.py"},
        ]

    monkeypatch.setattr(llm_proposer, "propose_multi", _fake_propose_multi)
    result = llm_proposer.maybe_propose_ranked(state_dir, None)

    assert result is not None
    assert "new script with a named consumer" in result.lower() or "new" in result.lower()

    rows = llm_proposer._load_ledger_rows(state_dir)
    proposed = [r for r in rows if r.get("phase") == "proposed"]
    assert len(proposed) == 1
    assert proposed[0]["target_path"] == "scripts/new_thing.py"  # new_with_consumer beats doc_no_route


def test_ranked_mode_records_every_candidates_score_before_trying_any(tmp_path, monkeypatch):
    """AC: 'a score is recorded per candidate with its three terms visible
    separately' -- and it must be recorded even for candidates that never
    get tried because an earlier one wrote successfully."""
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")
    monkeypatch.setenv(RANKED_ENV, "1")

    def _fake_propose_multi(context, **kw):
        return [
            {"task_title": "A doc", "rationale": "r", "target_path": "docs/x.md", "serves": "priority 1"},
            {"task_title": "Another doc", "rationale": "r", "target_path": "docs/y.md", "serves": "priority 1"},
        ]

    monkeypatch.setattr(llm_proposer, "propose_multi", _fake_propose_multi)
    llm_proposer.maybe_propose_ranked(state_dir, None)

    rows = llm_proposer._load_ledger_rows(state_dir)
    ranked_rows = [r for r in rows if r.get("phase") == "ranked_candidates"]
    assert len(ranked_rows) == 1
    assert ranked_rows[0]["count"] == 2
    for score in ranked_rows[0]["scores"]:
        for key in ("value_tier", "value_score", "urgency_score", "size_cost", "size_estimated", "total"):
            assert key in score


def test_ranked_mode_records_rung_gained_as_a_claim_on_the_proposed_row(tmp_path, monkeypatch):
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")
    monkeypatch.setenv(RANKED_ENV, "1")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "leaf.py").write_text("x = 1\n", encoding="utf-8")
    artifact_graph.publish_artifact_graph(state_dir, repo)

    def _fake_propose_multi(context, **kw):
        return [{
            "task_title": "connect the leaf", "rationale": "r",
            "target_path": "scripts/leaf.py", "serves": "priority 1",
        }]

    monkeypatch.setattr(llm_proposer, "propose_multi", _fake_propose_multi)
    llm_proposer.maybe_propose_ranked(state_dir, None)

    rows = llm_proposer._load_ledger_rows(state_dir)
    proposed = [r for r in rows if r.get("phase") == "proposed"][0]
    assert proposed["rung_gained_claim"] is True  # connects_leaf tier


def test_ranked_mode_maybe_propose_without_ranking_never_sets_the_claim_field(tmp_path, monkeypatch):
    """Distinguishing claim from silence: the pre-#1796 maybe_propose path
    makes NO rung claim at all -- absent, not False -- so a reader can
    tell "no ranking ran" apart from "ranking ran and claimed no gain"."""
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")

    def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
        return {
            "task_title": "Document the deployment runbook", "rationale": "r",
            "target_path": "docs/deploy.md", "serves": "priority 4",
        }

    monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
    llm_proposer.maybe_propose(state_dir, None)

    rows = llm_proposer._load_ledger_rows(state_dir)
    proposed = [r for r in rows if r.get("phase") == "proposed"][0]
    assert "rung_gained_claim" not in proposed


# ─── the AC's own mandatory test: rank can never reject a task ─────────────


def test_the_lowest_ranked_candidate_still_gets_written_when_nothing_else_qualifies(tmp_path, monkeypatch):
    """The mandatory test: no code branch may reject a task for its rank.
    A doc-no-route candidate (the LOWEST possible tier) is the ONLY
    candidate offered and it still gets written -- ranking decided nothing
    about whether it was ADMITTED, only that (with nothing to compare
    against) it is first."""
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")
    monkeypatch.setenv(RANKED_ENV, "1")

    def _fake_propose_multi(context, **kw):
        return [{
            "task_title": "A document nothing routes to", "rationale": "r",
            "target_path": "docs/notes.md", "serves": "priority 1",
        }]

    monkeypatch.setattr(llm_proposer, "propose_multi", _fake_propose_multi)
    result = llm_proposer.maybe_propose_ranked(state_dir, None)

    assert result is not None
    rows = llm_proposer._load_ledger_rows(state_dir)
    assert any(r.get("phase") == "proposed" for r in rows)
    # Nothing in the reject vocabulary mentions score/value/rank/tier --
    # rejection here can only ever come from sizing or self-dedup.
    rejects = [r for r in rows if r.get("phase") == "proposer_reject"]
    assert rejects == []


def test_a_low_ranked_candidate_is_tried_and_written_when_higher_ones_fail_validation(tmp_path, monkeypatch):
    """The stronger form of the same guarantee: TWO candidates, the
    HIGHER-scored one fails sizing (missing serves), and the ranking loop
    still falls through to try the lower-scored one rather than giving up
    because "the best one didn't work"."""
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")
    monkeypatch.setenv(RANKED_ENV, "1")

    def _fake_propose_multi(context, **kw):
        return [
            {"task_title": "connect a leaf but forgot serves", "rationale": "r",
             "target_path": "scripts/leaf.py"},  # no `serves` -> fails sizing
            {"task_title": "A document nothing routes to", "rationale": "r",
             "target_path": "docs/notes.md", "serves": "priority 1"},
        ]

    monkeypatch.setattr(llm_proposer, "propose_multi", _fake_propose_multi)
    result = llm_proposer.maybe_propose_ranked(state_dir, None)

    assert result is not None
    rows = llm_proposer._load_ledger_rows(state_dir)
    proposed = [r for r in rows if r.get("phase") == "proposed"]
    assert len(proposed) == 1
    assert proposed[0]["target_path"] == "docs/notes.md"
    rejects = [r for r in rows if r.get("phase") == "proposer_reject"]
    assert len(rejects) == 1
    assert rejects[0]["reason"] == "sizing_rejected"


def test_all_candidates_failing_validation_writes_nothing_but_still_records_scores(tmp_path, monkeypatch):
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")
    monkeypatch.setenv(RANKED_ENV, "1")

    def _fake_propose_multi(context, **kw):
        return [
            {"task_title": "missing serves one", "rationale": "r", "target_path": "scripts/a.py"},
            {"task_title": "missing serves two", "rationale": "r", "target_path": "scripts/b.py"},
        ]

    monkeypatch.setattr(llm_proposer, "propose_multi", _fake_propose_multi)
    result = llm_proposer.maybe_propose_ranked(state_dir, None)

    assert result is None
    rows = llm_proposer._load_ledger_rows(state_dir)
    assert not any(r.get("phase") == "proposed" for r in rows)
    assert any(r.get("phase") == "ranked_candidates" for r in rows)
    rejects = [r for r in rows if r.get("phase") == "proposer_reject"]
    assert len(rejects) == 2


def test_no_candidates_from_the_gateway_records_a_reject_not_a_silent_none(tmp_path, monkeypatch):
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")
    monkeypatch.setenv(RANKED_ENV, "1")
    monkeypatch.setattr(llm_proposer, "propose_multi", lambda *a, **kw: None)

    result = llm_proposer.maybe_propose_ranked(state_dir, None)

    assert result is None
    rows = llm_proposer._load_ledger_rows(state_dir)
    assert any(r.get("phase") == "proposer_reject" for r in rows)
