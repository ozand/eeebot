from pathlib import Path

from nanobot.runtime import lessons_context


def test_real_lesson_index_and_retrieval(tmp_path):
    from nanobot.runtime.lesson_index import generate_index
    directory = tmp_path / "lessons"
    directory.mkdir()
    fixture = Path(__file__).parent / "fixtures/lessons/avoiding_repeat_failures.md"
    (directory / fixture.name).write_bytes(fixture.read_bytes())
    assert generate_index(tmp_path)["rows"] == 1
    first = (directory / "index.md").read_bytes()
    generate_index(tmp_path)
    assert first == (directory / "index.md").read_bytes()
    result = lessons_context.build_lessons_context(tmp_path, "Avoiding repeat failures")
    card = result["relevant_lesson"]
    assert "lessons/avoiding_repeat_failures.md" in card["approach"]
    assert "## 1. Duplicate Script Proposals" not in str(card)


def test_generator_bounds_and_missing_prevention(tmp_path, monkeypatch):
    from nanobot.runtime import lesson_index
    directory = tmp_path / "lessons"
    directory.mkdir()
    (directory / "empty.md").write_text("# Empty lesson\n", encoding="utf-8")
    (directory / "bad.md").write_bytes(b"\xff")
    assert lesson_index.generate_index(tmp_path)["rows"] == 2
    entries = lesson_index.read_index(directory / "index.md")
    assert len(entries) == 2
    assert all("unavailable" in e["approach"] for e in entries)
    before = (directory / "index.md").read_bytes()
    monkeypatch.setattr(lesson_index, "MAX_FILES", 1)
    assert lesson_index.generate_index(tmp_path)["reason"] == "file_count_limit"
    assert before == (directory / "index.md").read_bytes()
    monkeypatch.setattr(lesson_index, "MAX_INDEX_BYTES", 1)
    assert lesson_index.read_index(directory / "index.md") == []


def test_index_fail_open(tmp_path, monkeypatch):
    assert lessons_context.build_lessons_context(tmp_path, "repeat failures") == {}
    directory = tmp_path / "lessons"
    directory.mkdir()
    (directory / "index.md").write_text("broken", encoding="utf-8")
    assert lessons_context.build_lessons_context(tmp_path, "repeat failures") == {}
    monkeypatch.setattr(lessons_context, "_YAML_OK", False)
    assert lessons_context.build_lessons_context(tmp_path, "repeat failures") == {}
