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
    solution_is_meaningful,
    validate_lesson,
    validate_lesson_for_mint,
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
        "timestamp": "2026-08-31T12:00:00Z",
        "summary": "Reflector found an inefficient parser path",
        "recommendations": [{"kind": "approach_hint", "detail": "Use bounded parser reads incrementally for large files"}],
    }) + "\n", encoding="utf-8")
    import os
    old_time = time.time() - 10
    os.utime(state / "reflections.jsonl", (old_time, old_time))
    assert promote_reflector_recommendations_to_v2(workspace, state.parent.parent, max_items=2) == 1
    lessons = yaml.safe_load((workspace / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))
    assert validate_lesson_for_mint(lessons["lessons"][0])
    assert lessons["lessons"][0]["solution"] == "Use bounded parser reads incrementally for large files"


def test_solution_validator_rejects_reflector_template() -> None:
    assert not solution_is_meaningful(
        "A concrete parser issue was observed",
        "Apply the reflected approach hint.",
    )


def test_solution_validator_rejects_trivial() -> None:
    # Meaningful_chars = 5 (less than 12)
    assert not solution_is_meaningful("Database crashes on load.", "Fix it.")


def test_solution_validator_rejects_near_duplicate() -> None:
    # Problem and solution are near duplicates (Jaccard >= 0.8)
    p = "The server crashes on load with a segmentation fault at address 0x0."
    s = "Server crashes on load with a segmentation fault at address 0x0."
    assert not solution_is_meaningful(p, s)


def test_curator_folds_duplicate_and_upgrades_meaningless_solution(tmp_path: Path) -> None:
    from nanobot.runtime.knowledge_curator import promote_reflector_recommendations_to_v2
    import yaml

    # State path needs to have a file at reflector/reflections.jsonl
    state_dir = tmp_path / "state"
    reflector_dir = state_dir / "reflector"
    reflector_dir.mkdir(parents=True)
    source = reflector_dir / "reflections.jsonl"
    source.write_text('{"phase":"reflect","summary":"Node missing","recommendations":[{"kind":"error_pattern","detail":"Run apt-get update to fix missing package listings."}]}\n', encoding="utf-8")

    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir(parents=True)
    target = lessons_dir / "lessons.yaml"
    target.write_text(yaml.dump({
        "lessons": [{
            "id": "LESS-000000000000-0000-0000-0000-000000000000",
            "problem": "Node missing",
            "solution": "Apply the reflected error pattern.",
            "seen_count": 1,
            "last_seen": "old-date"
        }]
    }), encoding="utf-8")

    count = promote_reflector_recommendations_to_v2(tmp_path, state_dir)

    with target.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    cards = doc.get("lessons", [])
    
    # Assert card count is strictly 1 (deduped rather than proliferating)
    assert len(cards) == 1
    # Assert the meaningless template solution was upgraded to the new concrete one extracted from reflector output
    assert cards[0]["solution"] == "Run apt-get update to fix missing package listings."
    assert cards[0]["seen_count"] == 2


