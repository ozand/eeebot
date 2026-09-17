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

import gzip
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from nanobot.runtime import state_access
from nanobot.runtime.schemas import CONTROLLED_LESSON_TAGS, LESSON_SEVERITIES

_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_FILE_AGE_DAYS = 90
_MAX_ENTRIES = 200
_CITATION_SCAN_MAX_ROWS = 2_000
_CITATION_SCAN_MAX_ARCHIVES = 16
CITATION_SCAN_RETENTION_DAYS = 14
_CITATION_SCAN_FILE = "scans.jsonl"
_CITATION_ARCHIVE_DIR = "archive"
_CITATION_PROVENANCE_MAX_CARDS = 3
_CITATION_SHORT_RESULT_CHARS = 128
_DECISIONS_FILE = "decisions.jsonl"
_DECISIONS_RETENTION_DAYS = 30
_DECISIONS_MAX_ROWS = 2_000
_DECISIONS_MAX_ARCHIVES = 16
_DECISIONS_MAX_FILE_BYTES = 2 * 1024 * 1024
_DECISIONS_ROTATE_BYTES = 200 * 1024
_EXECUTOR_RESULT_MAX_BYTES = 256 * 1024
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
_MAX_TITLE_DIAGNOSTIC_CHARS = 160
_TITLE_GATE_PASS_REASON = "title_gate_pass"
def _title_gate_reason(
    card: dict[str, Any], existing: list[dict[str, Any]] = (),
) -> dict[str, str] | None:
    """Reject a title that collides with an existing selection key."""
    title = _quality_text(card.get("title"))
    if not title:
        return None
    for entry in existing[:_MAX_ENTRIES]:
        if not isinstance(entry, dict) or not _quality_text(entry.get("title")):
            continue
        if title == _quality_text(entry.get("title")):
            return {
                "reason": "title_gate:duplicate_title",
                "duplicate_id": str(entry.get("id") or entry.get("path") or "")[:200],
            }
    return None


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
# Real avoiding_repeat(ed)_failures Markdown pair: condition containment .25,
# action containment .659.
# Re-calibrated on live reflector cards from 2026-09-07 incident (#1344):
# Reflector paraphrases of identical 404 Gemini errors score action containment 0.429 - 0.500
# with 6-8 shared action keywords. Lowering to 0.42 and MIN_SHARED to 6 stops all 4 pairs
# of live paraphrases while cleanly passing all distinct lesson pairs in the 133-card corpus.
DUPLICATE_CONDITION_THRESHOLD = 0.25
DUPLICATE_ACTION_THRESHOLD = 0.42
DUPLICATE_MIN_SHARED = 6


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


_INCIDENT_TOOL_ID_RE = re.compile(
    r"\b(?:(?:write_file|read_file|edit_file|exec|grep|bash|python|shell)\s+|tool\s+call\s+)[A-Za-z0-9][A-Za-z0-9_-]{5,}\b",
    re.I,
)
_INCIDENT_TEMP_PATH_RE = re.compile(r"(?:(?:[A-Za-z]:)?/tmp/[A-Za-z0-9._/-]+|/var/tmp/[A-Za-z0-9._/-]+)", re.I)
_INCIDENT_LINE_RANGE_RE = re.compile(r"\blines?\s+\d+\s*[-–]\s*\d+(?:\s+and\s+\d+\s*[-–]\s*\d+)*", re.I)
_INCIDENT_SEQ_RE = re.compile(r"\bseq(?:uence)?\s*\d+\b", re.I)
_INCIDENT_TURN_RE = re.compile(r"\bturn\s+\d+\b", re.I)
_INCIDENT_CYCLE_RE = re.compile(r"\bcycle-[A-Za-z0-9][A-Za-z0-9_-]+\b", re.I)
_INCIDENT_LANGUAGE_RE = re.compile(
    r"\b(?:transcript|tool\s+call|agent\s+issued|analysis\s+during|partial\s+outcome|finish_reason|\bthe\s+agent\b)",
    re.I,
)
_BARE_TEST_SUBJECT_RE = re.compile(r"^\s*(?:the\s+)?test_[A-Za-z0-9_]{8,}\b", re.I)


def incident_only(problem: Any) -> bool:
    """Reject a problem that is an incident report rather than a condition.

    This deliberately examines only the problem/condition text. A hard
    transcript marker must be paired with incident language; cycle IDs and
    other provenance alone remain valid alongside a concrete condition.
    """
    text = str(problem or "").strip()
    hard_marker = any(pattern.search(text) for pattern in (
        _INCIDENT_TOOL_ID_RE, _INCIDENT_TEMP_PATH_RE, _INCIDENT_LINE_RANGE_RE,
        _INCIDENT_SEQ_RE, _INCIDENT_TURN_RE,
    ))
    if hard_marker and _INCIDENT_LANGUAGE_RE.search(text):
        return True
    return bool(_BARE_TEST_SUBJECT_RE.search(text))


