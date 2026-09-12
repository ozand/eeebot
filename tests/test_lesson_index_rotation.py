from __future__ import annotations

import gzip
from pathlib import Path

from nanobot.runtime import lesson_index, lessons_context


def _table(rows: list[str]) -> str:
    return lesson_index.HEADER + "".join(
        f"| [{title}]({filename}) | {prevention} | {tags} |\n"
        for title, filename, prevention, tags in rows
    )


def _write_index_fixture(tmp_path: Path, *, live_rows: list[str] | None = None) -> Path:
    lessons = tmp_path / "lessons"
    archive = lessons / "archive"
    archive.mkdir(parents=True)
    live_rows = live_rows if live_rows is not None else [
        ("Live Boundary", "live-boundary.md", "Keep active reads bounded.", ""),
        ("Live Parser", "live-parser.md", "Parse the current table first.", ""),
    ]
    (lessons / "index.md").write_text(_table(live_rows), encoding="utf-8")
    with gzip.open(archive / "index-2026-09-10.md.gz", "wt", encoding="utf-8") as fh:
        fh.write(_table([
            ("Archived Oldest", "archived-oldest.md", "Retain the oldest useful boundary.", ""),
            ("Archived Target", "archived-target.md", "Resolve retained references explicitly.", ""),
            ("Archived Newer", "archived-newer.md", "Search newer archive chunks first.", ""),
        ]))
    return lessons / "index.md"


def test_not_found_index_reference_is_distinguishable_before_implementation(tmp_path: Path):
    index = _write_index_fixture(tmp_path)

    result = lesson_index.resolve_index_reference(index, "never-created.md")

    assert result.status != "empty"
    assert result.reason == (
        "lesson index reference not in retained archives "
        "(beyond retention or never existed): never-created.md"
    )


def test_live_index_reference_reports_live_source(tmp_path: Path):
    index = _write_index_fixture(tmp_path)

    result = lesson_index.resolve_index_reference(index, "live-parser.md")

    assert result.status == "live"
    assert result.source == "lesson_index"
    assert result.entries[0]["path"] == "lessons/live-parser.md"


def test_archived_index_reference_reports_archive_source(tmp_path: Path):
    index = _write_index_fixture(tmp_path)

    result = lesson_index.resolve_index_reference(index, "archived-target.md")

    assert result.status == "archive"
    assert result.source == "lesson_index_archive"
    assert result.entries[0]["path"] == "lessons/archived-target.md"
    assert result.entries[0]["approach"].startswith(
        "Read lessons/archived-target.md:"
    )


def test_lessons_context_uses_archived_index_after_live_miss(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
    _write_index_fixture(tmp_path)

    result = lessons_context.build_lessons_context(
        tmp_path, "Resolve retained references explicitly"
    )

    assert result["relevant_lesson"]["id"] == "lessons/archived-target.md"
    assert result["relevant_lesson"]["source"] == "lesson_index_archive"
    assert "Resolve retained references explicitly" in result["relevant_lesson"]["approach"]


def test_curator_index_context_labels_archived_catalog_source(tmp_path: Path):
    from nanobot.runtime.knowledge_curator import _read_index

    _write_index_fixture(tmp_path, live_rows=[])

    context = _read_index(tmp_path)

    assert "lessons/index.md (lesson_index_archive)" in context
    assert "Read lessons/archived-target.md: Resolve retained references explicitly." in context


def test_archive_search_is_non_vacuous_against_live_only_copy(tmp_path: Path):
    index = _write_index_fixture(tmp_path)
    source = Path(lesson_index.__file__).read_text(encoding="utf-8")
    assert "lesson_index_archive" in source
    broken = source.replace(
        'max_archives=_MAX_ARCHIVE_FILES,\n        max_bytes=_MAX_ARCHIVE_BYTES,',
        'max_archives=0,\n        max_bytes=_MAX_ARCHIVE_BYTES,',
        1,
    )
    assert broken != source
    broken_path = tmp_path / "broken_lesson_index.py"
    broken_path.write_text(broken, encoding="utf-8")
    import sys
    import types
    module = types.ModuleType("broken_lesson_index")
    module.__file__ = str(broken_path)
    sys.modules[module.__name__] = module
    exec(compile(broken, str(broken_path), "exec"), module.__dict__)
    namespace = module.__dict__

    result = namespace["resolve_index_reference"](index, "archived-target.md")

    assert result.status != "archive"
    assert result.reason is not None


def test_curator_index_archive_search_is_non_vacuous(tmp_path: Path):
    import sys
    import types

    from nanobot.runtime import knowledge_curator

    _write_index_fixture(tmp_path)
    source = Path(knowledge_curator.__file__).read_text(encoding="utf-8")
    assert "read_index_archives" in source
    broken = source.replace(
        'entries = read_index_archives(lesson_path)',
        'entries = []',
        1,
    )
    assert broken != source
    broken_path = tmp_path / "broken_knowledge_curator.py"
    broken_path.write_text(broken, encoding="utf-8")
    module = types.ModuleType("broken_knowledge_curator")
    module.__file__ = str(broken_path)
    sys.modules[module.__name__] = module
    exec(compile(broken, str(broken_path), "exec"), module.__dict__)

    assert "archived-target.md" not in module.__dict__["_read_index"](tmp_path)
