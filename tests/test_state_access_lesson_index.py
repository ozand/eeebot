from __future__ import annotations

import gzip
from pathlib import Path

from nanobot.runtime import state_access


def test_lookup_rotated_text_is_live_first_and_archive_on_miss(tmp_path: Path):
    path = tmp_path / "lessons" / "index.md"
    (path.parent / "archive").mkdir(parents=True)
    path.write_text("live row", encoding="utf-8")
    with gzip.open(path.parent / "archive" / "index-2026-09-10.md.gz", "wt", encoding="utf-8") as fh:
        fh.write("archived row")

    live = state_access.lookup_rotated_text(
        path, archive_glob="index-*.md.gz", matches=lambda text: "live" in text
    )
    archived = state_access.lookup_rotated_text(
        path, archive_glob="index-*.md.gz", matches=lambda text: "archived" in text
    )

    assert live.status == "live"
    assert archived.status == "archive"
    assert archived.source is not None and archived.source.name == "index-2026-09-10.md.gz"


def test_lookup_rotated_text_reports_not_found_and_partial_distinctly(tmp_path: Path):
    path = tmp_path / "lessons" / "index.md"
    (path.parent / "archive").mkdir(parents=True)
    path.write_text("live row", encoding="utf-8")

    absent = state_access.lookup_rotated_text(
        path, archive_glob="index-*.md.gz", matches=lambda text: "missing" in text
    )
    (path.parent / "archive" / "index-2026-09-10.md.gz").write_bytes(b"bad")
    partial = state_access.lookup_rotated_text(
        path, archive_glob="index-*.md.gz", matches=lambda text: "missing" in text
    )

    assert absent.status == "not_found"
    assert partial.status == "partial"
    assert partial.status != absent.status