def mint_quality_reason(card: dict[str, Any], existing: list[dict[str, Any]] = (), *, extending: bool = False) -> dict[str, str] | None:
    if extending:
        return None  # existing-card evidence/count updates are not minting
    for left, right in (("solution", "title"), ("generalized_insight", "title"), ("hypothesis", "problem")):
        a, b = _quality_text(card.get(left)), _quality_text(card.get(right))
        if a and b and SequenceMatcher(None, a, b).ratio() >= TAUTOLOGY_THRESHOLD:
            return {"reason": f"tautology:{left}:{right}"}
    if anecdote_only(card.get("problem")):
        return {"reason": "anecdote_problem"}
    if incident_only(card.get("problem") or card.get("condition")):
        return {"reason": "incident_problem"}
    condition = keyword_set(card.get("problem") or card.get("root_cause"))
    action = keyword_set(card.get("solution") or card.get("prevention"))
    for entry in existing[:_MAX_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        other_condition = keyword_set(entry.get("problem") or entry.get("root_cause"))
        other_action = keyword_set(entry.get("solution") or entry.get("prevention"))
        def matches(left: frozenset[str], right: frozenset[str], threshold: float) -> bool:
            if not left or not right:
                return False
            if left == right:
                return True
            shared = len(left & right)
            return shared >= DUPLICATE_MIN_SHARED and shared / min(len(left), len(right)) >= threshold
        if (matches(condition, other_condition, DUPLICATE_CONDITION_THRESHOLD)
                and matches(action, other_action, DUPLICATE_ACTION_THRESHOLD)):
            return {"reason": "duplicate", "duplicate_id": str(entry.get("id") or entry.get("path") or entry.get("title"))[:200]}
    return None


def markdown_lesson_pair(workspace: Path, row: dict[str, Any]) -> dict[str, Any] | None:
    """Index authorizes a bounded top-level Markdown read for duplicate checks.

    The index's title/prevents summary alone cannot establish both axes. This
    read is mint-time only; prompt retrieval still never inlines lesson bodies.
    """
    try:
        path = Path(workspace) / str(row.get("path") or "")
        root = (Path(workspace) / "lessons").resolve()
        if path.is_symlink() or path.resolve().parent != root or path.suffix != ".md":
            return None
        if path.stat().st_size > 128 * 1024:
            return None
        text = path.read_text(encoding="utf-8")
        sections = re.split(r"(?m)^## ", text)[1:]
        condition = " ".join(s for s in sections if s.startswith(("Description", "Root Causes")))
        action = " ".join(s for s in sections if not s.startswith(("Description", "Root Causes")))
        if not condition.strip() or not action.strip():
            return None
        return {"id": row.get("id") or row.get("path"), "title": row.get("title"),
                "problem": condition, "solution": action}
    except (OSError, UnicodeError, ValueError):
        return None


def allow_mint(card: dict[str, Any], existing: list[dict[str, Any]], state_dir: Path, *, workspace: Path | None = None, extending: bool = False) -> bool:
    """Run the bounded mint gate and record pass/refusal diagnostics."""
    entries = list(existing[:_MAX_ENTRIES])
    if workspace is not None:
        from nanobot.runtime.lesson_index import read_index, read_index_archives
        entries += bounded_load_yaml(Path(workspace) / "lessons/errors.yaml")
        index_path = Path(workspace) / "lessons/index.md"
        index_entries = read_index(index_path)
        if not index_entries:
            index_entries = read_index_archives(index_path)
        for row in index_entries:
            pair = markdown_lesson_pair(Path(workspace), row)
            if pair:
                entries.append(pair)
    reason = mint_quality_reason(card, entries, extending=extending)
    if reason is None and not extending and card.get("title"):
        reason = _title_gate_reason(card, entries)
    try:
        if reason is None:
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "lesson_id": card.get("id"),
                "decision": "mint_gate_passed",
                "reason": _TITLE_GATE_PASS_REASON,
            }
        else:
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "lesson_id": card.get("id"),
                "decision": "mint_rejected",
                **reason,
                "reason": str(reason.get("reason") or "unknown")[:_MAX_TITLE_DIAGNOSTIC_CHARS],
                "instruction": "Extend the existing lesson with evidence instead of minting" if reason["reason"] == "duplicate" else "Supply a reusable condition and distinct corrective action",
            }
        append_curator_decision(Path(state_dir), row)
    except OSError:
        pass
    return reason is None



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


def _citation_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _citation_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _atomic_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _read_jsonl(path: Path, limit: int) -> list[dict[str, object]]:
    if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return rows[-limit:]


def _decisions_timestamp(row: dict[str, object]) -> datetime | None:
    return _citation_ts(row.get("timestamp") or row.get("ts"))


def _read_decision_lines(path: Path) -> list[bytes]:
    if not path.is_file() or path.stat().st_size > _DECISIONS_MAX_FILE_BYTES:
        return []
    return path.read_bytes().splitlines(keepends=True)


