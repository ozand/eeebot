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
    append_curator_decision,
    bounded_load_yaml,
    find_duplicate,
    keyword_jaccard,
    normalize_problem,
    read_citation_scans,
    read_curator_decisions,
    read_executor_result,
    record_citations,
    solution_is_meaningful,
    validate_lesson,
    validate_lesson_for_mint,
)
from nanobot.runtime.schemas import CONTROLLED_LESSON_TAGS


def _materialize_staged_lessons(workspace: Path, state_dir: Path) -> None:
    """#1209: the mint stages its cards; apply them the way the bridge pickup does."""
    from nanobot.runtime.knowledge_curator import (
        _STAGED_DIR,
        LESSONS_KIND,
        apply_staged_lesson_cards,
        load_staged_manifest,
    )
    for entry in load_staged_manifest(state_dir):
        if entry.get("kind") == LESSONS_KIND:
            payload_path = state_dir / "curator" / _STAGED_DIR / entry["payload_file"]
            apply_staged_lesson_cards(workspace, json.loads(payload_path.read_text(encoding="utf-8")))


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
    assert not validate_lesson({**card, "tags": ["reflector"]})
    assert validate_lesson({**card, "tags": ["runtime"], "source": "reflector"})
    assert "runtime" in CONTROLLED_LESSON_TAGS
    assert "curator" in CONTROLLED_LESSON_TAGS
    assert "reflector" not in CONTROLLED_LESSON_TAGS


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


def test_executor_result_citation_boundary_distinguishes_hit_zero_and_absent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    results = state / "subagents"
    results.mkdir(parents=True)
    (results / "hit.json").write_text(json.dumps({"result": "Done [Lesson LESS-HIT]"}), encoding="utf-8")
    (results / "zero.json").write_text(json.dumps({"result": "Done without a citation"}), encoding="utf-8")

    hit = read_executor_result(state, "hit")
    assert hit == "Done [Lesson LESS-HIT]"
    assert record_citations(state, "cycle-hit-result", executor_result=hit) == ["LESS-HIT"]

    zero = read_executor_result(state, "zero")
    assert zero == "Done without a citation"
    assert record_citations(state, "cycle-zero-result", executor_result=zero) == []
    zero_row = read_citation_scans(state)["rows"][-1]
    assert zero_row["status"] == "complete" and zero_row["marker_count"] == 0

    assert read_executor_result(state, "missing") is not None
    assert record_citations(state, "cycle-missing-result", executor_result=read_executor_result(state, "missing")) == []
    missing_row = read_citation_scans(state)["rows"][-1]
    assert missing_row["status"] == "unavailable"
    assert "marker_count" not in missing_row
    assert "lesson_ids" not in missing_row
    assert "executor_result_unavailable" in missing_row["notes"]

    assert "scanned_chars" in zero_row
    assert zero_row["scanned_chars"] == len("Done without a citation")


def test_executor_result_records_length_when_scanned(tmp_path: Path) -> None:
    """#1546: a reader must not have to open the subagent file to tell a
    substantive scan from a near-empty one behind the same status/marker_count.
    """
    state = tmp_path / "state"
    text = "A real answer with plenty of words and " + ("x" * 500)
    assert record_citations(state, "cycle-length", executor_result=text) == []
    row = read_citation_scans(state)["rows"][-1]
    assert row["status"] == "complete"
    assert row["scanned_chars"] == len(text)


