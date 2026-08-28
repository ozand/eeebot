"""Re-close the lessons loop (#912): fill the proposer's ``lessons_context``.

``bridge.py`` (~line 1229) has always been ready to render a
``relevant_error`` / ``relevant_lesson`` card pair into the executor prompt
("## Known pitfall ..." / "## Proven approach ..."), but nothing populated
the field once the coordinator (the old writer, via
``nanobot.runtime.lessons.LessonsDB.query_for_task``) was decommissioned —
``llm_proposer.write_request`` hard-coded ``"lessons_context": {}``.

This module is a small, standalone replacement for the read side of that
old path. It intentionally does NOT import ``nanobot.runtime.lessons``
(``LessonsDB`` is scheduled for deletion in #916) — it copies just the
minimal safe-YAML-load logic it needs.

Matching: every proposer request shares the same ``task_id``
(``llm-proposed-improvement``), so ``LessonsDB.query_for_task``'s
task_id-based lookup is useless here. Instead this module ranks cards by
plain word overlap between the proposal's ``task_title`` (+ ``target_path``)
and each card's ``title``/``category`` (weighted higher) and
``root_cause``/``approach`` (weighted lower) — the same "4+ letter words,
proportional/simple overlap" spirit as
``cycle_planning._title_already_done_in_git_log``, just scoped to a
handful of YAML cards instead of a git log.

On-disk shapes handled (#912 review): ``errors.yaml`` legacy/manual cards
are a bare top-level YAML list with ``title``/``root_cause``/``prevention``
fields directly. The LIVE per-cycle writer, ``bridge._write_structured_lesson``
(bridge.py ~3807-3882), instead writes ``lessons.yaml`` as a top-level
DICT — ``{'lessons': [...]}`` — and its entries carry NO ``title``/
``category``/``approach``/``reusable_insight`` at all, only
``hypothesis``/``result``/``generalized_insight``/``task_id`` (see
``_normalize_entry``, which maps those onto the canonical fields so
scoring/rendering can treat every card uniformly). Both writers prepend
new entries with ``list.insert(0, ...)`` — the list is newest-FIRST.

Fail-open everywhere: a missing lessons/ dir, missing/corrupt YAML, an
oversized file, a missing ``pyyaml``, or any other exception all degrade
to ``{}`` — the exact pre-#912 behavior (no section rendered).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
    _YAML_OK = True
except ImportError:
    _YAML_OK = False

ENABLED_ENV = "SELFEVO_LESSONS_CONTEXT_ENABLED"
_FALSY = {"0", "false", "no", "off"}

# Cards scanned per file, capped so a large lessons/errors.yaml can never
# make request-writing slow. Both writers prepend (``insert(0, ...)``), so
# the file is newest-FIRST — when over the cap, the HEAD slice (the newest
# entries) is kept, not the tail.
_MAX_CARDS_SCANNED = 200

# Size guard so "can never slow request-writing" is an honest claim even if
# lessons/errors.yaml somehow grows huge outside the normal cadence.
_MAX_FILE_BYTES = 2 * 1024 * 1024

# Minimum total distinct shared words (title/category + root_cause/approach
# combined) for a card to be considered relevant at all.
_MIN_SHARED_WORDS = 2

_TITLE_CAP = 200
_TEXT_CAP = 400

_WORD_RE = re.compile(r"[A-Za-z]{4,}")


def _extract_words(text: Any) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(str(text or ""))}


def _cap(text: Any, limit: int) -> str:
    return str(text or "")[:limit]


_MAX_FILE_AGE_DAYS = 90


def _safe_load_yaml(path: Path) -> list[dict[str, Any]]:
    """Minimal, standalone re-implementation of lessons.py's loader.

    Accepts three on-disk shapes: a bare top-level list (legacy manual
    cards, e.g. today's ``errors.yaml``); a top-level dict wrapping the
    list under a ``'lessons'`` key or an ``'errors'`` key (the LIVE
    ``bridge._write_structured_lesson`` shape for ``lessons.yaml``, and a
    defensive match for any future errors-side writer using the same
    convention). Any other dict shape, or anything that isn't a list once
    unwrapped, is treated as unrecognized -> ``[]``.

    Returns ``[]`` on any problem (missing file/dir, empty file, oversized
    file, malformed YAML, unrecognized top level) — never raises.
    """
    try:
        if not _YAML_OK or not path.exists():
            return []
        try:
            stat = path.stat()
            if stat.st_size > _MAX_FILE_BYTES:
                return []
            if time.time() - stat.st_mtime > _MAX_FILE_AGE_DAYS * 86400:
                return []
        except OSError:
            return []
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return []
        data = yaml.safe_load(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("lessons", "errors"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
            return []
        return []
    except Exception:
        return []


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Fill canonical title/approach/reusable_insight/id fields from the
    LIVE bridge writer's shape when they're absent.

    ``bridge._write_structured_lesson`` entries carry
    ``hypothesis``/``result``/``generalized_insight``/``task_id`` and NO
    ``title``/``category``/``approach``/``reusable_insight`` at all.
    Legacy ``LessonsDB``-authored cards (today's ``errors.yaml``, and any
    pre-#912 coordinator-written ``lessons.yaml``) already carry the
    canonical fields directly and pass through unchanged — this only fills
    gaps, never overwrites an existing value.
    """
    normalized = dict(entry)
    if not normalized.get("title") and normalized.get("hypothesis"):
        normalized["title"] = _cap(normalized["hypothesis"], _TITLE_CAP)
    if not normalized.get("approach") and normalized.get("result"):
        normalized["approach"] = normalized["result"]
    if not normalized.get("reusable_insight") and normalized.get("generalized_insight"):
        normalized["reusable_insight"] = normalized["generalized_insight"]
    if not normalized.get("id") and normalized.get("task_id"):
        normalized["id"] = normalized["task_id"]
    return normalized


def _capped_entries(path: Path) -> list[dict[str, Any]]:
    entries = [e for e in _safe_load_yaml(path) if isinstance(e, dict)]
    # Both writers prepend (insert(0, ...)) -> newest-FIRST. Keep the HEAD
    # slice (the newest entries), not the tail, when over the scan cap.
    if len(entries) > _MAX_CARDS_SCANNED:
        entries = entries[:_MAX_CARDS_SCANNED]
    return [_normalize_entry(e) for e in entries]


def _score_entry(task_words: set[str], entry: dict[str, Any], secondary_field: str) -> tuple[int, int]:
    """Return ``(score, shared_word_count)`` for one candidate card.

    ``title`` + ``category`` count double; ``secondary_field``
    (``root_cause`` for errors, ``approach`` for lessons, already
    normalized onto the entry by ``_normalize_entry``) counts once.
    """
    primary_words = _extract_words(f"{entry.get('title', '')} {entry.get('category', '')}")
    secondary_words = _extract_words(entry.get(secondary_field, ""))
    primary_shared = task_words & primary_words
    secondary_shared = task_words & secondary_words
    shared_count = len(primary_shared | secondary_shared)
    score = 2 * len(primary_shared) + len(secondary_shared)
    return score, shared_count


def _best_card(
    entries: list[dict[str, Any]], task_words: set[str], secondary_field: str
) -> dict[str, Any] | None:
    """Highest-scoring card at/above the minimum shared-word threshold.

    ``entries`` is newest-first (see ``_capped_entries``). Ties resolve to
    the EARLIEST matching entry, i.e. the newest card, via a strict ``>``
    comparison that never lets a later (older) same-score entry replace an
    earlier (newer) one.
    """
    if not task_words:
        return None
    best: dict[str, Any] | None = None
    best_score = -1
    for entry in entries:
        score, shared_count = _score_entry(task_words, entry, secondary_field)
        if shared_count < _MIN_SHARED_WORDS:
            continue
        if score > best_score:
            best_score = score
            best = entry
    return best


def build_lessons_context(
    selfevo_repo: Path | None, task_title: str, target_path: str = ""
) -> dict[str, Any]:
    """Return ``{}``, or up to one ``relevant_error`` + one ``relevant_lesson``
    card selected from the instance repo's ``lessons/errors.yaml`` and
    ``lessons/lessons.yaml``, shaped exactly as ``bridge.py``'s
    ``build_task`` renderer expects (see module docstring).

    Never raises: any failure (missing repo, missing/corrupt YAML, missing
    ``pyyaml``, kill-switch off, etc.) returns ``{}``, matching the field's
    behavior before #912.
    """
    try:
        raw_enabled = os.environ.get(ENABLED_ENV, "1").strip().lower()
        if raw_enabled in _FALSY:
            return {}
        if not selfevo_repo:
            return {}
        task_words = _extract_words(f"{task_title} {target_path}")
        if not task_words:
            return {}

        lessons_dir = Path(selfevo_repo) / "lessons"

        result: dict[str, Any] = {}

        err = _best_card(_capped_entries(lessons_dir / "errors.yaml"), task_words, "root_cause")
        if err:
            result["relevant_error"] = {
                "id": err.get("id"),
                "title": _cap(err.get("title"), _TITLE_CAP),
                "root_cause": _cap(err.get("root_cause"), _TEXT_CAP),
                "prevention": _cap(err.get("prevention"), _TEXT_CAP),
            }

        less = _best_card(_capped_entries(lessons_dir / "lessons.yaml"), task_words, "approach")
        if less:
            result["relevant_lesson"] = {
                "id": less.get("id"),
                "title": _cap(less.get("title"), _TITLE_CAP),
                "approach": _cap(less.get("approach"), _TEXT_CAP),
                "reusable_insight": _cap(less.get("reusable_insight"), _TEXT_CAP),
                **({"problem": _cap(less.get("problem"), _TEXT_CAP),
                    "solution": _cap(less.get("solution"), _TEXT_CAP)}
                   if less.get("problem") and less.get("solution") else {}),
            }

        return result
    except Exception:
        return {}
