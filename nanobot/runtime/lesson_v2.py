"""Bounded Lesson schema v2 helpers (#1071).

A v2 lesson is a problem-to-solution record. The module is intentionally
stdlib-first and contains no LLM or timer behavior; callers decide which
validated delta is allowed to mint a lesson.

Lateral links (#1095):
- ``fill_related_links(entries)`` fills ``related`` mechanically:
  entries sharing ≥2 controlled glossary tags, OR sharing a non-empty
  demand-lineage key (``delta_evidence`` or ``cycle_id``), are linked
  symmetrically up to a cap of 3 slugs per entry.
- Unknown slug targets are allowed (future entries); they are reported via
  the returned ``unknown`` set, never rejected.
- No LLM is used; the function is deterministic and bounded.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime.schemas import CONTROLLED_LESSON_TAGS, LESSON_SEVERITIES

_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_FILE_AGE_DAYS = 90
_MAX_ENTRIES = 200
_WORD_RE = re.compile(r"[a-z]{3,}")

# Lateral-links constants (#1095).
_RELATED_CAP = 3  # max slugs per entry
_RELATED_MIN_SHARED_TAGS = 2  # minimum shared controlled glossary tags to auto-link


def validate_lesson(card: Any) -> bool:
    """Validate required v2 fields, controlled tags, severity, and evidence."""
    if not isinstance(card, dict):
        return False
    if not isinstance(card.get("problem"), str) or not card["problem"].strip():
        return False
    if not isinstance(card.get("solution"), str) or not card["solution"].strip():
        return False
    tags = card.get("tags")
    if not isinstance(tags, list) or not tags or any(tag not in CONTROLLED_LESSON_TAGS for tag in tags):
        return False
    if card.get("severity", "medium") not in LESSON_SEVERITIES:
        return False
    evidence = card.get("evidence", [])
    return isinstance(evidence, (list, dict))


def normalize_problem(text: Any) -> str:
    """Lowercase problem text and remove digits, paths, and punctuation."""
    value = str(text or "").lower()
    value = re.sub(r"[a-z]:[\\/][a-z0-9_.\\/-]+", " ", value)
    value = re.sub(r"(?:^|\s)/[a-z0-9_.\\/-]+", " ", value)
    value = re.sub(r"\b[a-z0-9_.-]+/[a-z0-9_.\\/-]+", " ", value)
    value = re.sub(r"\d+", " ", value)
    value = re.sub(r"[^a-z\s]", " ", value)
    return " ".join(value.split())


def problem_hash(text: Any) -> str:
    normalized = normalize_problem(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def keyword_jaccard(first: Any, second: Any) -> float:
    left = set(_WORD_RE.findall(normalize_problem(first)))
    right = set(_WORD_RE.findall(normalize_problem(second)))
    return len(left & right) / len(left | right) if left and right else 0.0


def find_duplicate(problem: Any, entries: list[dict[str, Any]], threshold: float = 0.8) -> dict[str, Any] | None:
    digest = problem_hash(problem)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        existing = entry.get("problem") or entry.get("hypothesis") or entry.get("title")
        if digest and problem_hash(existing) == digest:
            return entry
        if keyword_jaccard(problem, existing) >= threshold:
            return entry
    return None


def bounded_load_yaml(
    path: Path,
    *,
    max_bytes: int = _MAX_FILE_BYTES,
    max_age_days: float = _MAX_FILE_AGE_DAYS,
) -> list[dict[str, Any]]:
    """Stat-check before opening/parsing and return only newest bounded entries."""
    try:
        stat = path.stat()
        if stat.st_size > max_bytes or (
            max_age_days > 0 and time.time() - stat.st_mtime > max_age_days * 86400
        ):
            return []
        import yaml
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value = value.get("lessons") or value.get("errors") or []
        return [item for item in value[:_MAX_ENTRIES] if isinstance(item, dict)] if isinstance(value, list) else []
    except Exception:
        return []


def atomic_write_yaml(path: Path, value: Any) -> None:
    """Write YAML atomically, preserving the existing fail-open boundary."""
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(yaml.safe_dump(value, allow_unicode=True, sort_keys=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _entry_slug(entry: dict[str, Any]) -> str:
    """Return the slug (id) for an entry, or empty string."""
    return str(entry.get("id") or "").strip()


def fill_related_links(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Fill ``related`` lists mechanically for v2 lesson entries (#1095).

    Two entries are related if they share ≥2 controlled glossary tags OR
    share the same non-empty demand-lineage key (``delta_evidence`` or
    ``cycle_id`` field on the entry).  Links are symmetric; each entry's
    ``related`` list is capped at ``_RELATED_CAP`` slugs.

    Entries without a slug (no ``id`` field) are skipped.
    Unknown slug targets (slugs in existing ``related`` lists that do not
    correspond to any entry in the input) are reported via the returned
    ``unknown`` set — never rejected.

    Returns ``(updated_entries, unknown_slugs)``.
    The input list is NOT mutated; a shallow-copy list is returned.
    No LLM calls; deterministic and stdlib-only.
    """
    # Index entries by slug.
    by_slug: dict[str, dict[str, Any]] = {}
    for entry in entries:
        slug = _entry_slug(entry)
        if slug:
            by_slug[slug] = entry

    # For each pair, decide if they are related.
    slugs = list(by_slug.keys())
    # Accumulate new related sets (symmetric).
    related_map: dict[str, set[str]] = {slug: set() for slug in slugs}

    for i in range(len(slugs)):
        for j in range(i + 1, len(slugs)):
            a_slug, b_slug = slugs[i], slugs[j]
            a, b = by_slug[a_slug], by_slug[b_slug]

            # Criterion 1: ≥2 shared controlled glossary tags.
            a_tags = frozenset(
                t for t in (a.get("tags") or []) if t in CONTROLLED_LESSON_TAGS
            )
            b_tags = frozenset(
                t for t in (b.get("tags") or []) if t in CONTROLLED_LESSON_TAGS
            )
            shared_tags = a_tags & b_tags

            # Criterion 2: shared non-empty demand-lineage key.
            def _lineage(e: dict[str, Any]) -> str:
                for field in ("delta_evidence", "cycle_id"):
                    val = str(e.get(field) or "").strip()
                    if val:
                        return val
                return ""

            a_lineage = _lineage(a)
            b_lineage = _lineage(b)
            shared_lineage = bool(a_lineage and a_lineage == b_lineage)

            if len(shared_tags) >= _RELATED_MIN_SHARED_TAGS or shared_lineage:
                related_map[a_slug].add(b_slug)
                related_map[b_slug].add(a_slug)

    # Collect unknown slugs from EXISTING related lists.
    unknown: set[str] = set()
    for entry in entries:
        for existing_slug in (entry.get("related") or []):
            if isinstance(existing_slug, str) and existing_slug.strip():
                if existing_slug.strip() not in by_slug:
                    unknown.add(existing_slug.strip())

    # Build updated entries: shallow-copy each, merge existing + new related,
    # cap at _RELATED_CAP, sorted for determinism.
    updated: list[dict[str, Any]] = []
    for entry in entries:
        slug = _entry_slug(entry)
        copy = dict(entry)
        if slug and slug in related_map:
            # Merge: start from existing related (preserving existing ones first),
            # add newly computed ones, deduplicate, cap.
            existing_related: list[str] = [
                s for s in (copy.get("related") or [])
                if isinstance(s, str) and s.strip() and s.strip() != slug
            ]
            new_related = sorted(related_map[slug])
            merged: list[str] = []
            seen: set[str] = set()
            for s in existing_related + new_related:
                if s not in seen:
                    merged.append(s)
                    seen.add(s)
            merged = merged[:_RELATED_CAP]
            if merged:
                copy["related"] = merged
            elif "related" in copy:
                # Keep existing related even if not mechanically linked
                # (they may be manually curated / future entries).
                existing = [
                    s for s in (copy.get("related") or [])
                    if isinstance(s, str) and s.strip() and s.strip() != slug
                ]
                if existing:
                    copy["related"] = existing[:_RELATED_CAP]
                else:
                    copy.pop("related", None)
        updated.append(copy)
    return updated, unknown


