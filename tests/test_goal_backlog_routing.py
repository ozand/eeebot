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


def test_skips_already_implemented_goal(tmp_path):
    import subprocess
    from nanobot.runtime.coordinator import _next_open_goal_as_backlog_task
    # todo with two open goals
    (tmp_path / "todo.md").write_text(
        "- [ ] 1. Approval truth normalization\n"
        "  - Problem: stale approval.\n"
        "- [ ] 2. Experiment status reconciliation\n"
        "  - Problem: status vs outcome.\n",
        encoding="utf-8",
    )
    repo = tmp_path / "selfevo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.py").write_text("x=1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m",
                    "feat: approval truth normalization — recompute freshness"], check=True)
    # P1 implemented in git → selection advances to P2
    task = _next_open_goal_as_backlog_task(tmp_path, repo)
    assert task is not None
    assert "Experiment status reconciliation" in task["title"]
