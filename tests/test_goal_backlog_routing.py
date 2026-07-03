"""Task #12: route OUR goals into the subagent's executable task.

The bridge subagent reads the materialized artifact's `next_bounded_candidate`
(title + backlog_instructions, with an imperative "implement and commit" prompt).
When the MEMORY backlog is empty the coordinator fell back to a stale research
feed, so the subagent never worked on our goals. The todo.md fallback routes the
top open goal in as a concrete implement-and-commit task.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.coordinator import (
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
    _next_open_goal_as_backlog_task,
    _parse_backlog_task_from_goal_text,
    _write_materialized_improvement_artifact,
)

TODO = """\
# todo
- [x] 0. done item
- [ ] 1. Approval truth normalization
  - Problem: approval file can be expired while dashboard still implies fresh.
  - Product changes:
    - recompute approval freshness from apply.ok
  - Acceptance:
    - UI says expired when expired
- [ ] 2. Next goal
"""

# Real on-host goal_text.json shape (#568) — see docs/changes/backlog-routing-real-dispatch/proposal.md.
GOAL_TEXT_JSON = """\
{
  "schema_version": "goal-text-v1",
  "goal_id": "goal-bootstrap",
  "updated_at_utc": "2026-06-21T15:30:00Z",
  "text": "eeebot is a resource-aware, self-evolving autonomous agent on a weak eeepc host.\\n\\nCurrent priority targets:\\n(A) Priority 5 \\u2014 Write scripts/cycle_logger.py: helper function append_cycle_summary(repo_root, cycle_id, action, files_changed) that appends one line to memory/HISTORY.md without duplicates. Test with python3 scripts/cycle_logger.py --test. Commit.\\n(B) Priority 6 \\u2014 Write scripts/smoke_test_loop.py: quick sanity check that state/current_health.json, state/host_capabilities.json, memory/MEMORY.md, and state/goals/history/ are non-empty. Output PASS: N/N or list failures. Test with python3 scripts/smoke_test_loop.py. Commit."
}
"""


def test_next_open_goal_as_backlog_task(tmp_path: Path):
    (tmp_path / "todo.md").write_text(TODO, encoding="utf-8")
    task = _next_open_goal_as_backlog_task(tmp_path)
    assert task is not None
    assert task["title"] == "Approval truth normalization"
    assert task["priority"] == 1
    assert "recompute approval freshness from apply.ok" in task["instructions"]
    # must stop at the next item (not bleed into goal 2)
    assert "Next goal" not in task["instructions"]


def test_no_todo_returns_none(tmp_path: Path):
    assert _next_open_goal_as_backlog_task(tmp_path) is None


def test_materialized_artifact_routes_goal_as_implement_and_commit(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    workspace = tmp_path
    (workspace / "todo.md").write_text(TODO, encoding="utf-8")

    path = _write_materialized_improvement_artifact(
        state_root=state_root,
        cycle_id="cycle-abc",
        goal_id="goal-bootstrap",
        current_task_id=MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
        summary="s",
        reward_signal={"value": 0.8},
        feedback_decision={"mode": "synthesize_next_candidate"},
        workspace=workspace,
    )
    assert path is not None
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    nbc = artifact["next_bounded_candidate"]
    # the subagent's concrete task is now OUR goal, with implement-and-commit acceptance
    assert nbc["title"] == "Approval truth normalization"
    assert "Implement and commit" in nbc["acceptance"]
    assert "recompute approval freshness" in (nbc["backlog_instructions"] or "")
    assert "Implement Priority 1" in artifact["recommended_next_action"]


def test_parse_backlog_task_from_goal_text_real_host_format(tmp_path: Path):
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / "goal_text.json").write_text(GOAL_TEXT_JSON, encoding="utf-8")

    task = _parse_backlog_task_from_goal_text(tmp_path)
    assert task is not None
    assert task["priority"] == 5
    assert "cycle_logger.py" in task["title"]
    assert "append_cycle_summary" in task["instructions"]
    assert task["source"] == "goal_text"


def test_parse_backlog_task_from_goal_text_missing_file_returns_none(tmp_path: Path):
    assert _parse_backlog_task_from_goal_text(tmp_path) is None


def test_parse_backlog_task_from_goal_text_no_priority_section_returns_none(tmp_path: Path):
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / "goal_text.json").write_text(
        json.dumps({"text": "just a plain goal description, no priority targets here."}),
        encoding="utf-8",
    )
    assert _parse_backlog_task_from_goal_text(tmp_path) is None


def test_materialized_artifact_routes_goal_text_over_research_feed(tmp_path: Path):
    state_root = tmp_path / "state"
    goals_dir = state_root / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "goal_text.json").write_text(GOAL_TEXT_JSON, encoding="utf-8")

    research_dir = state_root / "research"
    research_dir.mkdir(parents=True)
    (research_dir / "feed.json").write_text(
        json.dumps({"entries": [{"title": "stale research candidate", "action": "do something vague"}]}),
        encoding="utf-8",
    )

    # No todo.md / no eeebot-self-evolving MEMORY.md — MEMORY.md exhausted.
    path = _write_materialized_improvement_artifact(
        state_root=state_root,
        cycle_id="cycle-goal-text",
        goal_id="goal-bootstrap",
        current_task_id=MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
        summary="s",
        reward_signal={"value": 0.8},
        feedback_decision={"mode": "synthesize_next_candidate"},
        workspace=tmp_path,
    )
    assert path is not None
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    nbc = artifact["next_bounded_candidate"]
    assert "cycle_logger.py" in nbc["title"]
    assert nbc["backlog_priority"] == 5
    assert "stale research candidate" not in json.dumps(artifact)
