"""#1768 Part 2: the harness-side lesson-citation census writer.

``lesson_v2.lesson_zero_citation_census`` (ADR-021 rule 3 evidence) already
existed and is fully tested in ``tests/test_lesson_v2.py``. What #1768 adds
is persistence: a file the operator (or a dashboard reading
``state/demand/``) can find the same way ``state/demand/skill_census.json``
already works, written by the harness, never the loop. These tests cover
only the new writer, :func:`lesson_v2.write_lesson_citation_census` --
its file shape, and the "no data" vs "measured, found nothing idle" vs
"measured, found idle lessons" distinction, mirrored end to end through the
written JSON rather than just the in-memory census result.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.lesson_v2 import (
    LESSON_CENSUS_REL,
    LESSON_CENSUS_SCHEMA,
    record_citations,
    write_lesson_citation_census,
)


def test_missing_source_writes_unavailable_never_empty_as_all_idle(tmp_path: Path) -> None:
    """Evidence discipline, non-negotiable: 'no data' must never be
    published as 'never cited'. No lesson_usage/ directory at all -> the
    written file says ok: False with an empty zero_citation list, not
    ok: True with every lesson reported idle."""
    state = tmp_path / "state"
    result = write_lesson_citation_census(state)
    assert result["ok"] is False
    assert result["written"] == 0

    path = state / LESSON_CENSUS_REL
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == LESSON_CENSUS_SCHEMA
    assert payload["ok"] is False
    assert payload["reason"] == "missing"
    assert payload["lessons_offered"] == 0
    assert payload["zero_citation"] == []
    assert isinstance(payload["written_at"], str) and payload["written_at"]
    assert payload["window_days"] > 0


def test_real_scan_with_nothing_offered_is_ok_true_empty(tmp_path: Path) -> None:
    """A real, even if empty, scan is evidence -- distinct from no data at
    all, per the same rule the skill census applies."""
    state = tmp_path / "state"
    record_citations(state, "cycle-1", ["no marker"])
    result = write_lesson_citation_census(state)
    assert result["ok"] is True
    assert result["written"] == 0

    payload = json.loads((state / LESSON_CENSUS_REL).read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["reason"] is None
    assert payload["lessons_offered"] == 0
    assert payload["zero_citation"] == []


def test_zero_citation_lessons_are_written_with_last_offered_and_last_cited(tmp_path: Path) -> None:
    state = tmp_path / "state"
    offered_a = {
        "source": "executor_prompt_context", "status": "present",
        "selected_ids": ["LESS-A"], "selected_count": 1,
    }
    record_citations(state, "cycle-1", ["no marker"], selector_provenance=offered_a)
    record_citations(state, "cycle-2", ["no marker"], selector_provenance=offered_a)

    result = write_lesson_citation_census(state)
    assert result["ok"] is True
    assert result["written"] == 1

    payload = json.loads((state / LESSON_CENSUS_REL).read_text(encoding="utf-8"))
    assert payload["lessons_offered"] == 1
    [row] = payload["zero_citation"]
    assert row["lesson_id"] == "LESS-A"
    assert row["last_cited"] is None
    assert row["last_offered"]  # a real timestamp, not None: it WAS offered


def test_write_error_fails_open_never_raises(tmp_path: Path, monkeypatch) -> None:
    import nanobot.runtime.lesson_v2 as lesson_v2

    state = tmp_path / "state"
    record_citations(state, "cycle-1", ["no marker"])

    def broken_mkdir(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", broken_mkdir)
    result = lesson_v2.write_lesson_citation_census(state)
    assert result == {"ok": False, "written": 0, "path": str(state / LESSON_CENSUS_REL)}
