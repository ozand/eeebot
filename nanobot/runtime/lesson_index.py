"""Bounded, deterministic Markdown lesson catalogue (#1343)."""
from __future__ import annotations

import argparse
import itertools
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote

from nanobot.runtime.schemas import CONTROLLED_LESSON_TAGS
from nanobot.runtime.state_access import lookup_rotated_text, read_rotated_texts

MAX_FILES = 200
MAX_BYTES = 128 * 1024
MAX_INDEX_BYTES = 256 * 1024
# Rotated Markdown indexes are consulted only after the bounded live lookup.
# These limits keep a pathological archive set from becoming an unbounded
# prompt-time read on the constrained host.
_MAX_ARCHIVE_FILES = 7
_MAX_ARCHIVE_BYTES = 256 * 1024
HEADER = "# Lesson index\n\n| lesson | prevents | tags |\n|---|---|---|\n"


@dataclass(frozen=True)
class IndexLookup:
    """Result of resolving one index reference without collapsing read states."""

    entries: tuple[dict, ...]
    status: str  # live, archive, not_found, partial, unavailable
    source: str
    reason: str | None = None


# #1533 class hunt follow-up: bounded, capped diagnostics -- the shape
# knowledge_curator.iter_lessons's archive-failure diagnostics already use
# (#1513), not a log. A signal, not every rejected line.
_MAX_INDEX_DIAGNOSTICS = 10


def _reject_row(diagnostics: list[dict[str, str]] | None, line: str, reason: str) -> None:
    if diagnostics is None or len(diagnostics) >= _MAX_INDEX_DIAGNOSTICS:
        return
    stripped = line.strip()
    if stripped:
        diagnostics.append({"line": stripped[:120], "reason": reason})


def _parse_index_text(
    text: str, *, source: str, diagnostics: list[dict[str, str]] | None = None
) -> list[dict]:
    """Parse complete rows from either the live file or an archive chunk.

    ``diagnostics``, if given, collects up to :data:`_MAX_INDEX_DIAGNOSTICS`
    ``{"line", "reason"}`` entries naming rows this parser could not use --
    ``unrecognized_row_shape`` (the row doesn't match the 3-column pipe-table
    format at all), ``invalid_filename``, ``field_out_of_bounds``, or
    ``uncontrolled_tag`` for a row that matched the shape but failed a later
    check. Purely additive: omitting ``diagnostics`` (the default) changes
    nothing about which rows are accepted or rejected.
    """
    if text.startswith(HEADER):
        text = text[len(HEADER):]
    entries = []
    for line in text.splitlines():
        match = re.fullmatch(r"\| \[(.*?)\]\(([^)]+)\) \| (.*?) \| (.*?) \|", line)
        if not match:
            _reject_row(diagnostics, line, "unrecognized_row_shape")
            continue
        title, filename, prevention, tags = match.groups()
        filename = unquote(filename)
        if '/' in filename or '\\' in filename or not filename.endswith('.md') or filename in {'README.md', 'index.md'}:
            _reject_row(diagnostics, line, "invalid_filename")
            continue
        if not prevention or len(title) > 200 or len(prevention) > 240:
            _reject_row(diagnostics, line, "field_out_of_bounds")
            continue
        tag_list = tags.split(', ') if tags else []
        if not set(tag_list) <= CONTROLLED_LESSON_TAGS:
            _reject_row(diagnostics, line, "uncontrolled_tag")
            continue
        rel = f"lessons/{filename}"
        entries.append({"id": rel, "title": title, "category": tags,
                        "approach": f"Read {rel}: {prevention}", "path": rel})
    return entries