def test_cancelled_repair_stub_is_not_reported_as_a_confirmed_zero(tmp_path: Path) -> None:
    """#1546: `bridge.py`'s citation call site can, in principle, resolve to
    a repair-turn spawn (see test_bridge_authoritative_subagent.py for when).
    Whichever spawn is fed in here, one whose own persisted `status` names a
    canned-stub terminal state must never read the same as a real zero —
    that was the whole point of the issue (a cancelled/errored/bounded_stop/
    blocked stub cannot structurally contain a `[Lesson <id>]` marker).
    """
    for status, stub_text in (
        ("cancelled", "Cancelled before completion."),
        ("error", "Error: LLM execution failed: connection reset"),
        ("bounded_stop", "Stopped: wall-clock deadline reached."),
        ("blocked", "Blocked: identical tool-call loop detected."),
    ):
        subagents = tmp_path / "state" / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        task_id = f"stub-{status}"
        (subagents / f"{task_id}.json").write_text(
            json.dumps({"result": stub_text, "status": status}), encoding="utf-8",
        )
        state = tmp_path / "state"
        executor_result = read_executor_result(state, task_id)
        # Not the plain "nothing could be read at all" sentinel, and not a
        # bare string either — a distinct, third outcome carrying what was
        # actually there.
        assert executor_result is not None
        assert not isinstance(executor_result, str)

        cycle_id = f"cycle-{status}-stub"
        assert record_citations(state, cycle_id, executor_result=executor_result) == []
        row = [r for r in read_citation_scans(state)["rows"] if r["cycle_id"] == cycle_id][-1]
        assert row["status"] == "unavailable"
        assert "marker_count" not in row
        assert "lesson_ids" not in row
        assert row["scanned_chars"] == len(stub_text)
        assert "executor_result_not_final" in row["notes"]
        assert f"subagent_status:{status}" in row["notes"]


def test_ok_status_and_missing_status_are_both_still_trusted(tmp_path: Path) -> None:
    """#1546 must not make every result suspicious — an explicit `status: ok`
    and a legacy row with no `status` key at all (written before that field
    existed) both keep reading as a real, scannable answer, unchanged.
    """
    state = tmp_path / "state"
    subagents = state / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    (subagents / "ok-task.json").write_text(
        json.dumps({"result": "Done. [Lesson LESS-OK]", "status": "ok"}), encoding="utf-8",
    )
    (subagents / "legacy-task.json").write_text(
        json.dumps({"result": "Done. [Lesson LESS-LEGACY]"}), encoding="utf-8",
    )

    ok_result = read_executor_result(state, "ok-task")
    assert ok_result == "Done. [Lesson LESS-OK]"
    assert record_citations(state, "cycle-ok-status", executor_result=ok_result) == ["LESS-OK"]

    legacy_result = read_executor_result(state, "legacy-task")
    assert legacy_result == "Done. [Lesson LESS-LEGACY]"
    assert record_citations(state, "cycle-legacy-status", executor_result=legacy_result) == ["LESS-LEGACY"]


