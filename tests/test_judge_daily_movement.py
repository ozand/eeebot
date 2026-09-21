"""Tests for daily loop movement verdict evaluator (#1855)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.judge_daily_movement import (
    evaluate_daily_movement,
    is_progressive_cycle,
    is_progressive_file,
    read_daily_verdict,
    run_judge,
)


def test_is_progressive_file_filters_trivial_surfaces() -> None:
    assert not is_progressive_file("AGENTS.md")
    assert not is_progressive_file("memory/MEMORY.md")
    assert not is_progressive_file("memory/HISTORY.md")
    assert not is_progressive_file("diary/2026-09-21.md")
    assert not is_progressive_file("diary/notes.txt")
    assert not is_progressive_file("")

    assert is_progressive_file("scripts/judge_daily_movement.py")
    assert is_progressive_file("tests/test_judge_daily_movement.py")
    assert is_progressive_file("nanobot/runtime/bridge.py")


def test_is_progressive_cycle() -> None:
    assert not is_progressive_cycle(["AGENTS.md"])
    assert not is_progressive_cycle(["diary/2026-09-21.md", "memory/MEMORY.md"])
    assert not is_progressive_cycle([])

    assert is_progressive_cycle(["AGENTS.md", "tests/test_agents_structure.py"])
    assert is_progressive_cycle(["scripts/new_tool.py"])


def test_evaluate_daily_movement_insufficient_data() -> None:
    w_start = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    w_end = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)
    res = evaluate_daily_movement([], window_start=w_start, window_end=w_end)

    assert res.verdict == "insufficient_data"
    assert res.total_attempts == 0
    assert res.productive_ratio == 0.0


def test_evaluate_daily_movement_stalled_zero_progressive() -> None:
    w_start = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    w_end = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)

    rows = [
        {"phase": "outcome", "outcome": "failed", "reason": "gate_failed"}
        for _ in range(8)
    ]
    res = evaluate_daily_movement(rows, window_start=w_start, window_end=w_end)

    assert res.verdict == "stalled"
    assert res.total_attempts == 8
    assert res.progressive_cycles == 0


def test_evaluate_daily_movement_appearance_of_work() -> None:
    w_start = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    w_end = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)

    rows = [
        {"phase": "outcome", "outcome": "success", "files_changed": ["diary/2026-09-21.md"]}
        for _ in range(6)
    ]
    rows.append({"phase": "outcome", "outcome": "success", "files_changed": ["scripts/tool.py"]})
    rows.extend([{"phase": "outcome", "outcome": "failed", "reason": "timeout"} for _ in range(3)])

    res = evaluate_daily_movement(rows, window_start=w_start, window_end=w_end)

    assert res.verdict == "appearance"
    assert res.successful_cycles == 7
    assert res.progressive_cycles == 1
    assert res.appearance_cycles == 6
    assert res.total_attempts == 10
    assert res.productive_ratio == 0.1


def test_evaluate_daily_movement_true_movement() -> None:
    w_start = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    w_end = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)

    rows = [
        {"phase": "outcome", "outcome": "success", "files_changed": ["scripts/tool.py", "tests/test_tool.py"]}
        for _ in range(4)
    ]
    rows.append({"phase": "outcome", "outcome": "success", "files_changed": ["AGENTS.md"]})
    rows.extend([{"phase": "outcome", "outcome": "failed", "reason": "timeout"} for _ in range(5)])

    res = evaluate_daily_movement(rows, window_start=w_start, window_end=w_end)

    assert res.verdict == "movement"
    assert res.progressive_cycles == 4
    assert res.productive_ratio == 0.4


def test_run_judge_writes_state_and_read_distinguishes_missing(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    assert read_daily_verdict(state_dir) is None

    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir()
    cycles_file = ledger_dir / "cycles.jsonl"
    cycles_file.write_text(
        json.dumps({
            "phase": "outcome",
            "outcome": "success",
            "files_changed": ["scripts/eval.py"],
            "ts": "2026-09-21T02:00:00Z",
            "iteration_fraction": 0.25,
        }) + "\n",
        encoding="utf-8",
    )

    now = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)
    verdict = run_judge(state_dir, now=now)

    assert verdict.verdict == "movement"
    assert verdict.progressive_cycles == 1
    assert verdict.total_attempts == 1

    data = read_daily_verdict(state_dir)
    assert data is not None
    assert data["verdict"] == "movement"
    assert data["progressive_cycles"] == 1
    assert (state_dir / "day_verdict" / "history.jsonl").exists()