def related_hint(entry: dict[str, Any], *, cap: int = _RELATED_CAP) -> str:
    """Return a compact one-line related hint string for prompt cards/indexes.

    Returns empty string when ``related`` is absent or empty (byte-identical
    prompt for entries without lateral links).
    """
    slugs = [
        s for s in (entry.get("related") or [])
        if isinstance(s, str) and s.strip()
    ][:cap]
    if not slugs:
        return ""
    return "related: " + ", ".join(slugs)


def record_citations(
    state_dir: Path,
    cycle_id: str,
    texts: list[str],
    *,
    max_chars: int = 64_000,
) -> list[str]:
    """Grep bounded proposal/transcript text and atomically record usage rows."""
    ids: set[str] = set()
    pattern = re.compile(r"\[Lesson\s+([A-Za-z0-9_-]+)\]", re.IGNORECASE)
    for text in texts:
        ids.update(pattern.findall(str(text or "")[:max_chars]))
    if not ids:
        return []
    path = Path(state_dir) / "lesson_usage" / "citations.jsonl"
    try:
        rows: list[dict[str, str]] = []
        if path.exists():
            stat = path.stat()
            if stat.st_size <= _MAX_FILE_BYTES:
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                rows = [row for row in rows if isinstance(row, dict)][-_MAX_ENTRIES:]
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rows.extend({"lesson_id": lesson_id, "cycle_id": cycle_id, "ts": now} for lesson_id in sorted(ids))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows[-_MAX_ENTRIES:]))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
        return sorted(ids)
    except Exception:
        return []
