"""Tests for strategist role (#999)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nanobot.runtime import demand
from nanobot.runtime.strategist import (
    SCHEMA,
    _apply,
    build_strategist_prompt,
    collect_inputs,
    load_watermark,
    run_strategist,
    save_watermark,
    validate_strategist_output,
)


@pytest.fixture
def mock_state_and_repo(tmp_path: Path, monkeypatch):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    # #1182: the charter lives in the release tree, never in the instance repo.
    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "goals.md").write_text("# Goals\n1. Improve runtime\n", encoding="utf-8")
    monkeypatch.setenv("RELEASE_ROOT", str(release_root))

    # Setup some initial state
    (state_root / "demand").mkdir(parents=True)
    (state_root / "demand" / "futility.json").write_text(
        json.dumps({"futile_gap_ids": ["gap-1", "gap-2"], "total_tracked": 2}),
        encoding="utf-8",
    )
    (state_root / "scorecard.json").write_text(
        json.dumps({"cycles_total": 42, "success_rate": 0.8}),
        encoding="utf-8",
    )
    (state_root / "cycle_ledger.jsonl").write_text(
        json.dumps({"cycle": 1, "success": True}) + "\n" +
        json.dumps({"cycle": 2, "success": False, "error": "timeout"}) + "\n",
        encoding="utf-8",
    )

    # Evolution tree: keeps the input gate (#1182) satisfied; the funnel is the
    # only empty input in this fixture (no ledger rows with a phase). Written
    # directly because record_node also appends a ledger row.
    (state_root / "evolution").mkdir()
    (state_root / "evolution" / "tree.json").write_text(json.dumps({
        "nodes": {"f1x": {"parent_sha": None, "cycle_id": "c-0", "fitness": {"integrations": 1}}}, "current_sha": "f1x",
    }), encoding="utf-8")

    # Repo docs: one v2 lesson card (#1071 shape) and one legacy row.
    (repo_root / "lessons").mkdir(parents=True)
    (repo_root / "lessons" / "lessons.yaml").write_text(
        "- id: L1\n  schema_version: 2\n  problem: check timers drift after deploy\n"
        "  solution: assert the timer unit is enabled in the deploy smoke test\n"
        "  first_seen: 2026-08-30T00:00:00Z\n"
        "- id: L0\n  generalized_insight: pin the model name in the unit env\n",
        encoding="utf-8",
    )

    return state_root, repo_root


def test_collect_inputs(mock_state_and_repo):
    state_root, repo_root = mock_state_and_repo
    inputs = collect_inputs(state_root, repo_root)

    assert inputs["futility"]["total_tracked"] == 2
    assert inputs["scorecard"]["latest"]["cycles_total"] == 42
    assert len(inputs["recent_cycles"]) == 2
    assert "Improve runtime" in inputs["goals"]
    assert inputs["inputs_status"]["goals"]["source"] == "release_root"
    assert "check timers" in inputs["insights"]["cards"][0]["problem"]
    assert inputs["lessons"] == ["pin the model name in the unit env"]
    assert inputs["inputs_status"]["insights"]["status"] == "complete"


def test_tree_digest_from_real_record_node(tmp_path):
    from nanobot.runtime.evolution_tree import record_node

    state_root = tmp_path
    record_node(state_root, sha="abc1", parent_sha=None, branch="main", cycle_id="c-1", reward=1.0)
    record_node(state_root, sha="abc2", parent_sha="abc1", branch="main", cycle_id="c-2", reward=2.5)

    from nanobot.runtime.strategist import _tree_digest

    digest = _tree_digest(state_root)
    assert digest["node_count"] == 2
    assert digest["current_best_path"] == ["abc2", "abc1"]
    assert digest["fitness_summary"]["chain_depth"] == 2
    assert digest["fitness_summary"]["reward_count"] == 2
    assert digest["fitness_summary"]["reward_max"] == 2.5
    assert digest["fitness_summary"]["reward_mean"] == 1.75


def test_durable_hypothesis_survives_backlog_snapshot(tmp_path):
    """Strategist hypotheses must survive the bridge's queue snapshot rewrite."""
    from nanobot.runtime.backlog_snapshot import write_backlog_snapshot
    from nanobot.runtime.hypothesis_backlog import append_hypotheses

    state_root = tmp_path / "state"
    append_hypotheses(state_root, [{
        "title": "Durable probe",
        "hypothesis": "The bounded probe improves the metric",
        "action": "Run the probe once",
        "data_to_collect": "metric value",
        "insight_criterion": "metric improves without regression",
        "source": "strategist",
    }])
    write_backlog_snapshot(state_root)

    items = demand._hypothesis_items(state_root, None)
    assert any(item["summary"] == "Durable probe" for item in items)
    assert (state_root / "hypotheses" / "durable.json").is_file()