def _read_live_index(
    path: Path, *, diagnostics: list[dict[str, str]] | None = None
) -> tuple[list[dict], str, str | None]:
    """Read the bounded live index.

    Returns ``(entries, status, reason)``, the #1173 vocabulary
    (``complete``/``partial``/``unavailable``) rather than a bare error
    string discarded by every caller. An absent, symlinked, oversized,
    header-mismatched, or unreadable file is ``unavailable`` (unchanged
    behavior, now named). New: a file that reads fine but whose EVERY
    candidate row was rejected by :func:`_parse_index_text` is also
    ``unavailable`` (``reason="malformed"``) rather than a silent, valid-
    looking empty index -- matching
    ``knowledge_lift.read_knowledge_lift_summary``'s
    ``if not rows: return _unavailable_summary("malformed")`` template. A
    file where SOME candidate rows parsed and at least one did not is
    ``partial`` -- ``diagnostics``, when passed, names which and why.

    This changes no parsing behaviour: the same rows are accepted and
    rejected as before, in the same order, and ``entries`` is exactly what
    the pre-#1533-follow-up version returned. Only the second and third
    return values are new; every existing caller here already discarded the
    old single error string.
    """
    try:
        if path.is_symlink() or path.stat().st_size > MAX_INDEX_BYTES:
            return [], "unavailable", "missing_or_oversized"
        text = path.read_text(encoding="utf-8")
        if not text.startswith(HEADER):
            return [], "unavailable", "invalid_header"
        body = text[len(HEADER):]
        candidate_lines = sum(1 for line in body.splitlines() if line.strip())
        entries = _parse_index_text(text, source="lesson_index", diagnostics=diagnostics)
        if len(entries) > MAX_FILES:
            return [], "unavailable", "entry_limit"
        if candidate_lines and not entries:
            return [], "unavailable", "malformed"
        if len(entries) < candidate_lines:
            return entries, "partial", None
        return entries, "complete", None
    except (OSError, UnicodeError, ValueError):
        return [], "unavailable", "read_error"


def _archive_paths(path: Path) -> list[Path]:
    archive_dir = path.parent / "archive"
    try:
        return sorted(
            (item for item in archive_dir.glob(f"{path.stem}-*.md.gz") if item.is_file()),
            key=lambda item: item.name,
            reverse=True,
        )[:_MAX_ARCHIVE_FILES]
    except (OSError, ValueError):
        return []


def resolve_index_reference(path: Path, filename: str) -> IndexLookup:
    """Resolve one cited lesson filename, live first then newest archives."""
    normalized = unquote(str(filename or "")).replace("\\", "/").rsplit("/", 1)[-1]
    lookup = lookup_rotated_text(
        path,
        archive_glob=f"{Path(path).stem}-*.md.gz",
        matches=lambda text: any(
            str(entry.get("path", "")).rsplit("/", 1)[-1] == normalized
            for entry in _parse_index_text(text, source="lesson_index")
        ),
        max_archives=_MAX_ARCHIVE_FILES,
        max_bytes=_MAX_ARCHIVE_BYTES,
    )
    if lookup.text is not None:
        source = "lesson_index_archive" if lookup.status == "archive" else "lesson_index"
        entries = [
            entry for entry in _parse_index_text(lookup.text, source=source)
            if str(entry.get("path", "")).rsplit("/", 1)[-1] == normalized
        ]
        return IndexLookup(tuple(entries), lookup.status, source)
    if lookup.status == "partial":
        return IndexLookup((), "partial", "lesson_index_archive", "; ".join(lookup.notes))
    if lookup.status == "unavailable":
        return IndexLookup((), "unavailable", "lesson_index", "; ".join(lookup.notes))
    return IndexLookup(
        (), "not_found", "lesson_index",
        f"lesson index reference not in retained archives (beyond retention or never existed): {normalized}",
    )


def read_index_archives(
    path: Path, predicate: Callable[[dict], bool] | None = None
) -> list[dict]:
    """Read bounded archive rows after a live-index lookup has missed.

    Archives are newest-first. With a predicate, stop after the first archive
    containing a matching row; this is the normal targeted lookup path and
    avoids opening older archives once the cited/relevant row is found.
    """
    window = read_rotated_texts(
        path, archive_glob=f"{Path(path).stem}-*.md.gz", include_live=False,
        include_archives=True, max_archives=_MAX_ARCHIVE_FILES, max_bytes=_MAX_ARCHIVE_BYTES,
    )
    entries: list[dict] = []
    for _source, text in window.texts:
        chunk = _parse_index_text(text, source="lesson_index_archive")
        if predicate is not None:
            matches = [entry for entry in chunk if predicate(entry)]
            if matches:
                return matches
            continue
        entries.extend(chunk)
    return entries


def find_index_matches(path: Path, predicate: Callable[[dict], bool]) -> list[dict]:
    """Find relevant rows in the newest archive containing a match.

    This is the bounded miss path for readers that rank an index rather than
    holding an exact filename. The central reader opens archives newest-first
    and stops at the first archive containing a match.
    """
    lookup = lookup_rotated_text(
        path,
        archive_glob=f"{Path(path).stem}-*.md.gz",
        matches=lambda text: any(
            predicate(entry) for entry in _parse_index_text(text, source="lesson_index_archive")
        ),
        max_archives=_MAX_ARCHIVE_FILES,
        max_bytes=_MAX_ARCHIVE_BYTES,
    )
    if lookup.text is None:
        return []
    return [
        entry for entry in _parse_index_text(lookup.text, source="lesson_index_archive")
        if predicate(entry)
    ]


