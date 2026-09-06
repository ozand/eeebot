from pathlib import Path

import pytest

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
    for task in (
        "Add tabular-nums to the dashboard state table test",
        "Refactor prompt runtime state handling in subagent telemetry",
    ):
        assert lessons_context.build_lessons_context(tmp_path, task) == {}
    monkey_task = "Fix repeat failures in duplicate proposals"
    assert lessons_context.build_lessons_context(tmp_path, monkey_task)["relevant_lesson"]
    from unittest.mock import patch
    with patch.object(lessons_context, "_YAML_OK", False):
        assert lessons_context.build_lessons_context(tmp_path, "Avoiding repeat failures")["relevant_lesson"]


def test_live_numbered_prevention_fixture(tmp_path):
    from nanobot.runtime.lesson_index import generate_index, read_index

    directory = tmp_path / "lessons"
    directory.mkdir()
    source = Path(__file__).parent / "fixtures/lessons/avoid_bundled_test_executions.md"
    (directory / source.name).write_bytes(source.read_bytes())
    assert generate_index(tmp_path)["rows"] == 1
    approach = read_index(directory / "index.md")[0]["approach"]
    assert "Default to targeted, single-suite verification." in approach
    assert not approach.endswith(": 1.")


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


def test_prevention_summary_skips_bare_numbered_list_marker(tmp_path):
    from nanobot.runtime import lesson_index

    directory = tmp_path / "lessons"
    directory.mkdir()
    (directory / "avoid_bundled_test_executions.md").write_text(
        """# Avoid Bundled Test Executions

## Prevention
1. Run bundled test commands only from a disposable project worktree.
2. Prefer focused commands before full suites.
""",
        encoding="utf-8",
    )

    assert lesson_index.generate_index(tmp_path)["rows"] == 1
    text = (directory / "index.md").read_text(encoding="utf-8")
    assert "| [Avoid Bundled Test Executions](avoid_bundled_test_executions.md) | 1. |" not in text
    assert "Run bundled test commands only from a disposable project worktree." in text


def test_degenerate_prevention_marker_is_labelled_unavailable(tmp_path):
    from nanobot.runtime import lesson_index

    directory = tmp_path / "lessons"
    directory.mkdir()
    (directory / "marker_only.md").write_text(
        """# Marker Only

## Prevention
1.
""",
        encoding="utf-8",
    )

    assert lesson_index.generate_index(tmp_path)["rows"] == 1
    entries = lesson_index.read_index(directory / "index.md")
    assert entries[0]["approach"] == "Read lessons/marker_only.md: unavailable: prevention missing"


@pytest.mark.parametrize("prefix", ["  1. ", "1) ", "IV.\n", "First.\n", "### Checklist\n", "**Checklist:**\n"])
def test_prevention_skips_ordinals_and_heading_fragments(tmp_path, prefix):
    from nanobot.runtime import lesson_index

    directory = tmp_path / "lessons"
    directory.mkdir()
    content = "Run each validation command separately before committing changes."
    (directory / "lesson.md").write_text(
        f"# Lesson\n## Prevention\n{prefix}{content}\n", encoding="utf-8"
    )
    lesson_index.generate_index(tmp_path)
    entry = lesson_index.read_index(directory / "index.md")[0]
    assert entry["approach"] == f"Read lessons/lesson.md: {content}"


@pytest.mark.parametrize("body", ["First.", "### Checklist", "**Checklist:**", "IV."])
def test_content_free_prevention_is_unavailable(tmp_path, body):
    from nanobot.runtime import lesson_index

    directory = tmp_path / "lessons"
    directory.mkdir()
    (directory / "lesson.md").write_text(f"# Lesson\n## Prevention\n{body}\n", encoding="utf-8")
    lesson_index.generate_index(tmp_path)
    assert "unavailable: prevention missing" in lesson_index.read_index(directory / "index.md")[0]["approach"]


def test_prevention_summary_truncates_on_word_boundary(tmp_path):
    from nanobot.runtime import lesson_index

    directory = tmp_path / "lessons"
    directory.mkdir()
    long_sentence = " ".join(["word"] * 70) + "."
    (directory / "long_prevention.md").write_text(
        f"""# Long Prevention

## Prevention
{long_sentence}
""",
        encoding="utf-8",
    )

    assert lesson_index.generate_index(tmp_path)["rows"] == 1
    line = next(
        line
        for line in (directory / "index.md").read_text(encoding="utf-8").splitlines()
        if "long_prevention.md" in line
    )
    prevents = line.split(" | ")[1]
    assert len(prevents) <= 240
    assert prevents.endswith("word")