def test_build_strategist_prompt_includes_triz_and_hadi(mock_state_and_repo):
    state_root, repo_root = mock_state_and_repo
    inputs = collect_inputs(state_root, repo_root)
    system_prompt, user_msg = build_strategist_prompt(inputs, watermark={})

    assert "TRIZ" in system_prompt
    assert "HADI" in system_prompt
    assert "strategist-hadi-v1" in user_msg


def test_validate_strategist_output():
    valid = {
        "schema": "strategist-hadi-v1",
        "period_reviewed": "2026-W34",
        "hypotheses": [
            {
                "title": "Optimize cache",
                "hypothesis": "Caching improves latency",
                "action": "Add redis cache",
                "data_to_collect": "Latency drops 50%",
                "insight_criterion": "p99_latency improves without error-rate regression",
                "priority": "high",
            }
        ],
        "futility_advisories": [
            {
                "topic_or_direction": "brute-force retry",
                "reason": "Always fails due to rate limits",
                "evidence": "30 failed cycles",
                "confidence": 0.9,
            }
        ],
        "strategy_notes": "Focus on caching",
    }
    valid.pop("strategy_notes")
    for item in valid["futility_advisories"]:
        item.pop("evidence", None)
        item.pop("confidence", None)
    assert validate_strategist_output(valid) is True

    # Missing schema
    assert validate_strategist_output({"hypotheses": []}) is False

    # Invalid hypothesis fields
    invalid_h = dict(valid)
    invalid_h["hypotheses"] = [{"title": "Incomplete"}]
    assert validate_strategist_output(invalid_h) is False


