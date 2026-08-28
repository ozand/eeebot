"""Acceptance tests for Lesson schema v2 (#1071)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import yaml

from nanobot.runtime.bridge import _write_structured_lesson, build_task
from nanobot.runtime.knowledge_curator import promote_reflector_recommendations_to_v2
from nanobot.runtime.lesson_v2 import (
    bounded_load_yaml,
    find_duplicate,
    keyword_jaccard,
    normalize_problem,
    record_citations,
    validate_lesson,
)
from nanobot.runtime.schemas import CONTROLLED_LESSON_TAGS


def _base_artifact(**extra: object) -> dict:
    value = {
        "problem": "Parser failed on large input",
        "solution": "Process input incrementally",
        "tags": ["runtime"],
        "severity": "medium",
        "evidence": ["cycle-resolved"],
        "reusable_insight": "Incremental processing avoids memory exhaustion.",
        "delta_evidence": "errors-to-integrated-resolution",
    }
    value.update(extra)
    return value


def test_schema_rejects_missing_required_fields_and_unknown_tags() -> None:
    card = _base_artifact(id="LESS-1")
    assert validate_lesson(card)
    assert not validate_lesson({**card, "problem": ""})
    assert not validate_lesson({**card, "solution": ""})
    assert not validate_lesson({**card, "tags": ["not-controlled"]})
    assert set(("runtime", "reflector", "test")) <= CONTROLLED_LESSON_TAGS


def test_normalization_and_dedup() -> None:
    assert normalize_problem("Error in /var/log/app.py line 42 code 500") == "error in line code"
    existing = [{"id": "LESS-1", "problem": "Error in C:/tmp/app.py line 99 code 404"}]
    assert find_duplicate("Error in /var/log/app.py line 42 code 500", existing)
    assert keyword_jaccard("parser timeout failure", "parser timeout failure") == 1.0


def test_plain_success_does_not_mint_but_delta_pair_does(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact = _base_artifact()
    artifact.pop("delta_evidence")
    assert not _write_structured_lesson(
        repo_root=repo, cycle_id="cycle-plain", backlog_title="Parser task",
        files_changed=["scripts/parser.py"], commits_pushed=1, artifact_data=artifact,
    )
    errors = repo / "lessons" / "errors.yaml"
    errors.parent.mkdir()
    errors.write_text(yaml.safe_dump([{"task_id": "Parser task", "reason": "parser failure"}]), encoding="utf-8")
    assert _write_structured_lesson(
        repo_root=repo, cycle_id="cycle-resolved", backlog_title="Parser task",
        files_changed=["scripts/parser.py"], commits_pushed=1, artifact_data=artifact,
    )
    data = yaml.safe_load((repo / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))
    lesson = data["lessons"][0]
    assert validate_lesson(lesson)
    assert lesson["problem"] and lesson["solution"] and lesson["tags"]


def test_dedup_increments_seen_count_without_append(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact = _base_artifact()
    assert _write_structured_lesson(repo_root=repo, cycle_id="cycle-a", backlog_title="Parser", files_changed=[], commits_pushed=1, artifact_data=artifact)
    assert _write_structured_lesson(repo_root=repo, cycle_id="cycle-b", backlog_title="Parser", files_changed=[], commits_pushed=1, artifact_data=artifact)
    data = yaml.safe_load((repo / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))
    assert len(data["lessons"]) == 1
    assert data["lessons"][0]["seen_count"] == 2


def test_citations_are_bounded_and_reporting_only(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cited = record_citations(state, "cycle-1", ["proposal [Lesson LESS-1]", "x" * 100_000])
    assert cited == ["LESS-1"]
    rows = [json.loads(line) for line in (state / "lesson_usage" / "citations.jsonl").read_text().splitlines()]
    assert rows == [{"lesson_id": "LESS-1", "cycle_id": "cycle-1", "ts": rows[0]["ts"]}]


def test_bounded_reader_skips_before_open(tmp_path: Path, monkeypatch) -> None:
    old = tmp_path / "old.yaml"
    old.write_text("lessons:\n  - problem: old\n", encoding="utf-8")
    stamp = time.time() - 100 * 86400
    os.utime(old, (stamp, stamp))
    opened = False
    original = Path.read_text
    def counted(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal opened
        opened = True
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", counted)
    assert bounded_load_yaml(old) == []
    assert not opened


def test_curator_promotes_reflector_delta(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state" / "reflector"
    workspace.mkdir()
    state.mkdir(parents=True)
    (state / "reflections.jsonl").write_text(json.dumps({
        "cycle_id": "cycle-reflect",
        "recommendations": [{"kind": "approach_hint", "detail": "Use bounded parser reads"}],
    }) + "\n", encoding="utf-8")
    assert promote_reflector_recommendations_to_v2(workspace, state.parent.parent, max_items=2) == 1
    lessons = yaml.safe_load((workspace / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))
    assert validate_lesson(lessons["lessons"][0])


def test_prompt_includes_lesson_citation_instruction() -> None:
    task = build_task(
        {"task_title": "Parser task", "request_id": "r", "cycle_id": "c", "goal_id": "g",
         "lessons_context": {"relevant_lesson": {"id": "LESS-1", "title": "Parser", "problem": "bad", "solution": "good"}}},
        "goal", "report",
    )
    assert "[Lesson LESS-1]" in task
