from __future__ import annotations

import gzip
import os
import time
from pathlib import Path

from nanobot.runtime import lessons_rotation
from nanobot.runtime.lessons_context import build_lessons_context


def _write(path: Path, key: str, count: int) -> list[dict]:
    entries = [{"id": f"{key}-{i}"} for i in range(count)]
    lines = [f"{key}:\n"]
    for entry in entries:
        lines.extend([f"  - id: {entry['id']}\n", "    root_cause: known\n", "    approach: keep me\n", "    reusable_insight: keep me\n"])
    path.write_text("".join(lines), encoding="utf-8")
    return entries


def _stale(path: Path) -> None:
    old = time.time() - 172800
    os.utime(path, (old, old))


def test_stale_lessons_rotation_archives_and_bounds(tmp_path, monkeypatch):
    path = tmp_path / "lessons.yaml"
    entries = _write(path, "lessons", 205)
    _stale(path)
    monkeypatch.setattr(lessons_rotation, "_archive_path", lambda d, s: d / "archive" / f"{s}-old.yaml.gz")
    result = lessons_rotation.rotate_lessons_file(path)
    assert result == "archive/lessons-old.yaml.gz"
    assert path.read_text().count("  - id:") <= 200
    with gzip.open(tmp_path / "archive" / "lessons-old.yaml.gz", "rt", encoding="utf-8") as fh:
        archived = fh.read()
    assert "lessons-204" in archived and "lessons-0" not in archived
    assert entries[0]["id"] not in archived


def test_rotation_is_idempotent_and_archive_write_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "lessons.yaml"
    _write(path, "lessons", 205)
    _stale(path)
    archive = tmp_path / "archive" / "lessons-old.yaml.gz"
    monkeypatch.setattr(lessons_rotation, "_archive_path", lambda d, s: archive)
    assert lessons_rotation.rotate_lessons_file(path)
    active_after_first = path.read_bytes()
    assert lessons_rotation.rotate_lessons_file(path) is None
    assert path.read_bytes() == active_after_first
    assert not list(tmp_path.glob("*.tmp.yaml"))


def test_rotated_live_lessons_still_feed_context(tmp_path):
    repo = tmp_path / "repo"
    path = repo / "lessons" / "lessons.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("lessons:\n  - id: new\n    title: keep rotation\n    approach: keep me\n    reusable_insight: keep me\n", encoding="utf-8")
    context = build_lessons_context(repo, "keep me rotation")
    assert context.get("relevant_lesson", {}).get("id") == "new"


def test_errors_use_same_rotation_mechanism(tmp_path, monkeypatch):
    path = tmp_path / "errors.yaml"
    _write(path, "errors", 205)
    _stale(path)
    monkeypatch.setattr(lessons_rotation, "_archive_path", lambda d, s: d / "archive" / f"{s}-old.yaml.gz")
    assert lessons_rotation.rotate_lessons_directory(tmp_path) == ["archive/errors-old.yaml.gz"]
    assert path.read_text().count("  - id:") <= 200
    assert (tmp_path / "archive" / "errors-old.yaml.gz").exists()


def test_rotation_path_has_no_llm_imports():
    text = Path(lessons_rotation.__file__).read_text(encoding="utf-8")
    assert "openai" not in text.lower()
    assert "litellm" not in text.lower()


def test_rotation_preserves_world_readable_mode(tmp_path, monkeypatch):
    """#988: mkstemp temp files are 0600 and os.replace preserves that mode,
    which locked out non-agent readers (ops-dashboard collector over ssh).
    Rotation must preserve the source file's permission bits and create
    archives world-readable."""
    if os.name == "nt":
        import pytest
        pytest.skip("POSIX permission bits")
    path = tmp_path / "lessons.yaml"
    _write(path, "lessons", 205)
    os.chmod(path, 0o644)
    _stale(path)
    monkeypatch.setattr(lessons_rotation, "_archive_path", lambda d, s: d / "archive" / f"{s}-mode.yaml.gz")
    lessons_rotation.rotate_lessons_file(path)
    assert path.stat().st_mode & 0o777 == 0o644
    archive = tmp_path / "archive" / "lessons-mode.yaml.gz"
    assert archive.stat().st_mode & 0o044 == 0o044


