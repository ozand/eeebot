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
from difflib import SequenceMatcher
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
_MIN_SOLUTION_MEANINGFUL_CHARS = 20
_FILLER_SOLUTIONS = frozenset({
    "apply the reflected approach hint.",
    "apply the reflected approach hint",
    "apply the reflected error pattern.",
    "apply the reflected error pattern",
    "fixed",
    "fixed it",
    "done",
    "n/a",
    "na",
    "none",
    "null",
    "ok",
    "pass",
    "todo",
})

# Lateral-links constants (#1095).
_RELATED_CAP = 3  # max slugs per entry
_RELATED_MIN_SHARED_TAGS = 2  # minimum shared glossary tags to auto-link
_RELATED_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
_INLINE_RELATED_RE = re.compile(r"\[\[([^\]]+)\]\]")


def solution_is_meaningful(problem: Any, solution: Any) -> bool:
    """Reject filler and problem-shaped solutions before a v2 lesson is stored."""
    if not isinstance(solution, str) or not solution.strip():
        return False
    normalized = " ".join(solution.casefold().split())
    if normalized in _FILLER_SOLUTIONS or normalized.startswith("apply the reflected "):
        return False
    meaningful_chars = len(re.sub(r"[^\w]+", "", solution, flags=re.UNICODE))
    if meaningful_chars < _MIN_SOLUTION_MEANINGFUL_CHARS:
        return False
    # The solution must teach an action/answer, not repeat the problem.
    return keyword_jaccard(problem, solution) < 0.8


def validate_lesson(card: Any) -> bool:
    """Validate the compatible v2 schema, controlled tags, severity, and evidence."""
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
    if not isinstance(evidence, (list, dict)):
        return False
    related = card.get("related", [])
    return (
        isinstance(related, list)
        and len(related) <= _RELATED_CAP
        and all(isinstance(slug, str) and _RELATED_SLUG_RE.fullmatch(slug.strip()) for slug in related)
    )


# Calibrated on LESS-REF-46e5f4c07cd9-d91f (exact copies), the real
# LESS-20260904-d44ed220 passing card, and avoiding_repeat(ed)_failures.md.
TAUTOLOGY_THRESHOLD = 0.90
DUPLICATE_TITLE_THRESHOLD = 0.65


def _quality_text(value: Any) -> str:
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))[:2000]


def anecdote_only(problem: Any) -> bool:
    """Reject only the explicit cycle/outcome/turn-count narrative grammar.

    A cycle id alongside a concrete failure condition is not itself a defect.
    """
    text = str(problem or "")
    if not re.search(r"\bcycle-[0-9a-f]+\b", text, re.I):
        return False
    text = re.sub(r"\bcycle-[0-9a-f]+\b|files_changed\s*=\s*\[\s*\]|\d+", " ", text, flags=re.I)
    words = set(re.findall(r"[a-z]+", text.lower()))
    narrative = set("in the cycle a an with and after before at ended terminated completed outcome partial failed success successful turns turn files changed no empty was had reached limit limits budget exhausted".split())
    return not (words - narrative)


def mint_quality_reason(card: dict[str, Any], existing: list[dict[str, Any]] = (), *, extending: bool = False) -> dict[str, str] | None:
    if extending:
        return None  # existing-card evidence/count updates are not minting
    for left, right in (("solution", "title"), ("generalized_insight", "title"), ("hypothesis", "problem")):
        a, b = _quality_text(card.get(left)), _quality_text(card.get(right))
        if a and b and SequenceMatcher(None, a, b).ratio() >= TAUTOLOGY_THRESHOLD:
            return {"reason": f"tautology:{left}:{right}"}
    if anecdote_only(card.get("problem")):
        return {"reason": "anecdote_problem"}
    title = _quality_text(card.get("title"))
    for entry in existing[:_MAX_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        other = _quality_text(entry.get("title"))
        # Containment catches expanded headings while preserving unrelated ones.
        a, b = set(title.split()), set(other.split())
        overlap = len(a & b) / min(len(a), len(b)) if a and b else 0
        if len(a & b) >= 2 and overlap >= DUPLICATE_TITLE_THRESHOLD:
            return {"reason": "duplicate", "duplicate_id": str(entry.get("id") or entry.get("path") or entry.get("title"))[:200]}
    return None


def allow_mint(card: dict[str, Any], existing: list[dict[str, Any]], state_dir: Path, *, workspace: Path | None = None, extending: bool = False) -> bool:
    """Record refusal on curator decisions; diagnostic I/O never fails a cycle."""
    entries = list(existing[:_MAX_ENTRIES])
    if workspace is not None:
        from nanobot.runtime.lesson_index import read_index
        entries += read_index(Path(workspace) / "lessons/index.md")
    reason = mint_quality_reason(card, entries, extending=extending)
    if reason is None:
        return True
    try:
        path = Path(state_dir) / "curator/decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now(timezone.utc).isoformat(), "lesson_id": card.get("id"),
               "decision": "mint_rejected", **reason,
               "instruction": "Extend the existing lesson with evidence instead of minting" if reason["reason"] == "duplicate" else "Supply a reusable condition and distinct corrective action"}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return False