def _read_decision_rows(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in _read_decision_lines(path):
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _write_decision_lines(path: Path, lines: list[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _read_gzip_lines(path: Path) -> list[bytes]:
    with gzip.open(path, "rb") as handle:
        return handle.readlines()


def _write_gzip_lines(path: Path, lines: list[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(fd)
    try:
        with gzip.open(name, "wb") as handle:
            handle.write(b"".join(lines))
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _read_gzip_jsonl(path: Path, limit: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
    return rows[-limit:]


def _write_gzip_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(fd)
    try:
        with gzip.open(name, "wt", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows))
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _archive_day(directory: Path, stem: str, day: str, rows: list[dict[str, object]]) -> None:
    archive = directory / _CITATION_ARCHIVE_DIR / f"{stem}-{day}.jsonl.gz"
    existing = _read_gzip_jsonl(archive, _CITATION_SCAN_MAX_ROWS) if archive.is_file() else []
    _write_gzip_jsonl(archive, (existing + rows)[-_CITATION_SCAN_MAX_ROWS:])


def _rotate_citation_file(directory: Path, filename: str, now: datetime) -> None:
    """Archive prior-date rows by their own UTC date and prune old archives."""
    active = directory / filename
    if not active.is_file():
        return
    rows = _read_jsonl(active, _CITATION_SCAN_MAX_ROWS)
    if not rows:
        return
    stem = filename.removesuffix(".jsonl")
    current: list[dict[str, object]] = []
    old_by_day: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        timestamp = _citation_ts(row.get("ts"))
        if timestamp is None or timestamp.date() == now.date():
            current.append(row)
        else:
            old_by_day.setdefault(timestamp.date().isoformat(), []).append(row)
    for day, old_rows in old_by_day.items():
        _archive_day(directory, stem, day, old_rows)
    _atomic_jsonl(active, current)
    cutoff = (now - timedelta(days=CITATION_SCAN_RETENTION_DAYS)).date()
    archive_dir = directory / _CITATION_ARCHIVE_DIR
    if archive_dir.is_dir():
        for archive in archive_dir.glob(f"{stem}-*.jsonl.gz"):
            try:
                day = datetime.strptime(archive.name[len(stem) + 1:-len(".jsonl.gz")], "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                archive.unlink(missing_ok=True)


def append_curator_decision(
    state_dir: Path,
    row: dict[str, object],
    *,
    now: datetime | None = None,
) -> None:
    """Append one curator decision through the shared bounded writer."""
    current = _citation_now(now)
    path = Path(state_dir) / "curator" / _DECISIONS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamped = dict(row)
        timestamped.setdefault("timestamp", current.isoformat().replace("+00:00", "Z"))
        existing_lines = _read_decision_lines(path)
        existing_lines.append(
            (json.dumps(timestamped, ensure_ascii=False) + "\n").encode("utf-8")
        )
        _write_decision_lines(path, existing_lines)
        _rotate_decisions(path.parent, current)
    except Exception:
        pass


def _rotate_decisions(directory: Path, now: datetime) -> None:
    active = directory / _DECISIONS_FILE
    if not active.is_file() or active.stat().st_size > _DECISIONS_MAX_FILE_BYTES:
        return
    lines = _read_decision_lines(active)
    if (
        active.stat().st_size <= _DECISIONS_ROTATE_BYTES
        and len(lines) <= _DECISIONS_MAX_ROWS
    ):
        return
    by_day: dict[str, list[bytes]] = {}
    current: list[tuple[datetime, bytes]] = []
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            continue
        timestamp = _decisions_timestamp(row)
        if timestamp is None:
            current.append((now, line))
        elif timestamp.date() == now.date():
            current.append((timestamp, line))
        else:
            by_day.setdefault(timestamp.date().isoformat(), []).append(line)
    current.sort(key=lambda item: item[0])
    current_lines = [line for _, line in current[-_DECISIONS_MAX_ROWS:]]
    for day, day_lines in by_day.items():
        if not day_lines:
            continue
        archive = directory / _CITATION_ARCHIVE_DIR / f"decisions-{day}.jsonl.gz"
        existing = _read_gzip_lines(archive) if archive.is_file() else []
        combined = existing + day_lines
        kept = combined[-_DECISIONS_MAX_ROWS:]
        _write_gzip_lines(archive, kept)
        if len(day_lines) <= len(kept):
            archived_tail = kept[-len(day_lines):]
            before_hash = hashlib.sha256(b"".join(day_lines)).hexdigest()
            after_hash = hashlib.sha256(b"".join(archived_tail)).hexdigest()
            if before_hash != after_hash:
                raise ValueError(f"decision archive hash mismatch for {day}")
    _write_decision_lines(active, current_lines)
    cutoff = (now - timedelta(days=_DECISIONS_RETENTION_DAYS)).date()
    archive_dir = directory / _CITATION_ARCHIVE_DIR
    if archive_dir.is_dir():
        for archive in archive_dir.glob("decisions-*.jsonl.gz"):
            try:
                day = datetime.strptime(archive.name[10:-9], "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                archive.unlink(missing_ok=True)


def _append_citation_rows(
    path: Path,
    rows: list[dict[str, object]],
    now: datetime,
    *,
    max_rows: int = _MAX_ENTRIES,
) -> None:
    """Append bounded JSONL rows, preserving the legacy citation path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_jsonl(path, max_rows)
    _atomic_jsonl(path, (existing + rows)[-max_rows:])


def _record_scan_failure(
    directory: Path,
    scan: dict[str, object],
    now: datetime,
    error: Exception,
) -> None:
    """Best-effort secondary trace when the primary scan ledger cannot be written."""
    try:
        failure = dict(scan)
        failure["status"] = "unavailable"
        failure["notes"] = ["scan_write_failed", type(error).__name__]
        _append_citation_rows(
            directory / "scan-failures.jsonl", [failure], now,
            max_rows=_CITATION_SCAN_MAX_ROWS,
        )
    except Exception:
        pass


_EXECUTOR_RESULT_UNSET = object()
_EXECUTOR_RESULT_UNAVAILABLE = object()

# #1546: subagent.py's own terminal statuses whose persisted `result` is a
# fixed canned stub (`"Cancelled before completion."`, `f"Error: {e}"`, the
# wall-clock/loop-breaker text) rather than the model's considered final
# answer. A scan of one of these can never contain a real `[Lesson <id>]`
# marker, so it must not be reported the same way a genuine zero is.
# `"ok"` (real answer, though occasionally itself a canned "no response"
# fallback — #1546 does not attempt to detect that narrower case) and a
# missing/legacy status (no `status` key at all, from before this field
# existed) both continue to read as trustworthy, unchanged from before.
_NOT_FINAL_SUBAGENT_STATUSES = frozenset({"cancelled", "error", "bounded_stop", "blocked"})


class _ExecutorResultNotFinal:
    """#1546: a persisted result exists, but its own status says it is a
    stub, not a considered final answer. Carries what was scanned (or at
    least its length) and the observed status, so `record_citations` can
    record them instead of the reader having to cross-check the ledger.
    """

    __slots__ = ("status", "length")

    def __init__(self, status: str, length: int) -> None:
        self.status = status
        self.length = length


def read_executor_result(
    state_dir: Path,
    task_id: str | None,
    *,
    max_bytes: int = _EXECUTOR_RESULT_MAX_BYTES,
) -> str | object:
    """Read the bounded persisted executor answer, or an unavailable sentinel.

    Returns the raw string when the payload's own ``status`` is ``"ok"`` or
    absent (legacy rows written before that field existed). Returns an
    :class:`_ExecutorResultNotFinal` when ``status`` names a canned-stub
    terminal state (see :data:`_NOT_FINAL_SUBAGENT_STATUSES`) — the text is
    real but structurally cannot contain a citation marker, so it must not
    be scanned as if it were.
    """
    if not task_id:
        return _EXECUTOR_RESULT_UNAVAILABLE
    try:
        path = Path(state_dir) / "subagents" / f"{task_id}.json"
        if not path.is_file():
            return _EXECUTOR_RESULT_UNAVAILABLE
        if path.stat().st_size > max_bytes:
            return _EXECUTOR_RESULT_UNAVAILABLE
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, str):
            return _EXECUTOR_RESULT_UNAVAILABLE
        status = payload.get("status") if isinstance(payload, dict) else None
        if isinstance(status, str) and status in _NOT_FINAL_SUBAGENT_STATUSES:
            return _ExecutorResultNotFinal(status, len(result))
        return result
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _EXECUTOR_RESULT_UNAVAILABLE


def _citation_selector_provenance(
    lessons_context: dict[str, object] | None,
) -> dict[str, object]:
    """Return the bounded set of cards offered in the executor prompt."""
    context = lessons_context if isinstance(lessons_context, dict) else {}
    provenance = context.get("selection_provenance")
    if isinstance(provenance, dict):
        return {"selection_provenance": provenance}
    offered: list[str] = []
    for key in ("relevant_error", "relevant_lesson"):
        card = context.get(key)
        if not isinstance(card, dict):
            continue
        card_id = str(card.get("id") or "").strip()
        if card_id and card_id not in offered:
            offered.append(card_id)
    offered = offered[:_CITATION_PROVENANCE_MAX_CARDS]
    return {
        "selection_provenance": {
            "source": "executor_prompt_context",
            "status": "present" if offered else "empty",
            "selected_ids": offered,
            "selected_count": len(offered),
        }
    }


def record_citations(
    state_dir: Path,
    cycle_id: str,
    texts: list[str] | None = None,
    *,
    executor_result: str | None | object = _EXECUTOR_RESULT_UNSET,
    max_chars: int = 64_000,
    now: datetime | None = None,
    lessons_context: dict[str, object] | None = None,
    selector_provenance: dict[str, object] | None = None,
) -> list[str]:
    """Scan bounded response fields and persist both hits and zero-result scans.

    The scan ledger is retained for :data:`CITATION_SCAN_RETENTION_DAYS` days.
    A successful zero-marker scan is evidence; an absent ledger is not.
    """
    current = _citation_now(now)
    # Store IDs are either simple slugs (LESS-123) or safe relative path-like
    # IDs (lessons/example.md).  Slash and dot are separators between non-empty
    # safe components; whitespace, traversal, URLs, and arbitrary punctuation
    # remain outside the marker grammar (#1570).
    pattern = re.compile(
        r"\[Lesson\s+([A-Za-z0-9_-]+(?:[/.][A-Za-z0-9_-]+)*)\]",
        re.IGNORECASE,
    )
    timestamp = current.isoformat().replace("+00:00", "Z")
    directory = Path(state_dir) / "lesson_usage"
    selector = (
        {"selection_provenance": selector_provenance}
        if selector_provenance is not None
        else _citation_selector_provenance(lessons_context)
    )
    if executor_result is _EXECUTOR_RESULT_UNAVAILABLE:
        scan = {
            "scan_ran": False,
            "cycle_id": str(cycle_id),
            "status": "unavailable",
            "notes": ["executor_result_unavailable"],
            **selector,
            "ts": timestamp,
        }
        try:
            _rotate_citation_file(directory, _CITATION_SCAN_FILE, current)
            _append_citation_rows(directory / _CITATION_SCAN_FILE, [scan], current, max_rows=_CITATION_SCAN_MAX_ROWS)
        except Exception as error:
            _record_scan_failure(directory, scan, current, error)
        return []
    if isinstance(executor_result, _ExecutorResultNotFinal):
        # #1546: a real persisted result whose own status marks it a canned
        # stub (cancelled/error/bounded_stop/blocked) — it cannot contain a
        # citation marker, so it must not read as a confirmed zero. Reuses
        # the "unavailable" status (no new vocabulary value, per #1312) with
        # its own reason, and — unlike the plain-unavailable case above,
        # where nothing was read at all — records what was scanned (its
        # length) and the subagent status that disqualified it, so the next
        # reader does not have to open the subagent file to find out.
        scan: dict[str, object] = {
            "scan_ran": False,
            "cycle_id": str(cycle_id),
            "status": "unavailable",
            "notes": ["executor_result_not_final", f"subagent_status:{executor_result.status}"],
            "scanned_chars": executor_result.length,
            **selector,
            "ts": timestamp,
        }
        try:
            _rotate_citation_file(directory, _CITATION_SCAN_FILE, current)
            _append_citation_rows(directory / _CITATION_SCAN_FILE, [scan], current, max_rows=_CITATION_SCAN_MAX_ROWS)
        except Exception as error:
            _record_scan_failure(directory, scan, current, error)
        return []
    if executor_result is _EXECUTOR_RESULT_UNSET:
        scan_texts = [str(text or "") for text in (texts or [])]
    else:
        scan_texts = [str(executor_result)]
    scanned_texts = [text[:max_chars] for text in scan_texts]
    truncated_chars = sum(len(text) - len(scanned) for text, scanned in zip(scan_texts, scanned_texts))
    matches: list[str] = []
    for text in scanned_texts:
        matches.extend(pattern.findall(text))
    ids = sorted(set(matches))
    scan: dict[str, object] = {
        "scan_ran": True,
        "cycle_id": str(cycle_id),
        "marker_count": len(matches),
        "lesson_ids": ids,
        "status": "partial" if truncated_chars else "complete",
        "notes": ["scan_truncated"] if truncated_chars else [],
        **selector,
        # #1546/#1570: report the characters actually scanned, separately
        # from a bounded tail that was not inspected.  A partial prefix is
        # lower-bound evidence and must never be labelled complete.
        "scanned_chars": sum(len(text) for text in scanned_texts),
        **({"truncated_chars": truncated_chars} if truncated_chars else {}),
        "ts": timestamp,
    }
    if scan["marker_count"] == 0 and scan["scanned_chars"] < _CITATION_SHORT_RESULT_CHARS:
        scan["notes"].append("short_result_not_confident_zero")
    # #1654: a hit no longer gets a second, separate write. `scan["lesson_ids"]`
    # already carries the exact same ids scans.jsonl persists below, from the
    # same local variable in this same call -- a citations.jsonl row was
    # never anything but a lossless copy of that field, one file, one write,
    # readers derive cited ids from scans.jsonl's lesson_ids instead.
    try:
        scan_path = directory / _CITATION_SCAN_FILE
        _rotate_citation_file(directory, _CITATION_SCAN_FILE, current)
        _append_citation_rows(
            scan_path, [scan], current, max_rows=_CITATION_SCAN_MAX_ROWS,
        )
    except Exception as error:
        _record_scan_failure(directory, scan, current, error)
    return ids


def _citation_sources(directory: Path) -> list[Path]:
    archive_dir = directory / _CITATION_ARCHIVE_DIR
    archives = sorted(archive_dir.glob("scans-*.jsonl.gz"), reverse=True) if archive_dir.is_dir() else []
    failures = sorted(archive_dir.glob("scan-failures-*.jsonl.gz"), reverse=True) if archive_dir.is_dir() else []
    return ([directory / _CITATION_SCAN_FILE] if (directory / _CITATION_SCAN_FILE).is_file() else []) + [
        *archives[:_CITATION_SCAN_MAX_ARCHIVES],
        directory / "scan-failures.jsonl",
        *failures[:_CITATION_SCAN_MAX_ARCHIVES],
    ]


def read_curator_decisions(
    state_dir: Path,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Read bounded curator decisions with explicit retention status."""
    current = _citation_now(now)
    directory = Path(state_dir) / "curator"
    active = directory / _DECISIONS_FILE
    archive_dir = directory / _CITATION_ARCHIVE_DIR
    archives = (
        sorted(archive_dir.glob("decisions-*.jsonl.gz"), reverse=True)
        if archive_dir.is_dir()
        else []
    )
    requested = None if since is None else _citation_now(since)
    cutoff = current - timedelta(days=_DECISIONS_RETENTION_DAYS)
    if requested is not None and requested < cutoff:
        return {"status": "unavailable", "rows": [], "notes": ["beyond_retention"]}
    if not active.is_file() and not archives:
        return {"status": "missing", "rows": [], "notes": []}
    try:
        rows = _read_jsonl(active, _DECISIONS_MAX_ROWS)
        for archive in archives[:_DECISIONS_MAX_ARCHIVES]:
            remaining = _DECISIONS_MAX_ROWS - len(rows)
            if remaining <= 0:
                break
            rows.extend(_read_gzip_jsonl(archive, remaining))
        retained = []
        stale = False
        for row in rows:
            timestamp = _decisions_timestamp(row)
            if timestamp is None or timestamp < cutoff:
                stale = True
                continue
            if requested is None or timestamp >= requested:
                retained.append(row)
        retained.sort(
            key=lambda row: _decisions_timestamp(row)
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        if not retained and stale:
            return {"status": "unavailable", "rows": [], "notes": ["beyond_retention"]}
        return {"status": "present" if retained else "empty", "rows": retained, "notes": []}
    except Exception as error:
        return {"status": "unavailable", "rows": [], "notes": [type(error).__name__]}


def read_citation_scans(
    state_dir: Path,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Read bounded retained scan evidence with explicit retention states."""
    current = _citation_now(now)
    directory = Path(state_dir) / "lesson_usage"
    requested = None if since is None else _citation_now(since)
    cutoff = current - timedelta(days=CITATION_SCAN_RETENTION_DAYS)
    if requested is not None and requested < cutoff:
        return {"status": "unavailable", "rows": [], "notes": ["beyond_retention"]}
    sources = _citation_sources(directory)
    if not sources:
        return {"status": "missing", "rows": [], "notes": []}
    rows: list[dict[str, object]] = []
    try:
        for source in sources[:1 + 2 * _CITATION_SCAN_MAX_ARCHIVES + 1]:
            if not source.is_file():
                continue
            remaining = _CITATION_SCAN_MAX_ROWS - len(rows)
            if remaining <= 0:
                break
            if source.name.endswith(".gz"):
                rows.extend(_read_gzip_jsonl(source, remaining))
            else:
                rows.extend(_read_jsonl(source, remaining))
        retained: list[dict[str, object]] = []
        stale = False
        for row in rows:
            timestamp = _citation_ts(row.get("ts"))
            if timestamp is None:
                continue
            if timestamp < cutoff:
                stale = True
                continue
            if requested is None or timestamp >= requested:
                retained.append(row)
        retained.sort(key=lambda row: _citation_ts(row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
        if not retained and stale:
            return {"status": "unavailable", "rows": [], "notes": ["beyond_retention"]}
        return {"status": "present" if retained else "empty", "rows": retained, "notes": []}
    except Exception as error:
        return {"status": "unavailable", "rows": [], "notes": [type(error).__name__]}


# Minimum citation count required before any trainer authority may be granted (ADR-021 Rule 1).
MIN_CITATIONS_FOR_AUTHORITY = 50


def correlate_citations_with_outcomes(
    state_dir: Path,
    *,
    now: datetime | None = None,
    window_days: int = CITATION_SCAN_RETENTION_DAYS,
) -> dict[str, object]:
    """Read-only join of the citation scan ledger (``scans.jsonl``'s own
    ``lesson_ids`` field) against the outcome ledger, by ``cycle_id``.
    Answers one question — does a cycle that cited a lesson show a
    different outcome distribution than one that did not — and answers it
    with bounded counts, nothing more.

    #1654: previously read a separate ``citations.jsonl`` file, which
    duplicated ``scans.jsonl``'s own ``lesson_ids`` field from the same
    write (verified: every citations.jsonl row matched a scans.jsonl row's
    ``lesson_ids`` for the same cycle, 0 mismatches, by construction — both
    came from the identical local variable in ``record_citations``). One
    source now.

    #1505 built the write path (``record_citations``) and #1654 found it had
    zero production readers. This is the first one, and it is explicitly
    NOT a decision: ADR-021 rule 1 forbids granting any trainer authority
    before usefulness is measured, and a function that both measures and
    acts would violate that rule the moment it existed. Callers must not
    branch on this function's output beyond reporting it; the correlation
    it returns is descriptive evidence for a human or a future proposal to
    cite, not a signal this module (or anything else) may act on.

    Fail-open to an ``unavailable`` result, matching this module's existing
    convention for every other bounded reader here.
    """
    try:
        current = _citation_now(now)
        scans = read_citation_scans(state_dir, now=current)
        if scans["status"] == "unavailable":
            return {"status": "unavailable", "notes": list(scans.get("notes") or [])}
        cited_cycle_ids = {
            str(row["cycle_id"])
            for row in scans["rows"]
            if row.get("lesson_ids") and row.get("cycle_id")
        }

        window = state_access.ledger_window(
            state_dir,
            since_ts=(current - timedelta(days=window_days)).isoformat().replace("+00:00", "Z"),
            phases=frozenset({"outcome"}),
            now=current,
        )
        cited_outcome_counts: dict[str, int] = {}
        baseline_outcome_counts: dict[str, int] = {}
        cited_cycles_matched: set[str] = set()
        for row in window.rows:
            cycle_id = str(row.get("cycle_id") or "")
            outcome = str(row.get("outcome") or "unknown")
            if cycle_id and cycle_id in cited_cycle_ids:
                cited_outcome_counts[outcome] = cited_outcome_counts.get(outcome, 0) + 1
                cited_cycles_matched.add(cycle_id)
            else:
                baseline_outcome_counts[outcome] = baseline_outcome_counts.get(outcome, 0) + 1

        return {
            "status": "present" if cited_cycle_ids or window.rows else "empty",
            "window_days": window_days,
            "ledger_status": window.status,
            "distinct_cited_cycles": len(cited_cycle_ids),
            "cited_cycles_matched_in_ledger": len(cited_cycles_matched),
            "cited_outcome_counts": cited_outcome_counts,
            "baseline_outcome_counts": baseline_outcome_counts,
            "offer_outcomes": _offer_outcomes(scans["rows"]),
        }
    except Exception as error:
        return {"status": "unavailable", "notes": [type(error).__name__]}


# #1728: a scan row's selection_provenance says whether a lesson was OFFERED
# in the executor prompt (the "## Proven approach" section with its
# ``cite [Lesson ID]`` line). A zero-marker scan of a prompt that offered no
# lesson is "no lesson offered", not "lesson not cited" -- the two must never
# be summed, or a stricter selector reads as a politeness collapse.
_OFFER_NO_LESSON = "no_lesson_offered"
_OFFER_NOT_CITED = "offered_not_cited"
_OFFER_CITED = "offered_cited"
_OFFER_SCAN_UNAVAILABLE = "offered_scan_unavailable"
_OFFER_UNKNOWN = "offer_unknown"


def lesson_offered_in_scan(row: dict[str, object]) -> bool | None:
    """Was a lesson offered in the prompt this scan row describes?

    ``True``/``False`` when the row's ``selection_provenance`` says so;
    ``None`` when it cannot (no provenance at all -- rows written before
    #1500's provenance existed). Two provenance shapes are read:

    - #1728 shape (``lessons_context.build_lessons_context`` /
      ``selection_provenance``): a ``lessons`` slot dict whose
      ``selected_id`` names the offered lesson, or is ``None`` with a
      ``reason`` (``below_threshold``/``no_candidates``) when none was.
    - pre-#1728 executor-prompt shape (``_citation_selector_provenance``):
      only ``selected_ids``, which mixes the pitfall card id and the lesson
      id. Error cards carry the recorder's ``ERR-`` prefix
      (``bridge._write_structured_error``, and the manual ``ERR-AUTO-``
      cards); anything else in the list is a lesson.
    """
    provenance = row.get("selection_provenance")
    if not isinstance(provenance, dict):
        return None
    if provenance.get("status") == "unavailable":
        # The reconstruction itself failed -- nothing can be read off it.
        return None
    slot = provenance.get("lessons")
    if isinstance(slot, dict) and ("selected_id" in slot or "reason" in slot):
        return bool(str(slot.get("selected_id") or "").strip())
    ids = provenance.get("selected_ids")
    if isinstance(ids, list):
        return any(
            str(raw).strip() and not str(raw).strip().upper().startswith("ERR-")
            for raw in ids
        )
    return None


def _offer_outcomes(rows: list[dict[str, object]]) -> dict[str, int]:
    """Distinct-cycle counts of the offer/citation distinction (#1728).

    ``no_lesson_offered`` -- the prompt carried no lesson, so a zero-marker
    scan says nothing about usefulness. ``offered_not_cited`` -- a lesson
    (and its ``cite [Lesson ID]`` line) was offered, the scan ran and found
    no marker. ``offered_cited`` -- offered and cited.
    ``offered_scan_unavailable`` -- offered, but the scan did not run
    (``status: unavailable``), so neither cited nor not-cited is known.
    ``offer_unknown`` -- the row carries no provenance to read.
    """
    latest: dict[str, str] = {}
    for row in rows:
        cycle_id = str(row.get("cycle_id") or "")
        if not cycle_id:
            continue
        offered = lesson_offered_in_scan(row)
        if offered is None:
            bucket = _OFFER_UNKNOWN
        elif not offered:
            bucket = _OFFER_NO_LESSON
        elif row.get("lesson_ids"):
            bucket = _OFFER_CITED
        elif row.get("scan_ran") is False or row.get("status") == "unavailable":
            bucket = _OFFER_SCAN_UNAVAILABLE
        else:
            bucket = _OFFER_NOT_CITED
        # A cycle with several scan rows (a retry) keeps its strongest
        # reading: cited beats not-cited beats unavailable beats unknown.
        rank = {_OFFER_CITED: 4, _OFFER_NOT_CITED: 3, _OFFER_NO_LESSON: 3,
                _OFFER_SCAN_UNAVAILABLE: 2, _OFFER_UNKNOWN: 1}
        if cycle_id not in latest or rank[bucket] > rank[latest[cycle_id]]:
            latest[cycle_id] = bucket
    counts = {key: 0 for key in (
        _OFFER_NO_LESSON, _OFFER_NOT_CITED, _OFFER_CITED, _OFFER_SCAN_UNAVAILABLE, _OFFER_UNKNOWN,
    )}
    for bucket in latest.values():
        counts[bucket] += 1
    return counts


_LESSON_CENSUS_MAX_LESSONS = 200


def lesson_zero_citation_census(
    state_dir: Path,
    *,
    now: datetime | None = None,
    window_days: int = CITATION_SCAN_RETENTION_DAYS,
) -> dict[str, object]:
    """Lessons offered into context at least once, in the retained scan
    window, with zero citations in that same window — the lesson-side
    sibling of ``skill_fitness.census`` (ADR-021 rule 3: retirement evidence
    must be non-use, never a clock; "never offered" is not evidence of "not
    useful", only "offered and ignored" is, so a lesson that was never
    offered at all does not appear here — there is no denominator for it).

    Fail-open the same way ``skill_fitness.census`` does: an unavailable or
    never-scanned source yields ``ok: False`` with an EMPTY census, because
    "no data" must never be published as "every offered lesson is unused";
    a real, even if empty, retained window yields ``ok: True``.
    """
    try:
        current = _citation_now(now)
        # #1516/#1546's own read_citation_scans reports "empty" (not
        # "missing") for a directory that was never created — its source
        # list always includes a scan-failures.jsonl candidate whether or
        # not the file exists (_citation_sources), so an empty result there
        # does not distinguish "never scanned" from "scanned, found
        # nothing". Check directory existence directly instead, the same
        # distinction skill_fitness.census makes for a missing reads.json.
        if not (Path(state_dir) / "lesson_usage").is_dir():
            return {"ok": False, "reason": "missing", "lessons_offered": 0, "zero_citation": []}
        scans = read_citation_scans(state_dir, now=current)
        if scans["status"] == "unavailable":
            return {
                "ok": False,
                "reason": scans["status"],
                "lessons_offered": 0,
                "zero_citation": [],
            }

        offered_count: dict[str, int] = {}
        last_offered: dict[str, datetime] = {}
        for row in scans["rows"]:
            provenance = row.get("selection_provenance")
            if not isinstance(provenance, dict):
                continue
            ids = provenance.get("selected_ids")
            if not isinstance(ids, list):
                continue
            ts = _citation_ts(row.get("ts"))
            for raw_id in ids:
                lesson_id = str(raw_id).strip()
                if not lesson_id:
                    continue
                offered_count[lesson_id] = offered_count.get(lesson_id, 0) + 1
                if ts is not None and (lesson_id not in last_offered or ts > last_offered[lesson_id]):
                    last_offered[lesson_id] = ts

        # #1654: cited lesson ids come from the same scans["rows"] already
        # read above (``lesson_ids`` per row) rather than a separate
        # citations.jsonl file — that file duplicated this exact field from
        # the same write, verified byte-for-byte, nothing lost by dropping it.
        cutoff = current - timedelta(days=window_days)
        cited_in_window: dict[str, int] = {}
        last_cited: dict[str, datetime] = {}
        for row in scans["rows"]:
            row_ids = row.get("lesson_ids")
            if not isinstance(row_ids, list) or not row_ids:
                continue
            ts = _citation_ts(row.get("ts"))
            if ts is None:
                continue
            for raw_id in row_ids:
                lesson_id = str(raw_id).strip()
                if not lesson_id:
                    continue
                if lesson_id not in last_cited or ts > last_cited[lesson_id]:
                    last_cited[lesson_id] = ts
                if ts >= cutoff:
                    cited_in_window[lesson_id] = cited_in_window.get(lesson_id, 0) + 1

        names = sorted(offered_count)[:_LESSON_CENSUS_MAX_LESSONS]
        return {
            "ok": True,
            "lessons_offered": len(names),
            "zero_citation": [
                {
                    "lesson_id": lesson_id,
                    "offered_in_window": offered_count[lesson_id],
                    "last_offered": (
                        last_offered[lesson_id].isoformat().replace("+00:00", "Z")
                        if lesson_id in last_offered else None
                    ),
                    "last_cited": (
                        last_cited[lesson_id].isoformat().replace("+00:00", "Z")
                        if lesson_id in last_cited else None
                    ),
                }
                for lesson_id in names
                if cited_in_window.get(lesson_id, 0) == 0
            ],
        }
    except Exception:
        return {"ok": False, "reason": "census_error", "lessons_offered": 0, "zero_citation": []}
