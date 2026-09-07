"""#1182: the strategist's archive inputs are alive, bounded and carry provenance.

Every repro test here fails against the pre-#1182 tree for the reason named
in its docstring; the existence test at the end is the exception required by
the try/except rule (names used inside helpers must be proven to exist).
"""
from __future__ import annotations

import datetime as dt
import gzip
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from nanobot.runtime import strategist, strategist_inputs
from nanobot.runtime.strategist import (
    collect_inputs,
    load_watermark,
    run_strategist,
    save_watermark,
)

VALID_OUTPUT = {
    "schema": "strategist-hadi-v1",
    "period_reviewed": "2026-W36",
    "hypotheses": [{
        "title": "t", "hypothesis": "h", "action": "a", "data_to_collect": "d",
        "insight_criterion": "c", "priority": "low",
    }],
    "futility_advisories": [],
}
FILLER = "Apply the reflected approach hint."


def _iso(days_ago: int, hour: int = 12) -> str:
    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return stamp.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")


def _card(idx: int, solution: str) -> str:
    return (f"- id: L{idx}\n  schema_version: 2\n  problem: problem {idx}\n"
            f"  solution: {solution}\n  first_seen: 2026-08-{10 + idx:02d}T00:00:00Z\n")


@pytest.fixture
def roots(tmp_path: Path, monkeypatch):
    """Bare state/repo/release roots; RELEASE_ROOT points at an empty dir."""
    state_root, repo_root, release_root = tmp_path / "state", tmp_path / "repo", tmp_path / "release"
    for root in (state_root, repo_root, release_root):
        root.mkdir()
    monkeypatch.setenv("RELEASE_ROOT", str(release_root))
    monkeypatch.setenv("SELFEVO_STRATEGIST_MODEL", "cl/test-strategist-model")
    return state_root, repo_root, release_root


def _write_ledger(state_root: Path, rows: list[dict], *, day_file: str | None = None) -> None:
    ledger = state_root / "ledger"
    ledger.mkdir(exist_ok=True)
    text = "".join(json.dumps(row) + "\n" for row in rows)
    if day_file is None:
        (ledger / "cycles.jsonl").write_text(text, encoding="utf-8")
    else:
        with gzip.open(ledger / f"cycles-{day_file}.jsonl.gz", "wt", encoding="utf-8") as handle:
            handle.write(text)


def _write_lessons(repo_root: Path, text: str) -> None:
    (repo_root / "lessons").mkdir(exist_ok=True)
    (repo_root / "lessons" / "lessons.yaml").write_text(text, encoding="utf-8")


def _copy_real_lessons(repo_root: Path) -> None:
    source = Path(__file__).parent / "fixtures" / "lessons-origin-main.yaml"
    (repo_root / "lessons").mkdir(exist_ok=True)
    (repo_root / "lessons" / "lessons.yaml").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _write_tree(state_root: Path, nodes: dict, current: str) -> None:
    (state_root / "evolution").mkdir(exist_ok=True)
    (state_root / "evolution" / "tree.json").write_text(json.dumps({"nodes": nodes, "current_sha": current, "switches": []}), encoding="utf-8")


def _healthy(state_root: Path, repo_root: Path, release_root: Path) -> None:
    """All five inputs non-empty (live shape after the fix)."""
    (release_root / "goals.md").write_text("# Charter\nKeep the loop honest.\n", encoding="utf-8")
    (state_root / "scorecard").mkdir(exist_ok=True)
    (state_root / "scorecard" / "latest.json").write_text(json.dumps({"confirmed_ratio": 0.45}), encoding="utf-8")
    (state_root / "scorecard" / "history.jsonl").write_text(
        "".join(json.dumps({"computed_at_utc": _iso(d), "confirmed_ratio": 0.4}) + "\n" for d in range(6, -1, -1)), encoding="utf-8")
    _write_ledger(state_root, [
        {"phase": "proposed", "cycle_id": "c1", "demand_id": "d1", "ts": _iso(1)},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _iso(1, 13)},
        {"phase": "proposed", "cycle_id": "c2", "demand_id": "d2", "ts": _iso(0)},
        {"phase": "outcome", "cycle_id": "c2", "outcome": "failure", "ts": _iso(0, 13)},
    ])
    _write_tree(state_root, {
        "s1": {"parent_sha": None, "cycle_id": "c1", "ts": _iso(1), "fitness": {"reward": None, "integrations": 3, "confirmed_integrations": 1, "repeat_failure_rate": 0.25}},
        "s2": {"parent_sha": "s1", "cycle_id": "c2", "ts": _iso(0), "fitness": {"reward": None, "integrations": 5, "confirmed_integrations": 2, "repeat_failure_rate": 0.2}},
    }, "s2")
    _write_lessons(repo_root, _card(1, "pin the checker list in the fixture"))


