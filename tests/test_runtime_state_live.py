"""Tests for the #914 live operator-status surface.

Covers ``nanobot.runtime.state.load_runtime_state_from_root``'s new `live`
section (goal registry, ledger tail, scorecard) and ``format_runtime_state``'s
live-first rendering with decommissioned labeling of coordinator-era
(outbox/promotions/experiments/credits) artifacts. See issue #914: the
status surface previously showed only frozen coordinator-era data.
"""

import json
from pathlib import Path

from nanobot.runtime.state import format_runtime_state, load_runtime_state_from_root


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_live_section_populated_from_full_live_state_dir(tmp_path: Path):
    state_root = tmp_path / "state"
    # #1222: the live goal id is the operator canon's goal_text.json:goal_id.
    _write_json(state_root / "goals" / "goal_text.json", {"goal_id": "goal-live-1", "text": "goal"})
    _write_ledger(
        state_root / "ledger" / "cycles.jsonl",
        [
            {"phase": "proposed", "cycle_id": "cycle-1", "task_title": "Fix the thing"},
            {
                "phase": "outcome",
                "cycle_id": "cycle-1",
                "outcome": "success",
                "ts": "2026-08-01T00:00:00Z",
                "files_changed": ["a.py"],
            },
            {"phase": "proposed", "cycle_id": "cycle-2", "task_title": "Add the feature"},
            {
                "phase": "outcome",
                "cycle_id": "cycle-2",
                "outcome": "failed",
                "ts": "2026-08-02T00:00:00Z",
            },
        ],
    )
    _write_json(
        state_root / "scorecard" / "latest.json",
        {
            "schema_version": "scorecard-v1",
            "computed_at_utc": "2026-08-02T00:05:00Z",
            "loop": {
                "confirmed_integration_ratio": 0.5,
                "repeat_failure_rate": 0.25,
                "idle_share": 0.1,
            },
            "quality": {"compile_clean_ratio": 0.9},
            "control_plane": {
                "SELFEVO_PRESET": "balanced",
                "models": {"proposer": "gpt-x"},
            },
        },
    )

    runtime = load_runtime_state_from_root(state_root, source_kind="workspace_state")
    live = runtime["live"]
    assert live is not None
    assert live["active_goal_id"] == "goal-live-1"

    outcomes = live["recent_outcomes"]
    assert len(outcomes) == 2
    # newest first
    assert outcomes[0]["cycle_id"] == "cycle-2"
    assert outcomes[0]["outcome"] == "failed"
    assert outcomes[0]["task_title"] == "Add the feature"
    assert outcomes[1]["cycle_id"] == "cycle-1"
    assert outcomes[1]["outcome"] == "success"
    assert outcomes[1]["task_title"] == "Fix the thing"
    assert outcomes[1]["files_changed"] == ["a.py"]

    scorecard = live["scorecard"]
    assert scorecard["confirmed_integration_ratio"] == 0.5
    assert scorecard["repeat_failure_rate"] == 0.25
    assert scorecard["idle_share"] == 0.1
    assert scorecard["compile_clean_ratio"] == 0.9
    assert scorecard["preset"] == "balanced"
    assert scorecard["models"] == {"proposer": "gpt-x"}

    formatted = format_runtime_state(runtime)
    live_idx = next(i for i, line in enumerate(formatted) if line == "Live status:")
    legacy_idx = next(i for i, line in enumerate(formatted) if line == "Legacy runtime detail:")
    assert live_idx < legacy_idx
    assert any("Active goal (live): goal-live-1" in line for line in formatted)
    assert any("cycle=cycle-2" in line and "title=Add the feature" in line for line in formatted)
    assert any("Scorecard confirmed integration ratio: 0.5" in line for line in formatted)
    assert any("Active preset (live): balanced" in line for line in formatted)


def test_live_section_partial_when_scorecard_missing(tmp_path: Path):
    state_root = tmp_path / "state"
    _write_json(state_root / "goals" / "goal_text.json", {"goal_id": "goal-live-2", "text": "goal"})
    _write_ledger(
        state_root / "ledger" / "cycles.jsonl",
        [
            {
                "phase": "outcome",
                "cycle_id": "cycle-9",
                "outcome": "success",
                "ts": "2026-08-03T00:00:00Z",
            },
        ],
    )
    # no scorecard/ directory at all

    runtime = load_runtime_state_from_root(state_root, source_kind="workspace_state")
    live = runtime["live"]
    assert live is not None
    assert live["active_goal_id"] == "goal-live-2"
    assert len(live["recent_outcomes"]) == 1
    assert live["scorecard"] is None

    # fail-open: no exception, and format still renders cleanly.
    formatted = format_runtime_state(runtime)
    assert any("Scorecard (live): unknown" in line for line in formatted)


def test_live_section_empty_for_fully_empty_state_dir(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)

    runtime = load_runtime_state_from_root(state_root, source_kind="workspace_state")

    assert runtime["live"] is None

    formatted = format_runtime_state(runtime)
    assert any("Active goal (live): unknown" in line for line in formatted)
    assert any("Recent outcomes (live): unknown" in line for line in formatted)
    assert any("Scorecard (live): unknown" in line for line in formatted)


def test_coordinator_artifacts_are_not_read_and_promotions_are_live(tmp_path: Path):
    """#1222 supersedes #914's "(decommissioned — frozen data)" labelling: the
    frozen outbox/ and credits/ files are not read at all, so there is nothing
    to label; promotions/latest.json is written by the bridge and is live."""
    state_root = tmp_path / "state"
    _write_json(state_root / "outbox" / "latest.json", {"status": "PASS"})
    _write_json(state_root / "credits" / "latest.json", {"balance": 5, "delta": -1})
    _write_json(state_root / "promotions" / "latest.json", {"promotion_candidate_id": "promo-1"})

    runtime = load_runtime_state_from_root(state_root, source_kind="workspace_state")

    for retired in ("outbox_decommissioned", "credits_decommissioned", "experiment_decommissioned",
                    "promotion_decommissioned", "credits_balance", "outbox_path"):
        assert retired not in runtime, retired
    assert runtime["promotion_candidate_id"] == "promo-1"

    formatted = format_runtime_state(runtime)
    assert any(line.startswith("  Promotion source:") and line.endswith("latest.json") for line in formatted)
    assert not any("decommissioned" in line for line in formatted)
    assert not any(line.startswith("  Outbox source:") or line.startswith("  Credits") for line in formatted)


def test_empty_state_root_does_not_error(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)

    runtime = load_runtime_state_from_root(state_root, source_kind="workspace_state")

    assert runtime["active_goal"] is None
    assert runtime["promotion_candidate_id"] is None
    formatted = format_runtime_state(runtime)
    assert formatted[0] == "Runtime:"
    assert not any("decommissioned" in line for line in formatted)
