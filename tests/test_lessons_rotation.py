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