def test_rotation_preserves_custom_mode(tmp_path, monkeypatch):
    """A deliberately non-default mode on the live file survives rotation."""
    if os.name == "nt":
        import pytest
        pytest.skip("POSIX permission bits")
    path = tmp_path / "lessons.yaml"
    _write(path, "lessons", 205)
    os.chmod(path, 0o640)
    _stale(path)
    monkeypatch.setattr(lessons_rotation, "_archive_path", lambda d, s: d / "archive" / f"{s}-custom.yaml.gz")
    lessons_rotation.rotate_lessons_file(path)
    assert path.stat().st_mode & 0o777 == 0o640


def _write_live_format(path: Path, count: int) -> None:
    """Mirror the REAL bridge._write_structured_lesson output: 'lessons:'
    header, entries at 0-indent, nested 2-indent files_changed lists (#991)."""
    lines = ["lessons:\n"]
    for i in range(count):
        lines.extend([
            f"- id: LESS-2026-{i:04d}\n",
            f"  date: '2026-08-{(i % 28) + 1:02d}'\n",
            f"  cycle_id: cycle-{i:012d}\n",
            "  approach: keep me\n",
            "  reusable_insight: keep me\n",
            "  files_changed:\n",
            f"  - scripts/tool_{i}.py\n",
            f"  - tests/test_tool_{i}.py\n",
        ])
    path.write_text("".join(lines), encoding="utf-8")


def test_live_format_rotation_does_not_tear_entries(tmp_path, monkeypatch):
    """#991: a 2-indent '- <file>' line inside files_changed must never be an
    entry boundary. The first live rotation split on those lines and produced
    an archive starting with an orphaned files_changed fragment."""
    path = tmp_path / "lessons.yaml"
    _write_live_format(path, 205)
    _stale(path)
    monkeypatch.setattr(lessons_rotation, "_archive_path", lambda d, s: d / "archive" / f"{s}-live.yaml.gz")
    result = lessons_rotation.rotate_lessons_file(path)
    assert result == "archive/lessons-live.yaml.gz"
    live_text = path.read_text(encoding="utf-8")
    assert live_text.count("- id:") == 200
    # Every live entry keeps BOTH of its files_changed items — nothing torn.
    assert live_text.count("- scripts/tool_") == 200
    assert live_text.count("- tests/test_tool_") == 200
    with gzip.open(tmp_path / "archive" / "lessons-live.yaml.gz", "rt", encoding="utf-8") as fh:
        archived = fh.read()
    # Archived content starts at an entry boundary, not an orphan fragment.
    body = archived.split("lessons:\n", 1)[-1].lstrip("\n")
    assert body.startswith("- id:")
    assert archived.count("- id:") == 5
    assert archived.count("- scripts/tool_") == 5


def test_unrecognized_leading_content_degrades_to_noop(tmp_path, monkeypatch):
    """#991 ambiguity guard: content before the first '- id:' that is not
    whitespace means the format was not identified — rotation must not
    archive an orphan fragment."""
    path = tmp_path / "lessons.yaml"
    lines = ["lessons:\n", "  - stray/fragment.py\n"]
    for i in range(205):
        lines.extend([f"- id: LESS-x-{i:04d}\n", "  approach: keep me\n"])
    path.write_text("".join(lines), encoding="utf-8")
    _stale(path)
    monkeypatch.setattr(lessons_rotation, "_archive_path", lambda d, s: d / "archive" / f"{s}-guard.yaml.gz")
    before = path.read_text(encoding="utf-8")
    lessons_rotation.rotate_lessons_file(path)
    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "archive" / "lessons-guard.yaml.gz").exists()


def test_rotate_index_file_rotates_and_archives_older_entries(tmp_path, monkeypatch):
    """#1041 Part 1: index.md rotates when exceeding cap and archives overflow."""
    path = tmp_path / "index.md"
    lines = ["# Memory Index\n\n"]
    for i in range(250):
        lines.append(f"- [Fact {i}](facts/fact_{i}.md) — description {i}\n")
    path.write_text("".join(lines), encoding="utf-8")
    _stale(path)
    monkeypatch.setattr(lessons_rotation, "_archive_path", lambda d, s: d / "archive" / f"{s}-idx.md.gz")

    result = lessons_rotation.rotate_index_file(path, max_entries=200)
    assert result == "archive/index-idx.md.gz"

    live_text = path.read_text(encoding="utf-8")
    assert live_text.count("- [Fact") == 200
    # Header preserved
    assert live_text.startswith("# Memory Index\n\n")
    # Kept the latest entries (tail)
    assert "- [Fact 249]" in live_text
    assert "- [Fact 0]" not in live_text

    archive_file = tmp_path / "archive" / "index-idx.md.gz"
    assert archive_file.exists()
    with gzip.open(archive_file, "rt", encoding="utf-8") as fh:
        archived = fh.read()
    assert archived.count("- [Fact") == 50
    assert "- [Fact 0]" in archived
    assert "- [Fact 49]" in archived