def test_citations_are_bounded_and_reporting_only(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cited = record_citations(state, "cycle-1", ["proposal [Lesson LESS-1]", "x" * 100_000])
    assert cited == ["LESS-1"]
    rows = [json.loads(line) for line in (state / "lesson_usage" / "citations.jsonl").read_text().splitlines()]
    assert rows == [{"lesson_id": "LESS-1", "cycle_id": "cycle-1", "ts": rows[0]["ts"]}]


def test_citation_scan_records_zero_result(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert record_citations(state, "cycle-zero", ["no lesson marker here"]) == []

    result = read_citation_scans(state)
    assert result["status"] == "present"
    assert result["rows"][-1]["scan_ran"] is True
    assert result["rows"][-1]["marker_count"] == 0
    assert result["rows"][-1]["cycle_id"] == "cycle-zero"


def test_citation_scan_records_positive_result(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert record_citations(state, "cycle-hit", ["response [Lesson LESS-1] [Lesson LESS-2]"]) == ["LESS-1", "LESS-2"]

    row = read_citation_scans(state)["rows"][-1]
    assert row["scan_ran"] is True
    assert row["marker_count"] == 2
    assert row["cycle_id"] == "cycle-hit"


def test_citation_writer_failure_is_recorded_fail_open(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    original = Path.read_text

    def broken_read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "citations.jsonl":
            raise OSError("citation store unavailable")
        return original(self, *args, **kwargs)

    citations = state / "lesson_usage" / "citations.jsonl"
    citations.parent.mkdir(parents=True)
    citations.write_text("not-json\n", encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", broken_read)

    assert record_citations(state, "cycle-failed", ["[Lesson LESS-1]"]) == []
    row = read_citation_scans(state)["rows"][-1]
    assert row["scan_ran"] is True
    assert row["marker_count"] == 1
    assert row["status"] == "unavailable"
    assert "citation_write_failed" in row["notes"]


def test_curator_decisions_under_bound_do_not_rotate(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    for index in range(3):
        append_curator_decision(
            tmp_path, {"lesson_id": f"L{index}", "decision": "staged"}, now=now
        )
    assert not list((tmp_path / "curator" / "archive").glob("decisions-*.jsonl.gz"))
    assert len((tmp_path / "curator" / "decisions.jsonl").read_text().splitlines()) == 3


def test_curator_decisions_rotate_and_read_beyond_retention_is_unavailable(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    append_curator_decision(tmp_path, {"lesson_id": "old", "decision": "staged"}, now=first)
    append_curator_decision(
        tmp_path, {"lesson_id": "new", "decision": "promoted"}, now=first + timedelta(days=1)
    )
    archives = list((tmp_path / "curator" / "archive").glob("decisions-*.jsonl.gz"))
    assert len(archives) == 1
    assert read_curator_decisions(tmp_path, now=first + timedelta(days=1))["status"] == "present"
    beyond = read_curator_decisions(
        tmp_path, since=first, now=first + timedelta(days=31)
    )
    assert beyond["status"] == "unavailable"
    assert beyond["notes"] == ["beyond_retention"]


def test_curator_decisions_bound_real_shaped_copy(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    path = tmp_path / "curator" / "decisions.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "".join(json.dumps({
            "timestamp": f"2026-08-{index % 12 + 20:02d}T12:00:00Z",
            "lesson_id": f"LIVE-{index}", "decision": "unimportant",
        }) + "\n" for index in range(1_018)),
        encoding="utf-8",
    )
    append_curator_decision(
        tmp_path, {"lesson_id": "LIVE-final", "decision": "staged"},
        now=datetime(2026, 9, 12, tzinfo=timezone.utc),
    )
    active_rows = path.read_text(encoding="utf-8").splitlines()
    import gzip
    archive_rows = sum(
        1 for archive in (path.parent / "archive").glob("decisions-*.jsonl.gz")
        for _ in gzip.open(archive, "rt", encoding="utf-8")
    )
    assert len(active_rows) < 1_018
    assert archive_rows > 0


def test_curator_decision_writers_share_one_path(tmp_path: Path, monkeypatch) -> None:
    import nanobot.runtime.knowledge_curator as curator
    import nanobot.runtime.lesson_v2 as lesson

    calls = []
    original = lesson.append_curator_decision
    def spy(state_dir, row, **kwargs):
        calls.append(row["decision"])
        return original(state_dir, row, **kwargs)
    monkeypatch.setattr(lesson, "append_curator_decision", spy)
    monkeypatch.setattr(curator, "append_curator_decision", spy)
    lesson.allow_mint(_base_artifact(), [], tmp_path)
    curator._write_decision(tmp_path, "L1", "staged", "test")
    assert calls == ["mint_gate_passed", "staged"]


def test_citation_scan_rotation_and_beyond_retention(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from nanobot.runtime import lesson_v2

    state = tmp_path / "state"
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert record_citations(state, "cycle-old", ["none"], now=first) == []
    assert record_citations(state, "cycle-new", ["none"], now=first + timedelta(days=1)) == []
    assert list((state / "lesson_usage" / "archive").glob("scans-*.jsonl.gz"))

    retained = read_citation_scans(state, since=first + timedelta(hours=1), now=first + timedelta(days=1))
    assert retained["status"] == "present"
    assert [row["cycle_id"] for row in retained["rows"]] == ["cycle-new"]

    beyond = read_citation_scans(
        state,
        since=first - timedelta(days=lesson_v2.CITATION_SCAN_RETENTION_DAYS + 1),
        now=first + timedelta(days=1),
    )
    assert beyond["status"] == "unavailable"
    assert "beyond_retention" in beyond["notes"]


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
    # #1171: a recommendation earns a card on recurrence — two cycles, two days.
    rows = [
        {
            "cycle_id": "cycle-reflect",
            "timestamp": "2026-08-31T12:00:00Z",
            "summary": "Reflector found an inefficient parser path",
            "recommendations": [{"kind": "approach_hint", "detail": "Use bounded parser reads incrementally for large files", "evidence": "Whole-file parsing exceeds memory on large inputs"}],
        },
        {
            "cycle_id": "cycle-reflect2",
            "timestamp": "2026-09-01T12:00:00Z",
            "summary": "Reflector found the parser path again",
            "recommendations": [{"kind": "approach_hint", "detail": "Use bounded parser reads incrementally for large files", "evidence": "Whole-file parsing exceeds memory on large inputs"}],
        },
    ]
    (state / "reflections.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    import os
    old_time = time.time() - 10
    os.utime(state / "reflections.jsonl", (old_time, old_time))
    assert promote_reflector_recommendations_to_v2(workspace, state.parent.parent, max_items=2) == 1
    _materialize_staged_lessons(workspace, state.parent.parent)
    lessons = yaml.safe_load((workspace / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))
    assert validate_lesson_for_mint(lessons["lessons"][0])
    assert lessons["lessons"][0]["solution"] == "Use bounded parser reads incrementally for large files"
    assert lessons["lessons"][0]["tags"] == ["runtime"]
    assert lessons["lessons"][0]["source"] == "reflector"


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
    import yaml

    from nanobot.runtime.knowledge_curator import promote_reflector_recommendations_to_v2

    # State path needs to have a file at reflector/reflections.jsonl
    state_dir = tmp_path / "state"
    reflector_dir = state_dir / "reflector"
    reflector_dir.mkdir(parents=True)
    source = reflector_dir / "reflections.jsonl"
    source.write_text('{"phase":"reflect","summary":"Node missing","recommendations":[{"kind":"error_pattern","detail":"Run apt-get update to fix missing package listings.","evidence":"Node missing"}]}\n', encoding="utf-8")

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

    promote_reflector_recommendations_to_v2(tmp_path, state_dir)
    _materialize_staged_lessons(tmp_path, state_dir)

    with target.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    cards = doc.get("lessons", [])

    # Assert card count is strictly 1 (deduped rather than proliferating)
    assert len(cards) == 1
    # Assert the meaningless template solution was upgraded to the new concrete one extracted from reflector output
    assert cards[0]["solution"] == "Run apt-get update to fix missing package listings."
    assert cards[0]["seen_count"] == 2


def _write_reflections_over(path: Path, min_bytes: int, tail_row: dict[str, object] | list[dict[str, object]]) -> int:
    """Write padding rows until *path* exceeds *min_bytes*, then *tail_row* last.

    Padding rows carry no ``recommendations``, so nothing but *tail_row* is
    promotable and the assertions cannot be satisfied by the filler.
    """
    filler = "x" * 800
    written = 0
    index = 0
    tail_rows = tail_row if isinstance(tail_row, list) else [tail_row]
    with path.open("w", encoding="utf-8") as handle:
        while written <= min_bytes:
            # Timestamped like real journal rows so the #1171 cursor can advance
            # through the padding (a run reads a bounded number of rows).
            line = json.dumps({
                "cycle_id": f"cycle-pad-{written}", "summary": filler,
                "timestamp": f"2026-08-01T00:00:{index % 60:02d}.{index // 60:06d}Z",
            }) + "\n"
            handle.write(line)
            written += len(line)
            index += 1
        for row in tail_rows:
            handle.write(json.dumps(row) + "\n")
    return path.stat().st_size


def test_curator_promotes_from_log_larger_than_the_old_size_cap(tmp_path: Path) -> None:
    """#1183: a 512 KiB cap used to discard the whole file and return 0.

    ``reflections.jsonl`` is append-only with no rotation, so on the host it
    crossed that line once (699,393 bytes) and the reflector mint path was off
    permanently. Only the newest rows are ever read, so total size must not
    decide whether the file is readable at all.
    """
    workspace = tmp_path / "workspace"
    state = tmp_path / "state" / "reflector"
    workspace.mkdir()
    state.mkdir(parents=True)
    size = _write_reflections_over(
        state / "reflections.jsonl",
        512 * 1024,
        [
            {
                "cycle_id": "cycle-oversize",
                "timestamp": "2026-09-01T12:00:00Z",
                "summary": "Reflector found an unbounded read on a growing log",
                "recommendations": [
                    {"kind": "approach_hint", "detail": "Read a bounded tail instead of the whole file", "evidence": "Growing logs exhaust reader memory"}
                ],
            },
            {
                "cycle_id": "cycle-oversize2",
                "timestamp": "2026-09-02T12:00:00Z",
                "summary": "Reflector found the unbounded read again",
                "recommendations": [
                    {"kind": "approach_hint", "detail": "Read a bounded tail instead of the whole file", "evidence": "Growing logs exhaust reader memory"}
                ],
            },
        ],
    )
    assert size > 512 * 1024, size

    # #1171: a run reads a bounded number of rows after its cursor and carries
    # the rest over, so a file this size takes two runs to reach its tail —
    # but it is read, where the old size cap returned 0 forever.
    staged = 0
    for _ in range(4):
        staged += promote_reflector_recommendations_to_v2(workspace, state.parent.parent, max_items=2)
        if staged:
            break
    assert staged == 1
    _materialize_staged_lessons(workspace, state.parent.parent)
    lessons = yaml.safe_load((workspace / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))
    assert lessons["lessons"][0]["solution"] == "Read a bounded tail instead of the whole file"


def test_curator_skips_unparseable_row_instead_of_abandoning_the_file(tmp_path: Path) -> None:
    """#1183: one malformed line used to abort the whole promotion.

    In an append-only log that failure is permanent too, so a single bad write
    would silently retire the path in the same way the size cap did.
    """
    workspace = tmp_path / "workspace"
    state = tmp_path / "state" / "reflector"
    workspace.mkdir()
    state.mkdir(parents=True)
    (state / "reflections.jsonl").write_text(
        json.dumps({
            "cycle_id": "cycle-good",
            "timestamp": "2026-09-01T12:00:00Z",
            "summary": "Reflector found a parser that aborts on one bad row",
            "recommendations": [
                {"kind": "error_pattern", "detail": "Skip the unparseable row and keep the rest", "evidence": "One malformed row aborts the entire stream"}
            ],
        })
        + "\n"
        + '{"cycle_id": "cycle-truncated", "recommendations": [\n'
        + json.dumps({
            "cycle_id": "cycle-good2",
            "timestamp": "2026-09-02T12:00:00Z",
            "summary": "Reflector found the aborting parser again",
            "recommendations": [
                {"kind": "error_pattern", "detail": "Skip the unparseable row and keep the rest", "evidence": "One malformed row aborts the entire stream"}
            ],
        })
        + "\n",
        encoding="utf-8",
    )

    assert promote_reflector_recommendations_to_v2(workspace, state.parent.parent, max_items=2) == 1
    _materialize_staged_lessons(workspace, state.parent.parent)
    lessons = yaml.safe_load((workspace / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))
    assert lessons["lessons"][0]["solution"] == "Skip the unparseable row and keep the rest"


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