# --- repro tests: each FAILS on the pre-#1182 tree ---------------------------

def test_charter_is_read_from_release_root_not_instance_repo(roots):
    """Pre-fix: collect_inputs read <repo_root>/goals.md, which the instance repo lacks -> ''."""
    state_root, repo_root, release_root = roots
    (release_root / "goals.md").write_text("# Charter\nOnly here.\n", encoding="utf-8")
    inputs = collect_inputs(state_root, repo_root)
    assert "Only here." in inputs["goals"]
    assert inputs["inputs_status"]["goals"] == {"chars": len(inputs["goals"]), "source": "release_root", "status": "complete"}


def test_charter_falls_back_to_goal_text_json(roots):
    state_root, repo_root, _ = roots
    (state_root / "goals").mkdir()
    (state_root / "goals" / "goal_text.json").write_text(json.dumps({"text": "fallback charter"}), encoding="utf-8")
    inputs = collect_inputs(state_root, repo_root)
    assert inputs["goals"] == "fallback charter"
    assert inputs["inputs_status"]["goals"]["source"] == "goal_text.json"


def test_insights_input_reads_the_real_origin_main_corpus(roots):
    state_root, repo_root, _ = roots
    _copy_real_lessons(repo_root)
    entries = yaml.safe_load((repo_root / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))

    assert len(entries) == 14
    assert all("reusable_insight" in entry for entry in entries)
    assert all(not {"problem", "solution", "generalized_insight"}.intersection(entry) for entry in entries)

    insights, status = strategist_inputs.insights_input(repo_root)

    assert status["legacy"] == 10
    assert status["cards"] == 0
    assert status["status"] == "complete"
    assert len(insights["legacy_insights"]) == 10
    assert all(text for text in insights["legacy_insights"])


def test_insights_come_from_v2_cards_and_report_filler_skipped(roots):
    """v2 cards remain preferred over legacy reusable insights."""
    state_root, repo_root, _ = roots
    _write_lessons(repo_root, _card(1, "add a deploy smoke test for the timer unit") + _card(2, "pin the runtime version in the unit env")
                   + _card(3, "split the module below the size cap") + _card(4, FILLER))
    inputs = collect_inputs(state_root, repo_root)
    assert [card["id"] for card in inputs["insights"]["cards"]] == ["L3", "L2", "L1"]
    assert inputs["inputs_status"]["insights"]["filler_skipped"] == 1
    assert inputs["inputs_status"]["insights"]["status"] == "complete"
    assert "reusable_insight" not in inspect.getsource(strategist)


def test_v2_cards_are_preferred_when_a_row_has_both_shapes(roots):
    _, repo_root, _ = roots
    _write_lessons(repo_root, "lessons:\n"
                   "- id: CARD\n  schema_version: 2\n  problem: observed parser failures during bounded reads\n  solution: use bounded parser reads incrementally for large files\n  first_seen: 2026-08-10T00:00:00Z\n  reusable_insight: ignored legacy text\n"
                   "- id: LEGACY\n  reusable_insight: retained legacy text\n")

    insights, status = strategist_inputs.insights_input(repo_root)

    assert status["cards"] == 1
    assert status["legacy"] == 1
    assert insights["cards"][0]["id"] == "CARD"
    assert insights["legacy_insights"] == ["retained legacy text"]


def test_empty_insights_report_empty_and_trigger_refusal(roots, monkeypatch):
    state_root, repo_root, _ = roots
    (repo_root / "lessons").mkdir()
    (repo_root / "lessons" / "lessons.yaml").write_text("- id: L1\n  title: no insight\n", encoding="utf-8")
    (repo_root / "lessons" / "errors.yaml").write_text("- id: E1\n  title: error context\n", encoding="utf-8")
    (repo_root / "memory").mkdir()
    (repo_root / "memory" / "index.md").write_text("- index context\n", encoding="utf-8")
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "index.md").write_text("- index context\n", encoding="utf-8")

    inputs = collect_inputs(state_root, repo_root)
    assert inputs["inputs_status"]["insights"]["status"] == "empty"
    assert strategist_inputs.empty_inputs({"insights": inputs["inputs_status"]["insights"], "funnel": {"status": "empty"}}) == ["funnel", "insights"]
    assert strategist_inputs.should_refuse({"insights": inputs["inputs_status"]["insights"], "funnel": {"status": "empty"}}) is True


