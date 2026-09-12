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


# ─── #1533 class-hunt follow-up: _read_live_index reports what it drops ───
#
# This changes no parsing behaviour -- the same rows are accepted and
# rejected as before. It only reports it. See the #1533 comment thread for
# why this was built even though the live index was measured correct: the
# same silent-degrade shape, with no diagnostic, is what let #1533 itself
# go unnoticed since #1071.

_FIXTURE_INDEX = Path(__file__).parent / "fixtures/lesson_index_1533/index.md"


def test_real_live_index_is_complete_with_empty_diagnostics():
    """Non-vacuity, the unmutated direction: the actual live lessons/index.md
    (54 rows, fetched read-only from the deployed host and committed as a
    fixture) still parses to 54 entries, status=complete, diagnostics
    empty -- the same number the live #1533 class-hunt check measured
    through the deployed reader."""
    from nanobot.runtime.lesson_index import _read_live_index

    diagnostics: list[dict[str, str]] = []
    entries, status, reason = _read_live_index(_FIXTURE_INDEX, diagnostics=diagnostics)
    assert len(entries) == 54
    assert status == "complete"
    assert reason is None
    assert diagnostics == []


def test_one_unrecognized_row_produces_partial_with_a_named_diagnostic(tmp_path):
    """Non-vacuity, the mutated direction: the #1533 shape one notch
    smaller -- a recognized-format index where ONE row the parser cannot
    key on sits among otherwise-good rows. Isolated mutated copy, never
    git stash."""
    from nanobot.runtime.lesson_index import _read_live_index

    mutated = tmp_path / "index.md"
    lines = _FIXTURE_INDEX.read_text(encoding="utf-8").splitlines(keepends=True)
    # Corrupt exactly one real row's shape (drop the closing tags cell) --
    # analogous to a v2-schema row leading with the "wrong" first field.
    victim = next(i for i, line in enumerate(lines) if line.startswith("| ["))
    lines[victim] = lines[victim].rstrip("\n").rsplit(" | ", 1)[0] + "\n"
    mutated.write_text("".join(lines), encoding="utf-8")

    diagnostics: list[dict[str, str]] = []
    entries, status, reason = _read_live_index(mutated, diagnostics=diagnostics)
    assert status == "partial"
    assert reason is None
    assert len(entries) == 53
    assert len(diagnostics) == 1
    assert diagnostics[0]["reason"] == "unrecognized_row_shape"


def test_every_row_rejected_is_unavailable_not_an_empty_index(tmp_path):
    """The case the brief names explicitly: an index whose every line was
    rejected must not read as an empty index."""
    from nanobot.runtime.lesson_index import HEADER, _read_live_index

    path = tmp_path / "index.md"
    path.write_text(HEADER + "not a table row at all\nneither is this\n", encoding="utf-8")

    diagnostics: list[dict[str, str]] = []
    entries, status, reason = _read_live_index(path, diagnostics=diagnostics)
    assert entries == []
    assert status == "unavailable"
    assert reason == "malformed"
    assert len(diagnostics) == 2
    assert all(d["reason"] == "unrecognized_row_shape" for d in diagnostics)


def test_genuinely_empty_index_is_complete_not_malformed(tmp_path):
    """The distinction that makes the above meaningful: zero candidate rows
    (a fresh, real, empty catalogue) is `complete`, not `unavailable` --
    only rows that existed and were rejected trip `malformed`."""
    from nanobot.runtime.lesson_index import HEADER, _read_live_index

    path = tmp_path / "index.md"
    path.write_text(HEADER, encoding="utf-8")

    entries, status, reason = _read_live_index(path)
    assert entries == []
    assert status == "complete"
    assert reason is None


def test_unreadable_file_is_unavailable_with_a_reason(tmp_path):
    from nanobot.runtime.lesson_index import _read_live_index

    entries, status, reason = _read_live_index(tmp_path / "does_not_exist.md")
    assert entries == []
    assert status == "unavailable"
    assert reason == "read_error"


def test_each_rejection_reason_is_named_distinctly():
    """The four ways a shaped-but-invalid row is dropped inside
    _parse_index_text, each with its own diagnostic reason -- not just the
    outer regex mismatch."""
    from nanobot.runtime.lesson_index import HEADER, _parse_index_text

    body = (
        HEADER
        + "| [Bad path](../escape.md) | some prevention text |  |\n"
        + "| [No prevention](ok2.md) |  |  |\n"
        + "| [Bad tag](ok.md) | fine prevention text here | not_a_real_tag |\n"
    )
    diagnostics: list[dict[str, str]] = []
    entries = _parse_index_text(body, source="lesson_index", diagnostics=diagnostics)
    assert entries == []
    reasons = [d["reason"] for d in diagnostics]
    assert "invalid_filename" in reasons
    assert "field_out_of_bounds" in reasons
    assert "uncontrolled_tag" in reasons


def test_diagnostics_are_capped_not_a_log(tmp_path):
    from nanobot.runtime.lesson_index import HEADER, _MAX_INDEX_DIAGNOSTICS, _read_live_index

    path = tmp_path / "index.md"
    body = HEADER + "".join(f"not a row {i}\n" for i in range(_MAX_INDEX_DIAGNOSTICS + 15))
    path.write_text(body, encoding="utf-8")

    diagnostics: list[dict[str, str]] = []
    _read_live_index(path, diagnostics=diagnostics)
    assert len(diagnostics) == _MAX_INDEX_DIAGNOSTICS


def test_diagnostics_omitted_by_default_changes_nothing():
    """Callers that don't ask for diagnostics get exactly the same entries,
    status, and reason as callers that do -- opt-in, not a behavior change."""
    from nanobot.runtime.lesson_index import _read_live_index

    with_diag = _read_live_index(_FIXTURE_INDEX, diagnostics=[])
    without_diag = _read_live_index(_FIXTURE_INDEX)
    assert with_diag == without_diag