def test_apply_hypotheses_and_advisories(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()

    output = {
        "schema": "strategist-hadi-v1",
        "period_reviewed": "2026-W34",
        "hypotheses": [
            {
                "title": "Test H",
                "hypothesis": "Hypothesis statement",
                "action": "Do something",
                "data_to_collect": "Metrics",
                "insight_criterion": "metric_1 improves",
                "priority": "high",
            }
        ],
        "futility_advisories": [
            {
                "topic_or_direction": "dead_end",
                "reason": "Not working",
            }
        ],
    }

    counts = _apply(output, state_root, 3, 2)
    assert counts["hypotheses_appended"] == 1
    assert counts["advisories_recorded"] == 1

    # Check hypotheses file
    hyp_file = state_root / "hypotheses" / "durable.json"
    assert hyp_file.exists()
    hyp_data = json.loads(hyp_file.read_text(encoding="utf-8"))
    assert len(hyp_data["entries"]) == 1
    assert hyp_data["entries"][0]["title"] == "Test H"
    assert hyp_data["entries"][0]["source"] == "strategist"

    # Check advisories file
    adv_file = state_root / "strategist" / "advisories.json"
    assert adv_file.exists()
    adv_data = json.loads(adv_file.read_text(encoding="utf-8"))
    assert adv_data["schema"] == "strategist-advisories-v1"
    assert len(adv_data["advisories"]) == 1


def test_watermark_save_and_load(tmp_path: Path):
    state_root = tmp_path / "state"
    assert load_watermark(state_root) == {}

    payload = {"last_run_timestamp": "2026-08-26T12:00:00Z", "total_runs": 1}
    save_watermark(state_root, payload)

    loaded = load_watermark(state_root)
    assert loaded == payload


def test_malformed_output_leaves_watermark_and_outputs_unchanged(mock_state_and_repo, monkeypatch):
    state_root, repo_root = mock_state_and_repo
    save_watermark(state_root, {"total_runs": 4})
    result = run_strategist(state_root, repo_root, llm=lambda *_: "```json\\n{}\\n```")
    assert result["success"] is False
    assert load_watermark(state_root)["total_runs"] == 4
    assert not (state_root / "hypotheses" / "backlog.json").exists()
    assert not (state_root / "strategist" / "advisories.json").exists()


def test_hadi_missing_insight_criterion_is_rejected(mock_state_and_repo):
    state_root, repo_root = mock_state_and_repo
    output = {"schema": SCHEMA, "period_reviewed": "today", "hypotheses": [{"title": "x", "hypothesis": "h", "action": "a", "data_to_collect": "d"}], "futility_advisories": []}
    assert validate_strategist_output(output) is False


def test_run_strategist_end_to_end(mock_state_and_repo, monkeypatch):
    state_root, repo_root = mock_state_and_repo
    monkeypatch.setenv("SELFEVO_STRATEGIST_MODEL", "cl/test-strategist-model")

    mock_llm_response = {
        "schema": "strategist-hadi-v1",
        "period_reviewed": "2026-W34",
        "hypotheses": [
            {
                "title": "Subagent isolation",
                "hypothesis": "Isolating subagents reduces crosstalk",
                "action": "Introduce process boundary",
                "data_to_collect": "0 crosstalk incidents",
                "insight_criterion": "crosstalk_count stays at zero",
                "priority": "high",
            }
        ],
        "futility_advisories": [
            {
                "topic_or_direction": "regex fixing prompt bugs",
                "reason": "Brittle across models",
            }
        ],
    }

    mock_response_obj = MagicMock()
    mock_response_obj.content = json.dumps(mock_llm_response)

    with patch("nanobot.runtime.strategist._default_llm", return_value=mock_response_obj):
        result = run_strategist(state_root, repo_root)

    assert result["success"] is True
    assert result["counts"]["hypotheses_appended"] == 1
    assert result["counts"]["advisories_written"] == 1

    # Check decisions.jsonl
    decisions_file = state_root / "strategist" / "decisions.jsonl"
    assert decisions_file.exists()
    decision_lines = [json.loads(line) for line in decisions_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(decision_lines) == 1
    assert decision_lines[0]["success"] is True

    # Check watermark
    watermark = load_watermark(state_root)
    assert watermark["total_runs"] == 1
    assert watermark.get("last_model_used", watermark.get("model")) == "cl/test-strategist-model"

    # Check hypothesis backlog
    hyp_data = json.loads((state_root / "hypotheses" / "durable.json").read_text(encoding="utf-8"))
    assert len(hyp_data["entries"]) == 1
    assert hyp_data["entries"][0]["title"] == "Subagent isolation"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_strategist_atomic_json_mode_0644_under_restrictive_umask(tmp_path: Path) -> None:
    """#1096: _atomic_json must produce 0644 files even with umask 0077.

    Covers save_watermark (watermark.json) — the strategist's primary
    mkstemp-based writer.
    """
    state_root = tmp_path / "state"
    state_root.mkdir()
    old_umask = os.umask(0o077)
    try:
        save_watermark(state_root, {"total_runs": 1})
    finally:
        os.umask(old_umask)
    wm_path = state_root / "strategist" / "watermark.json"
    assert wm_path.exists()
    assert wm_path.stat().st_mode & 0o777 == 0o644, (
        f"Expected 0644, got {oct(wm_path.stat().st_mode & 0o777)}"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_hypothesis_backlog_durable_json_mode_0644_under_restrictive_umask(tmp_path: Path) -> None:
    """#1096: append_hypotheses must produce durable.json at 0644 under umask 0077."""
    from nanobot.runtime.hypothesis_backlog import append_hypotheses

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    old_umask = os.umask(0o077)
    try:
        append_hypotheses(
            state_dir,
            [{"title": "Test hypothesis", "hypothesis": "testing", "action": "do",
              "data_to_collect": "metrics", "insight_criterion": "x improves", "priority": "low"}],
        )
    finally:
        os.umask(old_umask)
    durable = state_dir / "hypotheses" / "durable.json"
    assert durable.exists()
    assert durable.stat().st_mode & 0o777 == 0o644, (
        f"Expected 0644, got {oct(durable.stat().st_mode & 0o777)}"
    )