def test_write_archive_once_merges_multiple_rotations_same_day(tmp_path, monkeypatch):
    """#1041 / Opus review blocker 2: second rotation in same day must merge raw entries, not discard."""
    path = tmp_path / "lessons.yaml"
    _write_live_format(path, 210)
    _stale(path)
    monkeypatch.setattr(lessons_rotation, "_archive_path", lambda d, s: d / "archive" / f"{s}-daily.yaml.gz")

    # First rotation: archives 10 entries (index 200..209 in lessons.yaml) and leaves 200 (0..199)
    res1 = lessons_rotation.rotate_lessons_file(path)
    assert res1 == "archive/lessons-daily.yaml.gz"

    archive_file = tmp_path / "archive" / "lessons-daily.yaml.gz"
    with gzip.open(archive_file, "rt", encoding="utf-8") as fh:
        arch1 = fh.read()
    assert arch1.count("- id: LESS-") == 10
    assert "- id: LESS-2026-0200" in arch1
    assert "- id: LESS-2026-0209" in arch1

    # Add 10 more entries to trigger second rotation on the same day
    with path.open("a", encoding="utf-8") as fh:
        for i in range(210, 220):
            fh.write(f"- id: LESS-new-{i:04d}\n  date: '2026-08-27'\n  approach: keep\n")
    _stale(path)

    # Second rotation: archives the tail 10 entries
    res2 = lessons_rotation.rotate_lessons_file(path)
    assert res2 == "archive/lessons-daily.yaml.gz"

    # Verify merged archive contains entries from BOTH rotations (10 + 10 = 20)
    with gzip.open(archive_file, "rt", encoding="utf-8") as fh:
        arch2 = fh.read()
    assert arch2.count("- id: LESS-") == 20
    assert "- id: LESS-2026-0200" in arch2
    assert "- id: LESS-2026-0209" in arch2
    assert "- id: LESS-new-0210" in arch2
    assert "- id: LESS-new-0219" in arch2


def test_rotate_index_file_splits_at_entry_boundary_on_byte_cap(tmp_path, monkeypatch):
    """#1041 / Opus review blocker 3: byte cap split must respect multi-line entry boundaries."""
    path = tmp_path / "index.md"
    header = "# Facts Catalog\n\nCatalog header text.\n\n"
    # Create 5 entries, each with multiple lines (500 bytes each)
    entries = []
    for i in range(10):
        entry_lines = [
            f"- [Fact {i}](facts/fact_{i}.md) — Title of fact {i}\n",
            f"  Detailed explanation line 1 for fact {i} with long description text...\n",
            f"  Detailed explanation line 2 for fact {i} with more context...\n",
            f"  Detailed explanation line 3 for fact {i} with conclusions...\n",
        ]
        entries.append("".join(entry_lines))

    full_text = header + "".join(entries)
    path.write_text(full_text, encoding="utf-8")
    _stale(path)
    monkeypatch.setattr(lessons_rotation, "_archive_path", lambda d, s: d / "archive" / f"{s}-byte-split.md.gz")

    # Set byte cap small enough that only ~3 newest entries fit
    # Total size ~ 2000 bytes. Cap = 800 bytes.
    result = lessons_rotation.rotate_index_file(path, max_entries=500, max_bytes=800)
    assert result == "archive/index-byte-split.md.gz"

    live_text = path.read_text(encoding="utf-8")
    # Must preserve header
    assert live_text.startswith(header)
    # Must NOT tear any entry (each kept entry must have all 3 detail lines intact)
    for i in range(10):
        if f"- [Fact {i}]" in live_text:
            assert f"Detailed explanation line 1 for fact {i}" in live_text
            assert f"Detailed explanation line 2 for fact {i}" in live_text
            assert f"Detailed explanation line 3 for fact {i}" in live_text
        else:
            assert f"Detailed explanation line 1 for fact {i}" not in live_text

    # Newest entry must be present in live text
    assert "- [Fact 9]" in live_text
    # Oldest entry must be in archive
    archive_file = tmp_path / "archive" / "index-byte-split.md.gz"
    with gzip.open(archive_file, "rt", encoding="utf-8") as fh:
        archived = fh.read()
    assert "- [Fact 0]" in archived
    assert "Detailed explanation line 1 for fact 0" in archived
