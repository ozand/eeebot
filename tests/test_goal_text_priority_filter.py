"""Issue #712: strip completed "Current priority target" entries from
goal_text.json's raw text before the bridge injects it verbatim into the
subagent prompt.

The deterministic coordinator path already skips done priorities via the
#575 git-log heuristic (`_parse_backlog_task_from_goal_text` /
`_title_already_done_in_git_log`), but the bridge's raw-text prompt injection
bypassed it entirely — a completed "Current priority target" kept being
shown/re-proposed every cycle (novelty collapse, per the #711 shadow run).
`filter_completed_priorities_from_goal_text` reuses that exact same
done-detection heuristic to rewrite the raw text before injection.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.cycle_planning import (
    _parse_backlog_task_from_goal_text,
    filter_completed_priorities_from_goal_text,
)
from tests.test_goal_backlog_routing import GOAL_TEXT_JSON, _make_git_repo_with_commit

RAW_TEXT = json.loads(GOAL_TEXT_JSON)["text"]


def test_done_priority_removed_and_moved_to_completed_sentence(tmp_path: Path):
    """Priority 5's title words all match one commit line → removed from
    "Current priority targets:" and listed under "Completed (do not repeat)"."""
    repo = _make_git_repo_with_commit(
        tmp_path, "feat: write scripts/cycle_logger.py — confirmed done for cycle-999"
    )

    rewritten = filter_completed_priorities_from_goal_text(RAW_TEXT, repo)

    assert rewritten != RAW_TEXT
    # Priority 5 no longer listed under "Current priority targets:"
    targets_section = rewritten.split("Current priority targets:", 1)[1]
    completed_split = targets_section.split("Completed (do not repeat):")
    current_targets_text = completed_split[0]
    assert "Priority 5" not in current_targets_text
    assert "cycle_logger.py" not in current_targets_text
    # Priority 6 (not done) is still listed
    assert "Priority 6" in current_targets_text
    assert "smoke_test_loop.py" in current_targets_text
    # Priority 5's title appears in the Completed sentence
    assert "Completed (do not repeat):" in rewritten
    completed_sentence = rewritten.split("Completed (do not repeat):", 1)[1]
    assert "cycle_logger.py" in completed_sentence

    # Still parseable, and the parser now returns Priority 6 (the only open one).
    state_root = tmp_path / "state1"
    goals_dir = state_root / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "goal_text.json").write_text(
        json.dumps({"text": rewritten}), encoding="utf-8"
    )
    task = _parse_backlog_task_from_goal_text(state_root)
    assert task is not None
    assert task["priority"] == 6


def test_all_done_current_priority_targets_section_empty(tmp_path: Path):
    """When every listed priority matches the log, "Current priority targets:"
    has no remaining entries and both titles are moved to Completed."""
    repo = _make_git_repo_with_commit(
        tmp_path,
        "feat: write scripts/cycle_logger.py finished",
        "feat: write scripts/smoke_test_loop.py finished with test",
    )

    rewritten = filter_completed_priorities_from_goal_text(RAW_TEXT, repo)

    assert "Completed (do not repeat):" in rewritten
    targets_section = rewritten.split("Current priority targets:", 1)[1]
    current_targets_text = targets_section.split("Completed (do not repeat):")[0]
    assert "Priority 5" not in current_targets_text
    assert "Priority 6" not in current_targets_text

    completed_sentence = rewritten.split("Completed (do not repeat):", 1)[1]
    assert "cycle_logger.py" in completed_sentence
    assert "smoke_test_loop.py" in completed_sentence

    # Parser now finds no open priorities.
    state_root = tmp_path / "state2"
    goals_dir = state_root / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "goal_text.json").write_text(
        json.dumps({"text": rewritten}), encoding="utf-8"
    )
    task = _parse_backlog_task_from_goal_text(state_root)
    assert task is None


def test_not_done_priority_left_untouched(tmp_path: Path):
    """A priority with no matching commit stays under "Current priority targets:"
    and the text is returned byte-identical (nothing to move)."""
    repo = _make_git_repo_with_commit(tmp_path, "chore: unrelated housekeeping commit")

    rewritten = filter_completed_priorities_from_goal_text(RAW_TEXT, repo)

    assert rewritten == RAW_TEXT
    assert "Completed (do not repeat):" not in rewritten


def test_fail_open_no_selfevo_repo_root(tmp_path: Path):
    assert filter_completed_priorities_from_goal_text(RAW_TEXT, None) == RAW_TEXT


def test_fail_open_repo_root_not_a_dir(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    assert filter_completed_priorities_from_goal_text(RAW_TEXT, missing) == RAW_TEXT


def test_fail_open_missing_current_priority_targets_marker(tmp_path: Path):
    repo = _make_git_repo_with_commit(tmp_path, "feat: write scripts/cycle_logger.py")
    text = "just a plain goal description, no priority targets here."
    assert filter_completed_priorities_from_goal_text(text, repo) == text


def test_fail_open_malformed_priority_section(tmp_path: Path):
    repo = _make_git_repo_with_commit(tmp_path, "feat: write scripts/cycle_logger.py")
    text = "mission statement\n\nCurrent priority targets:\nnot actually a priority line"
    assert filter_completed_priorities_from_goal_text(text, repo) == text


def test_fail_open_non_string_input(tmp_path: Path):
    repo = _make_git_repo_with_commit(tmp_path, "feat: write scripts/cycle_logger.py")
    assert filter_completed_priorities_from_goal_text(None, repo) is None  # type: ignore[arg-type]