def validate_lesson_for_mint(card: Any) -> bool:
    """Apply stricter content checks to a newly minted lesson."""
    return validate_lesson(card) and solution_is_meaningful(card.get("problem"), card.get("solution"))


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


def keyword_set(text: Any) -> frozenset[str]:
    """The normalized keyword set :func:`keyword_jaccard` compares — exposed so
    a caller comparing one text against many can normalize each text once
    (#1171: the reflector mint compares every recommendation against every
    card and pool entry; two regex passes per pair would dominate the run)."""
    return frozenset(_WORD_RE.findall(normalize_problem(text)))


def set_jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def keyword_jaccard(first: Any, second: Any) -> float:
    return set_jaccard(keyword_set(first), keyword_set(second))


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
    """Return a stable entry slug from an id/slug/path, or empty string."""
    value = str(entry.get("id") or entry.get("slug") or "").strip()
    if value:
        return value
    path = str(entry.get("path") or "").replace("\\", "/").strip()
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0] if path else ""


def _related_values(value: Any) -> list[str]:
    """Normalize related values without rejecting future/unknown slugs."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        slug = str(raw or "").strip()
        if not slug or slug in seen or not _RELATED_SLUG_RE.fullmatch(slug):
            continue
        seen.add(slug)
        result.append(slug)
    return result


def inline_related_slugs(*texts: Any) -> list[str]:
    """Extract bounded ``[[slug]]`` references from text, deterministically."""
    result: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for raw in _INLINE_RELATED_RE.findall(str(text or "")):
            slug = raw.strip()
            if slug and slug not in seen and _RELATED_SLUG_RE.fullmatch(slug):
                seen.add(slug)
                result.append(slug)
    return result


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

            # Criterion 1: ≥2 shared glossary tags. V2 entries use the
            # controlled vocabulary; generic KB records may carry arbitrary
            # bounded glossary tags.
            def _tags(e: dict[str, Any]) -> frozenset[str]:
                raw = e.get("tags") or e.get("glossary_tags") or []
                return frozenset(
                    str(tag).strip().lower() for tag in raw
                    if str(tag).strip() and (
                        e.get("schema_version") != 2 or tag in CONTROLLED_LESSON_TAGS
                    )
                )

            shared_tags = _tags(a) & _tags(b)

            # Criterion 2: shared non-empty demand-lineage key. Keep the
            # field list deliberately small and never infer lineage from prose.
            def _lineage(e: dict[str, Any]) -> str:
                for field in ("demand_lineage", "demand_id", "delta_evidence", "cycle_id"):
                    val = str(e.get(field) or "").strip()
                    if val:
                        return val
                return ""

            a_lineage = _lineage(a)
            b_lineage = _lineage(b)
            shared_lineage = bool(a_lineage and a_lineage == b_lineage)

            if (
                (len(shared_tags) >= _RELATED_MIN_SHARED_TAGS or shared_lineage)
                and len(related_map[a_slug]) < _RELATED_CAP
                and len(related_map[b_slug]) < _RELATED_CAP
            ):
                related_map[a_slug].add(b_slug)
                related_map[b_slug].add(a_slug)

    # Collect unknown slugs from explicit and inline links. They are retained
    # and reported, never treated as schema failures.
    unknown: set[str] = set()
    for entry in entries:
        for existing_slug in _related_values(entry.get("related")) + inline_related_slugs(
            entry.get("problem"), entry.get("solution"), entry.get("content"),
            entry.get("title"), entry.get("description"),
        ):
            if existing_slug not in by_slug:
                unknown.add(existing_slug)

    # Mirror known explicit/inline links where the target still has capacity.
    # Unknown targets remain retained on their source entry and are only
    # reported; they never cause validation or writing to fail.
    for left in slugs:
        for right in list(related_map[left]):
            if left not in related_map[right] and len(related_map[right]) < _RELATED_CAP:
                related_map[right].add(left)

    # Build updated entries: shallow-copy each, merge existing + new related,
    # cap at _RELATED_CAP, deterministic ordering.
    updated: list[dict[str, Any]] = []
    for entry in entries:
        slug = _entry_slug(entry)
        copy = dict(entry)
        if slug and slug in related_map:
            # Merge: start from existing related (preserving existing ones first),
            # add newly computed ones, deduplicate, cap.
            existing_related = [
                s for s in _related_values(copy.get("related"))
                if s != slug
            ]
            inline_related = [s for s in inline_related_slugs(
                copy.get("problem"), copy.get("solution"), copy.get("content"),
                copy.get("title"), copy.get("description"),
            ) if s != slug]
            new_related = sorted(related_map[slug])
            merged: list[str] = []
            seen: set[str] = set()
            for s in existing_related + inline_related + new_related:
                if s not in seen:
                    merged.append(s)
                    seen.add(s)
            merged = merged[:_RELATED_CAP]
            if merged:
                copy["related"] = merged
            elif "related" in copy:
                # Keep existing related even if not mechanically linked
                # (they may be manually curated / future entries).
                existing = [s for s in _related_values(copy.get("related")) if s != slug]
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
    slugs = _related_values(entry.get("related"))[:max(0, cap)]
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
