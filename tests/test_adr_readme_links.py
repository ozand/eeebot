"""The ADR index must not name a record that does not exist (#1510).

`docs/adr/README.md` is the only entry point to the decision records, so a row
there is a promise that the file is on this branch.  Rows have been added ahead
of their files before, and nothing reported it: the index rendered normally and
the link 404'd only for a reader who followed it.
"""

from __future__ import annotations

import re
from pathlib import Path

ADR_DIR = Path(__file__).resolve().parents[1] / "docs" / "adr"
README = ADR_DIR / "README.md"

# | [ADR-007](ADR-007-....md) | Title | proposed |
_ROW = re.compile(r"^\|\s*\[(ADR-\d+)\]\(([^)]+)\)\s*\|", re.MULTILINE)


def _rows() -> list[tuple[str, str]]:
    return _ROW.findall(README.read_text(encoding="utf-8"))


def _record_files() -> set[str]:
    return {p.name for p in ADR_DIR.glob("ADR-*.md")}


def test_every_indexed_record_exists() -> None:
    """A row in the index resolves to a file next to it."""
    missing = [
        f"{adr} -> {target}"
        for adr, target in _rows()
        if not (ADR_DIR / target).is_file()
    ]
    assert not missing, f"ADR index links to absent records: {missing}"


def test_every_record_is_indexed() -> None:
    """A record on disk is reachable from the index."""
    linked = {target for _, target in _rows()}
    orphaned = sorted(_record_files() - linked)
    assert not orphaned, f"ADR records absent from the index: {orphaned}"


def test_row_label_matches_its_target() -> None:
    """`[ADR-008](ADR-009-....md)` is a copy-paste slip the two checks above miss."""
    mismatched = [
        f"{adr} -> {target}"
        for adr, target in _rows()
        if not target.startswith(f"{adr}-")
    ]
    assert not mismatched, f"ADR rows point at another record: {mismatched}"


def test_index_has_rows_at_all() -> None:
    """Guards the three checks above against a parser that matches nothing."""
    assert len(_rows()) >= 7