def test_curator_rejects_missing_or_filler_recommendation_detail(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state" / "reflector"
    workspace.mkdir()
    state.mkdir(parents=True)
    rows = [
        {"cycle_id": "cycle-empty", "recommendations": [{"kind": "approach_hint", "detail": ""}]},
        {"cycle_id": "cycle-filler", "recommendations": [{"kind": "error_pattern", "detail": "Apply the reflected error pattern."}]},
    ]
    (state / "reflections.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    assert promote_reflector_recommendations_to_v2(workspace, state.parent.parent, max_items=2) == 0
    assert not (workspace / "lessons" / "lessons.yaml").exists()


def test_prompt_includes_lesson_citation_instruction() -> None:
    task = build_task(
        {"task_title": "Parser task", "request_id": "r", "cycle_id": "c", "goal_id": "g",
         "lessons_context": {"relevant_lesson": {"id": "LESS-1", "title": "Parser", "problem": "bad", "solution": "good"}}},
        "goal", "report",
    )
    assert "[Lesson LESS-1]" in task


def test_write_structured_lesson_persists_concrete_reflector_recommendation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact = {
        "problem": "Parser crashed on malformed nested tokens",
        "tags": ["runtime"],
        "severity": "medium",
        "evidence": ["cycle-resolved"],
        "reusable_insight": "Streaming JSON parser chunks prevents OOM crash",
        "reflector_recommendation": "Use chunked generator streaming instead of reading entire file into memory",
        "reflector_delta": True,
    }
    # When artifact contains concrete reflector recommendation, solution must persist that real recommendation, not template
    assert _write_structured_lesson(
        repo_root=repo,
        cycle_id="cycle-reflector-detail",
        backlog_title="Memory issue",
        files_changed=["parser.py"],
        commits_pushed=1,
        artifact_data=artifact,
    )
    data = yaml.safe_load((repo / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))
    lesson = data["lessons"][0]
    assert lesson["solution"] == "Use chunked generator streaming instead of reading entire file into memory"


def test_write_structured_lesson_rejects_filler_or_empty_or_trivial_solution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # Empty solution
    artifact_empty = {
        "problem": "Connection timeout on remote service call",
        "solution": "",
        "tags": ["runtime"],
        "severity": "medium",
        "evidence": ["cycle-resolved"],
        "reusable_insight": "",
        "delta_evidence": "errors-to-integrated-resolution",
    }
    assert not _write_structured_lesson(
        repo_root=repo,
        cycle_id="cycle-empty",
        backlog_title="Fix timeout",
        files_changed=["client.py"],
        commits_pushed=1,
        artifact_data=artifact_empty,
    )

    # Filler solution like "fixed it", "done", "n/a", etc.
    for filler in ("fixed it", "done", "fixed", "n/a", "ok", "pass", "N/A", "todo"):
        artifact = {
            "problem": "Connection timeout on remote service call",
            "solution": filler,
            "tags": ["runtime"],
            "severity": "medium",
            "evidence": ["cycle-resolved"],
            "reusable_insight": filler,
            "delta_evidence": "errors-to-integrated-resolution",
        }
        written = _write_structured_lesson(
            repo_root=repo,
            cycle_id=f"cycle-filler-{filler.replace('/', '_')}",
            backlog_title="Fix timeout",
            files_changed=["client.py"],
            commits_pushed=1,
            artifact_data=artifact,
        )
        assert not written, f"Expected filler '{filler}' to be rejected"
    assert not (repo / "lessons" / "lessons.yaml").exists()


def test_write_structured_lesson_rejects_problem_equal_or_near_duplicate_solution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # (a) Exact match problem == solution
    artifact_exact = {
        "problem": "Timeout connecting to remote database in /var/log/db.py line 50",
        "solution": "Timeout connecting to remote database in /var/log/db.py line 50",
        "tags": ["runtime"],
        "severity": "medium",
        "evidence": ["cycle-resolved"],
        "reusable_insight": "Timeout connecting to remote database",
        "delta_evidence": "errors-to-integrated-resolution",
    }
    written_exact = _write_structured_lesson(
        repo_root=repo,
        cycle_id="cycle-exact-same",
        backlog_title="DB timeout",
        files_changed=["db.py"],
        commits_pushed=1,
        artifact_data=artifact_exact,
    )
    assert not written_exact

    # (b) Near-duplicate problem and solution (e.g. minor whitespace / case / punctuation differences)
    artifact_near = {
        "problem": "Error connecting to Redis backend at redis://localhost:6379",
        "solution": "Error connecting to Redis backend at redis://localhost:6379!",
        "tags": ["runtime"],
        "severity": "medium",
        "evidence": ["cycle-resolved"],
        "reusable_insight": "Error connecting to Redis backend",
        "delta_evidence": "errors-to-integrated-resolution",
    }
    written_near = _write_structured_lesson(
        repo_root=repo,
        cycle_id="cycle-near-same",
        backlog_title="Redis connection",
        files_changed=["redis.py"],
        commits_pushed=1,
        artifact_data=artifact_near,
    )
    assert not written_near
    assert not (repo / "lessons" / "lessons.yaml").exists()


def test_write_structured_lesson_valid_v2_lesson_remains_accepted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact = _base_artifact(
        problem="Database connection pool exhausts under heavy concurrency",
        solution="Increase max pool size and configure aggressive idle connection timeout",
        tags=["runtime"],
        severity="high",
        reusable_insight="Aggressive idle cleanup prevents connection leaks in pool",
        delta_evidence="errors-to-integrated-resolution",
    )
    written = _write_structured_lesson(
        repo_root=repo,
        cycle_id="cycle-valid-v2",
        backlog_title="Tune DB pool",
        files_changed=["pool.py"],
        commits_pushed=1,
        artifact_data=artifact,
    )
    assert written
    data = yaml.safe_load((repo / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))
    assert len(data["lessons"]) == 1
    lesson = data["lessons"][0]
    assert validate_lesson(lesson)
    assert lesson["problem"] == "Database connection pool exhausts under heavy concurrency"
    assert lesson["solution"] == "Increase max pool size and configure aggressive idle connection timeout"
