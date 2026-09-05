"""#1309: a single lesson larger than the curator's input budget must not block
the queue forever.

Under #1307 the curator keeps the oldest complete prefix that fits and keys the
watermark off the last retained item. An item that is over the budget on its
own can never head a retained prefix, so `fit_lessons_to_input_budget` returned
`[]`, `run_curation` took the empty early return, and the watermark never
moved: every run picked the same item and stopped, honestly. Measured on the
live stream on 2026-09-05: 993 items, max 2,113 chars against 48,000 — a guard
against a theoretical shape, so the cheapest correct arm: quarantine the item
(durable row with id and size), advance the watermark past it, continue.
"""
from __future__ import annotations

import json

from nanobot.runtime import knowledge_curator as curator
from nanobot.runtime.knowledge_curator import run_curation

BUDGET = curator.MAX_INPUT_CHARS


def _lesson(i: int, size: int) -> dict:
    return {"id": f"L{i:02d}", "timestamp": f"2026-09-05T00:{i:02d}:00Z", "approach": "x" * size}


def _wire(monkeypatch, lessons: list[dict]):
    """A watermark-honouring lessons_after over an in-memory stream, and an LLM
    that acknowledges every item it is shown and records what it saw."""
    def fake_lessons_after(_workspace, watermark, *, limit, state_dir):
        start = 0
        if watermark:
            start = next((i + 1 for i, item in enumerate(lessons) if curator._entry_key(item) == watermark), 0)
        return lessons[start:start + limit]

    seen: list[list[str]] = []

    def llm(messages, _model):
        body = messages[1]["content"].split("NEW LESSONS:\n", 1)[1].split("\n\nKB INDEXES:", 1)[0]
        batch = json.loads(body)
        seen.append([item["id"] for item in batch])
        return json.dumps([{"action": "unimportant", "lesson_id": item["id"], "reason": "t"} for item in batch])

    monkeypatch.setattr(curator, "lessons_after", fake_lessons_after)
    monkeypatch.setattr(curator, "promote_reflector_recommendations_to_v2", lambda *_a, **_k: {})
    return llm, seen


def _read(state, name):
    return json.loads((state / "curator" / name).read_text(encoding="utf-8"))


def test_quarantine_helper_takes_only_the_oversized_head():
    big, small = _lesson(0, BUDGET + 10), _lesson(1, 100)
    remaining, quarantined, blocked = curator._quarantine_oversized_head([big, big | {"id": "L09"}, small, _lesson(3, BUDGET + 10)], BUDGET)
    assert [item["id"] for item, _ in quarantined] == ["L00", "L09"]
    assert all(size > BUDGET for _, size in quarantined)
    assert [item["id"] for item in remaining] == ["L01", "L03"], "an oversized item behind a fitting one is deferred, not quarantined"
    assert blocked == ""
    assert curator._quarantine_oversized_head([small], BUDGET) == ([small], [], "")


def test_oversized_head_is_quarantined_and_the_rest_is_curated(tmp_path, monkeypatch):
    """Fails on main: the LLM is never called, watermark.json is never written."""
    lessons = [_lesson(0, BUDGET + 500), _lesson(1, 200), _lesson(2, 200)]
    llm, seen = _wire(monkeypatch, lessons)
    state = tmp_path / "state"

    result = run_curation(tmp_path, state, llm=llm)

    assert seen == [["L01", "L02"]], "the two fitting lessons reached the model in one batch"
    assert result["processed"] == 2 and result["ok"] is True
    assert _read(state, "watermark.json")["last_processed_id"] == "L02"
    rows = [json.loads(line) for line in (state / "curator" / "quarantine.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["lesson_id"] == "L00" and rows[0]["chars"] > BUDGET == rows[0]["budget"]
    assert rows[0]["reason"] == "single item exceeds MAX_INPUT_CHARS" and rows[0]["timestamp"]
    status = _read(state, "status.json")["curation"]
    assert status["status"] == "ok" and status["quarantined"] == 1 and status["quarantined_ids"] == ["L00"]

    second = run_curation(tmp_path, state, llm=llm)
    assert second["processed"] == 0 and len(seen) == 1, "nothing left; the quarantined item is not re-offered"


def test_only_oversized_items_advance_the_watermark_without_an_llm_call(tmp_path, monkeypatch):
    lessons = [_lesson(0, BUDGET + 1), _lesson(1, BUDGET + 1)]
    llm, seen = _wire(monkeypatch, lessons)
    state = tmp_path / "state"

    result = run_curation(tmp_path, state, llm=llm)

    assert seen == [] and result["processed"] == 0 and result["ok"] is True
    assert _read(state, "watermark.json")["last_processed_id"] == "L01"
    assert _read(state, "watermark.json")["quarantined"] == ["L00", "L01"]
    rows = (state / "curator" / "quarantine.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(r)["lesson_id"] for r in rows] == ["L00", "L01"]
    status = _read(state, "status.json")["curation"]
    assert status["status"] == "quarantined" and status["quarantined"] == 2 and status["omitted"] == 0
    assert run_curation(tmp_path, state, llm=llm)["processed"] == 0 and len(rows) == 2, "a second run does not re-quarantine"


def test_quarantine_is_recorded_before_the_llm_call_and_survives_its_failure(tmp_path, monkeypatch):
    lessons = [_lesson(0, BUDGET + 1), _lesson(1, 200)]
    _wire(monkeypatch, lessons)
    state = tmp_path / "state"

    def dead_llm(*_a):
        raise RuntimeError("gateway 500")

    result = run_curation(tmp_path, state, llm=dead_llm)

    assert result["ok"] is False
    assert _read(state, "watermark.json")["last_processed_id"] == "L00", "past the quarantined item, not past the unseen L01"
    assert json.loads((state / "curator" / "quarantine.jsonl").read_text(encoding="utf-8"))["lesson_id"] == "L00"


def test_oversized_head_without_any_key_is_the_one_honest_stall(tmp_path, monkeypatch):
    lessons = [{"approach": "x" * (BUDGET + 1)}, _lesson(1, 200)]
    llm, seen = _wire(monkeypatch, lessons)
    state = tmp_path / "state"

    result = run_curation(tmp_path, state, llm=llm)

    assert seen == [] and result["processed"] == 0
    assert not (state / "curator" / "watermark.json").exists(), "a keyless item cannot be passed by a scalar cursor"
    assert not (state / "curator" / "quarantine.jsonl").exists()
    status = _read(state, "status.json")["curation"]
    assert status["status"] == "partial" and "no id or timestamp" in status["blocked_reason"]


def test_deferred_oversized_item_is_quarantined_on_the_next_run(tmp_path, monkeypatch):
    """Behind a fitting prefix the oversized item is deferred (#1307); it heads the next run and is quarantined then."""
    lessons = [_lesson(0, 200), _lesson(1, BUDGET + 1), _lesson(2, 200)]
    llm, seen = _wire(monkeypatch, lessons)
    state = tmp_path / "state"

    first = run_curation(tmp_path, state, llm=llm)
    assert seen[0] == ["L00"] and first["omitted"] == 2 and "quarantined" not in _read(state, "status.json")["curation"]
    assert _read(state, "watermark.json")["last_processed_id"] == "L00"

    second = run_curation(tmp_path, state, llm=llm)
    assert seen[1] == ["L02"] and second["processed"] == 1
    assert [json.loads(r)["lesson_id"] for r in (state / "curator" / "quarantine.jsonl").read_text(encoding="utf-8").splitlines()] == ["L01"]
    assert _read(state, "watermark.json")["last_processed_id"] == "L02"
