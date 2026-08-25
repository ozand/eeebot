from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.bridge import build_task


def test_memory_skill_documents_index_layout():
    path = Path(__file__).parents[1] / "nanobot" / "skills" / "memory" / "SKILL.md"
    text = path.read_text(encoding="utf-8")

    assert "memory/index.md" in text
    assert "memory/facts/*.md" in text
    assert "on demand" in text
    assert "curate" in text.lower()
    assert "Always loaded into your context" not in text
    assert "You don't need to manage this" not in text


def test_previous_attempt_learning_truncates_at_word_boundary(tmp_path: Path):
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        json.dumps({"next_bounded_candidate": {"title": "context-tail-task"}}),
        encoding="utf-8",
    )
    results = tmp_path / "subagents" / "results"
    results.mkdir(parents=True)
    learning = "This learning ends with a complete boundary before the final oversizedwordfragment and then includes enough additional context to exceed the rendering budget"
    (results / "result.json").write_text(
        json.dumps({
            "materialized_from": "bridge_llm_execution",
            "created_at": "2026-08-25T10:00:00Z",
            "commits_pushed": 1,
            "result_status": "completed",
            "key_learnings": [learning],
            "source_artifact": str(artifact),
        }),
        encoding="utf-8",
    )

    request = {
        "task_title": "context-tail-task",
        "request_id": "request-971",
        "cycle_id": "cycle-971",
        "goal_id": "goal-1",
        "source_artifact": str(artifact),
    }
    prompt = build_task(request, "goal", "", state_dir=tmp_path)

    assert "## Previous attempts for this task" in prompt
    assert "additional…" in prompt
    assert "additional c" not in prompt
