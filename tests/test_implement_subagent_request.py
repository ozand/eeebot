"""Task: the emitted subagent request must direct IMPLEMENT, not just VERIFY.

The bridge subagent does what the request's `task`/`task_title` say. When the
materialized artifact carries a concrete implementable goal (routed from todo.md
into next_bounded_candidate), the request must tell the subagent to implement &
commit it — otherwise the "review to verify the artifact" framing wins and the
subagent (correctly) only reviews, producing no code (changed_files=NONE).
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.coordinator import _write_subagent_request_artifact


def _materialized_artifact(state_root: Path, *, with_goal: bool) -> Path:
    improvements = state_root / "improvements"
    improvements.mkdir(parents=True, exist_ok=True)
    nbc = (
        {
            "title": "Approval truth normalization",
            "backlog_instructions": "Recompute approval freshness from apply.ok and expose ttl.",
            "backlog_priority": 1,
        }
        if with_goal
        else {"title": None, "backlog_instructions": None}
    )
    path = improvements / "materialized-cycle-x.json"
    path.write_text(json.dumps({"next_bounded_candidate": nbc}), encoding="utf-8")
    return path


def _plan(artifact_path: Path) -> dict:
    return {
        "current_task_id": "subagent-verify-materialized-improvement",
        "materialized_improvement_artifact_path": str(artifact_path),
        "tasks": [
            {
                "task_id": "subagent-verify-materialized-improvement",
                "title": "Use one bounded subagent-assisted review to verify the materialized improvement artifact",
            }
        ],
    }


def test_request_directs_implement_when_goal_present(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    art = _materialized_artifact(state_root, with_goal=True)
    out = _write_subagent_request_artifact(
        state_root=state_root, cycle_id="cycle-x", goal_id="goal-bootstrap", current_plan=_plan(art)
    )
    payload = json.loads(Path(out).read_text(encoding="utf-8"))
    assert payload["verification_role"] == "materialized_improvement_implementation"
    assert "Implement and commit" in payload["task_title"]
    assert "Approval truth normalization" in payload["task"]
    assert "Recompute approval freshness" in payload["task"]
    # not the review framing
    assert "review to verify" not in payload["task"].lower()


def test_request_falls_back_to_verify_without_goal(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    art = _materialized_artifact(state_root, with_goal=False)
    out = _write_subagent_request_artifact(
        state_root=state_root, cycle_id="cycle-y", goal_id="goal-bootstrap", current_plan=_plan(art)
    )
    payload = json.loads(Path(out).read_text(encoding="utf-8"))
    assert payload["verification_role"] == "materialized_improvement_review"
    assert "Implement and commit" not in (payload["task_title"] or "")
