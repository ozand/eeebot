"""Bounded Lesson schema v2 helpers (#1071).

A v2 lesson is a problem-to-solution record. The module is intentionally
stdlib-first and contains no LLM or timer behavior; callers decide which
validated delta is allowed to mint a lesson.
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