def test_funnel_reads_per_gap_futile_flag(roots):
    """Pre-fix: read a 'futile_gap_ids' key that lives in the scorecard, not futility.json -> futile 0."""
    state_root, repo_root, _ = roots
    _write_ledger(state_root, [{"phase": "proposed", "cycle_id": "c1", "demand_id": "goal-gap-a820", "ts": _iso(1)}])
    (state_root / "demand").mkdir()
    (state_root / "demand" / "futility.json").write_text(json.dumps({
        "goal-gap-a820": {"gap_id": "goal-gap-a820", "metric": "stale_feeds", "attempt_count": 3, "futile": True},
        "goal-gap-c095": {"gap_id": "goal-gap-c095", "metric": "heldout_gap", "attempt_count": 1, "futile": False},
    }), encoding="utf-8")
    funnel = collect_inputs(state_root, repo_root)["funnel"]
    assert funnel["columns"] == ["proposed", "integrated", "self_dedup", "futile", "attempt_count"]
    assert funnel["by_demand_id"]["goal-gap-a820"] == [1, 0, 0, 1, "unavailable"]
    assert "goal-gap-c095" not in funnel["by_demand_id"], "not futile and never proposed in the window"


def _futility_record(**overrides):
    record = {
        "futility_status": "measured",
        "attempt_unit": "demand_id",
        "window_status": "complete",
        "last_evaluated_ts": "2026-09-07T00:00:00Z",
        "attempt_count": 3,
        "futile": False,
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_futility_record(attempt_count=3), 3),
        (_futility_record(attempt_count=0), 0),
        (_futility_record(attempt_count=8, stale=True, futility_status="not_evaluated"), "unavailable"),
        (_futility_record(attempt_count=0, stale=True, futility_status="not_evaluated"), "unavailable"),
        (None, "unavailable"),
    ],
)
def test_funnel_distinguishes_measured_and_unavailable_futility_records(record, expected):
    """A stale non-zero fossil (the live 8-attempt row) must not enter as a measurement."""
    rows = [{"phase": "proposed", "cycle_id": "c1", "demand_id": "goal-gap-test", "ts": _iso(0)}]
    result, _ = strategist_inputs.funnel_input(rows, {"goal-gap-test": record} if record is not None else {})
    assert result["by_demand_id"]["goal-gap-test"][-1] == expected


def test_funnel_requires_all_measurement_markers_before_numeric_count():
    rows = [{"phase": "proposed", "cycle_id": "c1", "demand_id": "goal-gap-test", "ts": _iso(0)}]
    for missing in ("futility_status", "attempt_unit", "window_status", "last_evaluated_ts"):
        record = _futility_record()
        record.pop(missing)
        result, _ = strategist_inputs.funnel_input(rows, {"goal-gap-test": record})
        assert result["by_demand_id"]["goal-gap-test"][-1] == "unavailable", missing


def test_funnel_keeps_the_200_most_recently_proposed_ids(roots):
    """Pre-fix: kept the first 200 dict keys (oldest file first) and read at most 500 lines per file."""
    state_root, repo_root, _ = roots
    plan = [(2, range(0, 125)), (1, range(125, 250)), (0, range(250, 300))]
    for days_ago, ids in plan:
        rows = [{"phase": "proposed", "cycle_id": f"c{i}", "demand_id": f"d{i:03d}", "ts": _iso(days_ago)} for i in ids]
        _write_ledger(state_root, rows, day_file=(dt.date.today() - dt.timedelta(days=days_ago)).isoformat())
    inputs = collect_inputs(state_root, repo_root)
    kept = set(inputs["funnel"]["by_demand_id"])
    assert len(kept) == 200
    assert {f"d{i:03d}" for i in range(125, 300)} <= kept, "the two newest days must survive the cap whole"
    assert len(kept & {f"d{i:03d}" for i in range(0, 125)}) == 25, "only the oldest day is cut"
    status = inputs["inputs_status"]["funnel"]
    assert (status["ids"], status["ids_dropped"], status["status"]) == (200, 100, "complete")
    assert status["ledger"]["files_read"] == 3 and status["ledger"]["covered_from"] is not None


def test_tree_digest_summarises_populated_fitness_and_outcome_mix(roots):
    """Pre-fix: only fitness.reward (always None on the host) -> reward_count 0 and nothing else."""
    state_root, repo_root, release_root = roots
    _healthy(state_root, repo_root, release_root)
    tree = collect_inputs(state_root, repo_root)["evolution_tree"]
    assert tree["node_count"] == 2 and tree["current_best_path"] == ["s2", "s1"]
    assert tree["fitness_summary"]["reward_count"] == 0
    assert tree["fitness_summary"]["integrations"] == {"count": 2, "mean": 4.0, "max": 5}
    assert tree["fitness_summary"]["repeat_failure_rate"]["max"] == 0.25
    assert tree["outcome_mix"] == {"success": 1, "failure": 1}
    assert tree["ts_span"][0] < tree["ts_span"][1]
    # the wrapper the older test imports keeps its shape
    assert strategist._tree_digest(state_root)["fitness_summary"]["chain_depth"] == 2