def _cell(text: str, cap: int = 240) -> str:
    cleaned = " ".join(text.replace("|", " ").replace("[", "(").replace("]", ")").split())
    if len(cleaned) <= cap:
        return cleaned
    truncated = cleaned[:cap]
    last_space = truncated.rfind(" ")
    return truncated[:last_space] if last_space > 0 else truncated


_LIST_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[ivxlcdm]+[.)]|[-*+])(?:[ \t]+|$)", re.I)


def _prevention_summary(text: str) -> str:
    """Skip Markdown scaffolding; select content, not the first punctuation."""
    lines = []
    for raw in text.splitlines():
        line = _LIST_MARKER_RE.sub("", raw).strip()
        if line.startswith("#") or re.fullmatch(r"\*\*[^*]+\*\*:?", line):
            continue
        # A short colon-terminated line is a label, not prevention advice.
        if line.endswith(":") and len(line.split()) < 4:
            continue
        lines.append(line)
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(lines).strip()):
        sentence = sentence.strip()
        if len(sentence) >= 15 and len(re.findall(r"[^\W\d_]+", sentence)) >= 3:
            return sentence
    return "unavailable: prevention missing"


def generate_index(workspace: Path) -> dict:
    """Never rewrite lesson bodies; publish atomically or preserve the old index."""
    directory = Path(workspace) / "lessons"
    try:
        if not directory.is_dir():
            return {"status": "unavailable", "reason": "missing_directory"}
        paths = list(itertools.islice((p for p in directory.glob("*.md")
                     if p.name not in {"README.md", "index.md"}), MAX_FILES + 1))
        if len(paths) > MAX_FILES:
            return {"status": "unavailable", "reason": "file_count_limit"}
        rows = []
        for path in sorted(paths):
            title, prevents, tags = path.stem, "unavailable: prevention missing", []
            try:
                if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                    raise ValueError("unsafe or oversized lesson")
                text = path.read_text(encoding="utf-8")
                heading = re.search(r"^# (.+)$", text, re.M)
                if heading:
                    title = heading[1]
                section = re.search(r"^## (?:Prevention|Prevention Mechanisms|Reusable Insight)\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
                if section and section[1].strip():
                    prevents = _prevention_summary(section[1])
                # Headings describe topics; incidental body mentions must not
                # gain double-weight category relevance in the prompt ranker.
                headings = " ".join(re.findall(r"^#{1,2} (.+)$", text, re.M))
                words = set(re.findall(r"[a-z0-9]+", headings.lower()))
                tags = sorted(tag for tag in CONTROLLED_LESSON_TAGS
                              if set(re.findall(r"[a-z0-9]+", tag.lower())) <= words)
            except (OSError, UnicodeError, ValueError):
                prevents = "unavailable: unreadable or oversized lesson"
            rows.append(f"| [{_cell(title, 200)}]({quote(path.name, safe='')}) | {_cell(prevents)} | {', '.join(tags)} |\n")
        content = HEADER + "".join(rows)
        if len(content.encode()) > MAX_INDEX_BYTES:
            return {"status": "unavailable", "reason": "index_size_limit"}
        target = directory / "index.md"
        temp = directory / ".index.md.tmp"
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, target)
        return {"status": "ok", "rows": len(rows)}
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "io_error"}


def read_index(
    path: Path, *, reference: str | None = None, include_archives: bool = False
) -> list[dict]:
    """Read the bounded live index, optionally followed by retained archives.

    The default remains the cheap live-only read. Callers that need the complete
    retained catalogue opt in explicitly; a cited reference always uses the
    exact live-first/archive-on-miss resolver and can inspect its status via
    :func:`resolve_index_reference`.
    """
    if reference is not None:
        return list(resolve_index_reference(path, reference).entries)
    entries, _status, _reason = _read_live_index(Path(path))
    if include_archives and entries:
        return entries + read_index_archives(path)
    if include_archives and not entries:
        return read_index_archives(path)
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("TARGET_WORKSPACE", ".")))
    args = parser.parse_args()
    print({"workspace": str(args.workspace.resolve()), **generate_index(args.workspace)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