def test_decision_row_carries_inputs_status(roots):
    """Pre-fix: decisions.jsonl had prompt_chars only; the prompt record truncates at 1,000 chars."""
    state_root, repo_root, release_root = roots
    _healthy(state_root, repo_root, release_root)
    result = run_strategist(state_root, repo_root, llm=lambda *_: json.dumps(VALID_OUTPUT))
    assert result["success"] is True
    row = json.loads((state_root / "strategist" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
    status = row["inputs_status"]
    assert set(status) == {"goals", "scorecard", "funnel", "insights", "evolution_tree", "recent_cycles", "halved", "dropped"}
    assert status["goals"]["chars"] > 0
    assert status["scorecard"] == {"latest_keys": 1, "history_rows": 7, "history_samples": 7, "status": "complete"}
    assert status["funnel"]["ids"] == 2 and status["funnel"]["ledger"]["status"] == "complete"
    assert status["insights"]["cards"] == 1
    assert status["evolution_tree"]["fitness_values"] == 6
    assert status["recent_cycles"] == 4 and status["halved"] == [] and status["dropped"] == []


def test_refuses_the_llm_call_when_two_inputs_are_empty(roots, monkeypatch, capsys):
    """Pre-fix: the LLM was called on an empty view and advice was applied."""
    state_root, repo_root, _ = roots
    # scorecard latest present but no charter, no lessons, no tree; funnel from one row
    (state_root / "scorecard.json").write_text(json.dumps({"cycles_total": 1}), encoding="utf-8")
    _write_ledger(state_root, [{"phase": "proposed", "cycle_id": "c1", "demand_id": "d1", "ts": _iso(0)}])
    save_watermark(state_root, {"total_runs": 4, "last_run": "2026-09-01T00:00:00Z"})
    llm = MagicMock(return_value=json.dumps(VALID_OUTPUT))
    result = run_strategist(state_root, repo_root, llm=llm)
    assert llm.call_count == 0
    assert result["success"] is False and result["reason"] == "inputs_unavailable"
    assert result["empty_inputs"] == ["goals", "insights", "evolution_tree"]
    assert result["prompt_chars"] == 0
    assert load_watermark(state_root) == {"total_runs": 4, "last_run": "2026-09-01T00:00:00Z"}
    assert not (state_root / "hypotheses").exists()
    rows = (state_root / "strategist" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["reason"] == "inputs_unavailable"
    assert not (state_root / "strategist" / "errors.jsonl").exists()
    # the systemd entry point treats the refusal as a clean run
    monkeypatch.setattr(sys, "argv", ["strategist", "--state-root", str(state_root), "--repo", str(repo_root)])
    with patch("nanobot.runtime.strategist._default_llm", side_effect=AssertionError("must not be called")):
        assert strategist.main() == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "inputs_unavailable"


def test_one_empty_input_is_tolerated(roots):
    state_root, repo_root, release_root = roots
    _healthy(state_root, repo_root, release_root)
    (repo_root / "lessons" / "lessons.yaml").unlink()  # insights empty, the other four alive
    llm = MagicMock(return_value=json.dumps(VALID_OUTPUT))
    result = run_strategist(state_root, repo_root, llm=llm)
    assert llm.call_count == 1 and result["success"] is True
    assert result["inputs_status"]["insights"]["status"] == "empty"


def test_prompt_stays_within_cap_and_records_halved_sections(roots):
    """Pre-fix: one halving per section, silently; 600 long lessons fell through to {"truncated": true}."""
    state_root, repo_root, release_root = roots
    _healthy(state_root, repo_root, release_root)
    inputs = collect_inputs(state_root, repo_root)
    inputs["lessons"] = [f"lesson {i} " + "x" * 200 for i in range(600)]
    _, user = strategist.build_strategist_prompt(inputs, {})
    assert len(user) <= strategist._MAX_PROMPT_CHARS
    archive = json.loads(user)["archive"]
    assert "truncated" not in archive and archive["goals"].startswith("# Charter")
    assert inputs["inputs_status"]["halved"] == ["lessons", "lessons"], "the largest section is halved until the payload fits"
    assert archive["inputs_status"]["halved"] == inputs["inputs_status"]["halved"]
    # #1284: the record says what went, not only which section was cut
    assert inputs["inputs_status"]["dropped"] == [{"section": "lessons", "dropped": 300, "end": "tail"},
                                                  {"section": "lessons", "dropped": 150, "end": "tail"}]
    assert archive["lessons"][0] == inputs["lessons"][0] and len(archive["lessons"]) == 150, "newest-first list keeps its head"


# scorecard/latest.json on the host 2026-09-04 (key order and JSON bytes) — the
# run that cut this dict by position kept the first eight keys (#1284).
_LIVE_LATEST_ORDER = ("schema_version", "computed_at_utc", "window_days", "loop", "cost", "quality", "value", "heldout",
                      "integrity", "feeds", "bridge", "knowledge_lift", "control_plane", "reader_status", "gaps_status", "gaps")
_LIVE_LATEST_BYTES = (14, 29, 1, 478, 121, 93, 194, 91, 64, 894, 294, 127, 2427, 559, 10, 367)


def _live_latest(scale: int = 1) -> dict:
    return {key: {"payload": "x" * max(1, size * scale - 16)} for key, size in zip(_LIVE_LATEST_ORDER, _LIVE_LATEST_BYTES)}


def test_cut_step_drops_declared_cheap_keys_first_and_names_them():
    """Pre-fix (#1284): scorecard.latest kept schema_version/computed_at_utc/window_days and lost gaps."""
    archive = {"scorecard": {"latest": _live_latest(), "history_7d": {"samples": ["t0", "t1"], "series": {"a": [1, 2]}}}}
    record = strategist._cut_step(archive, "scorecard")
    assert record == {"section": "scorecard.latest", "dropped": 8, "keys": [
        "schema_version", "computed_at_utc", "window_days", "control_plane", "reader_status", "bridge", "knowledge_lift", "heldout"]}
    assert set(archive["scorecard"]["latest"]) == {"integrity", "cost", "quality", "feeds", "value", "loop", "gaps_status", "gaps"}
    # a second step on the same section keeps cutting from the cheap end
    assert strategist._cut_step(archive, "scorecard") == {"section": "scorecard.latest", "dropped": 4,
                                                          "keys": ["integrity", "cost", "quality", "feeds"]}
    assert set(archive["scorecard"]["latest"]) == {"value", "loop", "gaps_status", "gaps"}


def test_cut_step_order_is_what_pins_the_survivors(monkeypatch):
    """The same input under the reversed declaration loses gaps: the pin is the order, not the fixture."""
    archive = {"scorecard": {"latest": _live_latest(), "history_7d": {"samples": [], "series": {}}}}
    reversed_policy = {**strategist._CUT_POLICY, "scorecard.latest": tuple(reversed(strategist._CUT_POLICY["scorecard.latest"]))}
    monkeypatch.setattr(strategist, "_CUT_POLICY", reversed_policy)
    record = strategist._cut_step(archive, "scorecard")
    assert "gaps" in record["keys"] and "loop" in record["keys"]
    assert "schema_version" in archive["scorecard"]["latest"], "reversed order keeps the metadata — exactly the pre-fix outcome"


def test_cut_step_drops_undeclared_keys_before_any_declared_one():
    latest = {"experimental_block": {"payload": "y" * 5000}, **_live_latest()}
    archive = {"scorecard": {"latest": latest, "history_7d": {"samples": [], "series": {}}}}
    record = strategist._cut_step(archive, "scorecard")
    assert record["keys"][0] == "experimental_block" and "gaps" in archive["scorecard"]["latest"]


def test_cut_step_takes_the_cheap_end_of_each_sequence():
    """Ledger tails are oldest-first, so their head is the cheap end; ranked members lose their tail."""
    archive = {
        "recent_cycles": [{"ts": f"2026-09-0{d}"} for d in range(1, 9)],  # oldest first
        "prior_decisions": [{"timestamp": f"2026-08-2{d}"} for d in range(1, 5)],
        "lessons": [f"newest-first {i}" for i in range(4)],
        "funnel": {"columns": ["proposed"], "by_demand_id": {f"id-{i}": [i] for i in range(6)}},  # newest id first
        "insights": {"cards": [{"id": f"L{i}"} for i in range(4)], "errors": [{"title": f"E{i}"} for i in range(4)],
                     "indexes": {"memory/index.md": "m" * 300, "docs/index.md": "d" * 400}},
        "scorecard": {"latest": {"loop": {}}, "history_7d": {"samples": ["t0", "t1"], "series": {f"s{i}": [i, i] for i in range(4)}}},
    }
    assert strategist._cut_step(archive, "recent_cycles") == {"section": "recent_cycles", "dropped": 4, "end": "head"}
    assert [row["ts"] for row in archive["recent_cycles"]] == ["2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08"]
    assert strategist._cut_step(archive, "prior_decisions") == {"section": "prior_decisions", "dropped": 2, "end": "head"}
    assert [row["timestamp"] for row in archive["prior_decisions"]] == ["2026-08-23", "2026-08-24"]
    assert strategist._cut_step(archive, "lessons") == {"section": "lessons", "dropped": 2, "end": "tail"}
    assert archive["lessons"] == ["newest-first 0", "newest-first 1"]
    assert strategist._cut_step(archive, "funnel") == {"section": "funnel.by_demand_id", "dropped": 3, "end": "tail"}
    assert list(archive["funnel"]["by_demand_id"]) == ["id-0", "id-1", "id-2"] and archive["funnel"]["columns"] == ["proposed"]
    # insights: the largest member goes first (indexes), and its declared-cheap key is docs/index.md
    assert strategist._cut_step(archive, "insights") == {"section": "insights.indexes", "dropped": 1, "keys": ["docs/index.md"]}
    assert list(archive["insights"]["indexes"]) == ["memory/index.md"]
    archive["insights"]["indexes"] = {}
    assert strategist._cut_step(archive, "insights") == {"section": "insights.errors", "dropped": 2, "end": "head"}
    assert [e["title"] for e in archive["insights"]["errors"]] == ["E2", "E3"], "errors.yaml tail keeps the newest"
    # history axis (samples) is never cut; series lose the tail (least-moved) half
    assert strategist._cut_step(archive, "scorecard") == {"section": "scorecard.history_7d.series", "dropped": 2, "end": "tail"}
    assert archive["scorecard"]["history_7d"]["samples"] == ["t0", "t1"] and list(archive["scorecard"]["history_7d"]["series"]) == ["s0", "s1"]


def test_cut_step_yields_when_nothing_is_left():
    archive = {"lessons": ["one"], "funnel": {"columns": ["proposed"], "by_demand_id": {"id-0": [1]}}}
    assert strategist._cut_step(archive, "lessons") is None
    assert strategist._cut_step(archive, "funnel") is None
    assert strategist._cut_step(archive, "missing") is None


def test_every_prompt_leaf_declares_its_cheap_end(roots):
    """A member the policy does not know would fall back to position — the #1284 defect. Walk the
    live-shaped archive and demand a declaration for every list/str/dict-of-scalars a step could reach."""
    state_root, repo_root, release_root = roots
    _live_shape(state_root, repo_root, release_root)
    inputs = collect_inputs(state_root, repo_root)
    undeclared: list[str] = []

    def walk(path: str, value) -> None:
        if path in strategist._CUT_POLICY:
            return
        if isinstance(value, dict) and any(isinstance(v, (dict, list)) for k, v in value.items() if k not in strategist._HALVING_KEEP):
            for name, member in value.items():
                if name not in strategist._HALVING_KEEP and isinstance(member, (dict, list, str)):
                    walk(f"{path}.{name}", member)
            return
        undeclared.append(path)

    for section in strategist._HALVING_SECTIONS:
        walk(section, inputs[section])
    assert undeclared == []
    assert set(strategist._CUT_POLICY) >= {"scorecard.latest", "funnel.by_demand_id", "recent_cycles", "prior_decisions"}


def test_live_shape_over_budget_keeps_gaps_and_drops_control_plane_first(roots):
    """The 2026-09-04 run, replayed: scorecard.latest over budget must lose control_plane and the
    metadata keys, never gaps/loop/value/feeds, and the row must name what went."""
    state_root, repo_root, release_root = roots
    _live_shape(state_root, repo_root, release_root)
    inputs = collect_inputs(state_root, repo_root)
    inputs["scorecard"]["latest"] = _live_latest()
    inputs["scorecard"]["latest"]["control_plane"] = {"payload": "c" * 40_000}  # push the prompt over the cap
    _, user = strategist.build_strategist_prompt(inputs, {"total_runs": 9})
    assert len(user) <= strategist._MAX_PROMPT_CHARS
    archive = json.loads(user)["archive"]
    assert "truncated" not in archive
    status = inputs["inputs_status"]
    assert status["halved"][0] == "scorecard.latest" and status["dropped"][0]["section"] == "scorecard.latest"
    assert "control_plane" in status["dropped"][0]["keys"] and "schema_version" in status["dropped"][0]["keys"]
    for key in ("gaps", "gaps_status", "loop", "value", "feeds", "quality", "cost"):
        assert key in archive["scorecard"]["latest"], key
    assert "control_plane" not in archive["scorecard"]["latest"]
    assert archive["inputs_status"]["dropped"] == status["dropped"], "the record travels with the prompt and the decision row"


def _live_shape(state_root: Path, repo_root: Path, release_root: Path) -> None:
    """Sizes read on the host 2026-09-02: latest.json 6.8 KB, history rows 3 KB
    (48/day), 100 tree nodes, 250 demand ids in 30 days, 228 B ledger rows."""
    _healthy(state_root, repo_root, release_root)
    (state_root / "scorecard" / "latest.json").write_text(json.dumps(
        {f"section_{i}": {f"metric_{j}": 0.1 * j for j in range(12)} for i in range(20)}), encoding="utf-8")
    rows = []
    for i in range(7 * 48):
        stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30 * (7 * 48 - 1 - i))).isoformat().replace("+00:00", "Z")
        rows.append({"computed_at_utc": stamp, "quality": {"confirmed_ratio": 0.40 + i / 5000, "stale_feeds": 1.0},
                     "padding": {f"k{j}": "v" * 40 for j in range(60)}})
    (state_root / "scorecard" / "history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    ledger = []
    for i in range(250):
        ledger.append({"phase": "proposed", "cycle_id": f"c{i}", "demand_id": f"demand-{i:04d}", "ts": _iso(29 - i // 9, hour=i % 24),
                       "target_path": f"scripts/tool_{i % 40}.py", "task_title": "t" * 60, "request_id": "r" * 32})
        ledger.append({"phase": "outcome", "cycle_id": f"c{i}", "outcome": "success" if i % 3 else "failure", "ts": _iso(29 - i // 9, hour=i % 24),
                       "files_changed": [f"scripts/tool_{i % 40}.py"], "elapsed_seconds": 120})
    _write_ledger(state_root, ledger)
    _write_tree(state_root, {f"sha{i:03d}": {"parent_sha": f"sha{i - 1:03d}" if i else None, "cycle_id": f"c{i}", "ts": _iso(29 - i // 4),
                             "fitness": {"reward": None, "integrations": i, "confirmed_integrations": i // 2, "repeat_failure_rate": 0.1}}
                             for i in range(100)}, "sha099")
    _write_lessons(repo_root, "".join(_card(i, f"solution {i} " + "pin the exact version in the fixture " * 3) for i in range(1, 11))
                   + "".join(f"- id: G{i}\n  reusable_insight: legacy insight {i} " + "words " * 20 + "\n" for i in range(10)))
    (repo_root / "lessons" / "errors.yaml").write_text("".join(
        f"- id: E{i}\n  title: error {i}\n  root_cause: {'cause ' * 30}\n  prevention: {'guard ' * 30}\n" for i in range(16)), encoding="utf-8")
    (repo_root / "memory").mkdir()
    (repo_root / "memory" / "index.md").write_text("- memory line\n" * 300, encoding="utf-8")
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "index.md").write_text("- doc line\n" * 150, encoding="utf-8")
    for i in range(10):
        strategist._append_jsonl(state_root / "strategist" / "decisions.jsonl",
                                 {"timestamp": _iso(10 - i), "success": True, "reason": "valid bounded advisory output applied",
                                  "prompt_chars": 40000, "inputs_status": {"goals": {"chars": 2749}}, "counts": {"hypotheses_appended": 2}})


def test_live_shape_prompt_fits_and_keeps_every_input(roots):
    """Pre-fix: history_7d was 100 raw 3 KB rows or nothing; the halving pass ran once per section."""
    from nanobot.runtime import strategist_inputs

    state_root, repo_root, release_root = roots
    _live_shape(state_root, repo_root, release_root)
    inputs = collect_inputs(state_root, repo_root)
    status = inputs["inputs_status"]
    assert strategist_inputs.empty_inputs(status) == []
    assert status["scorecard"]["history_rows"] == 336 and status["scorecard"]["history_samples"] == 8
    assert status["funnel"]["ids"] == 200 and status["funnel"]["ids_dropped"] == 50
    assert status["insights"] == {"cards": 10, "filler_skipped": 0, "legacy": 10, "errors": 5, "status": "complete"}
    assert status["evolution_tree"]["nodes"] == 100
    trend = inputs["scorecard"]["history_7d"]
    assert len(trend["samples"]) == 8 and trend["samples"][0] < trend["samples"][-1]
    assert list(trend["series"])[0] == "quality.confirmed_ratio", "the metric that moved sorts first"
    assert trend["series"]["quality.confirmed_ratio"][0] < trend["series"]["quality.confirmed_ratio"][-1]
    assert "inputs_status" not in json.dumps(inputs["prior_decisions"])
    _, user = strategist.build_strategist_prompt(inputs, {"total_runs": 9})
    assert len(user) <= strategist._MAX_PROMPT_CHARS
    archive = json.loads(user)["archive"]
    assert "truncated" not in archive
    for name in strategist_inputs.INPUT_NAMES:
        assert archive[name], name
    assert archive["inputs_status"]["halved"] == status["halved"]


def test_dry_run_reports_inputs_and_writes_nothing(roots, monkeypatch, capsys):
    """Pre-fix: no --dry-run; the only way to see the inputs was a real LLM run."""
    state_root, repo_root, release_root = roots
    _healthy(state_root, repo_root, release_root)
    monkeypatch.setattr(sys, "argv", ["strategist", "--state-root", str(state_root), "--repo", str(repo_root), "--dry-run"])
    with patch("nanobot.runtime.strategist._default_llm", side_effect=AssertionError("must not be called")):
        assert strategist.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and report["would_refuse"] is False and report["empty_inputs"] == []
    assert report["inputs_status"]["goals"]["chars"] > 0 and report["prompt_chars"] > 0
    assert not (state_root / "strategist").exists(), "dry run must not create decisions or watermark"


def test_ledger_rows_skip_an_unreadable_archive_and_report_partial(roots):
    """Provenance of the window (state_access.ledger_window, #1174) reaches inputs_status verbatim."""
    from nanobot.runtime import strategist_inputs

    state_root, _, _ = roots
    _write_ledger(state_root, [{"phase": "proposed", "cycle_id": "c1", "demand_id": "d1", "ts": _iso(1)}], day_file=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    (state_root / "ledger" / f"cycles-{dt.date.today().isoformat()}.jsonl.gz").write_bytes(b"not gzip")
    (state_root / "ledger" / "cycles-notadate.jsonl.gz").write_bytes(b"")
    rows, meta = strategist_inputs.ledger_rows(state_root)
    assert [row["demand_id"] for row in rows] == ["d1"]
    assert meta["files_read"] == 1 and meta["files_skipped"] == 1 and meta["status"] == "partial"
    assert {note.split(":")[0] for note in meta["notes"]} >= {"gz_corrupt", "invalid_archive"}, meta["notes"]


def test_ledger_rows_ignore_archives_outside_the_horizon(roots):
    from nanobot.runtime import strategist_inputs

    state_root, _, _ = roots
    _write_ledger(state_root, [{"phase": "proposed", "cycle_id": "c0", "demand_id": "old", "ts": _iso(40)}], day_file=(dt.date.today() - dt.timedelta(days=40)).isoformat())
    _write_ledger(state_root, [{"phase": "proposed", "cycle_id": "c1", "demand_id": "new", "ts": _iso(1)}])
    rows, meta = strategist_inputs.ledger_rows(state_root)
    assert [row["demand_id"] for row in rows] == ["new"] and meta["files_read"] == 1


# --- names referenced inside helpers must exist (passes on both trees) -------

def test_helper_names_used_by_strategist_inputs_exist():
    from nanobot.runtime import goal_review, lesson_v2, llm_proposer

    assert callable(goal_review.read_charter_text)
    assert callable(llm_proposer._release_root_from_env)
    assert callable(lesson_v2.bounded_load_yaml)
    assert callable(lesson_v2.solution_is_meaningful)
    assert not lesson_v2.solution_is_meaningful("p", FILLER)


def test_issue1231_both_legacy_insight_keys_are_read(tmp_path):
    """Reading only one legacy key is the defect, whichever key that is.

    `reusable_insight` is what the entire origin/main corpus uses; but
    `generalized_insight` is what `bridge` and `knowledge_curator` write, so it
    is the shape new rows arrive in. Before #1231 the reader saw only the second
    and returned nothing from a corpus of 14; a fix that swaps rather than adds
    would go blind the moment curator output lands.
    """
    (tmp_path / "lessons").mkdir()
    (tmp_path / "lessons" / "lessons.yaml").write_text(
        yaml.safe_dump([
            {"id": "old", "date": "2026-01-01", "reusable_insight": "prefer the bounded read"},
            {"id": "new", "date": "2026-02-01", "generalized_insight": "avoid the unbounded scan"},
        ]),
        encoding="utf-8",
    )

    data, meta = strategist_inputs.insights_input(tmp_path)

    assert meta["legacy"] == 2, "one of the two legacy insight keys was ignored"
    assert "prefer the bounded read" in data["legacy_insights"]
    assert "avoid the unbounded scan" in data["legacy_insights"]
    assert meta["status"] == "complete"
