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

Fail-open everywhere: a missing lessons/ dir, missing/corrupt YAML, a
missing ``pyyaml``, or any other exception all degrade to ``{}`` — the
exact pre-#912 behavior (no section rendered).
"""
from __future__ import annotations

import os
import re
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
# make request-writing slow. Files grow with newest entries last (plain
# append order); when over the cap only the newest (tail) slice is scanned.
_MAX_CARDS_SCANNED = 200

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


def _safe_load_yaml(path: Path) -> list[dict[str, Any]]:
    """Minimal, standalone re-implementation of lessons.py's loader.

    Returns ``[]`` on any problem (missing file/dir, empty file, malformed
    YAML, non-list top level) — never raises.
    """
    try:
        if not _YAML_OK or not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return []
        data = yaml.safe_load(text)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _capped_entries(path: Path) -> list[dict[str, Any]]:
    entries = [e for e in _safe_load_yaml(path) if isinstance(e, dict)]
    if len(entries) > _MAX_CARDS_SCANNED:
        entries = entries[-_MAX_CARDS_SCANNED:]
    return entries


def _score_entry(task_words: set[str], entry: dict[str, Any], secondary_field: str) -> tuple[int, int]:
    """Return ``(score, shared_word_count)`` for one candidate card.

    ``title`` + ``category`` count double; ``secondary_field``
    (``root_cause`` for errors, ``approach`` for lessons) counts once.
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

    Ties resolve to the LAST matching entry in ``entries`` (i.e. the newest,
    per this module's newest-last file convention) by using ``>=`` when
    comparing to the running best.
    """
    if not task_words:
        return None
    best: dict[str, Any] | None = None
    best_score = -1
    for entry in entries:
        score, shared_count = _score_entry(task_words, entry, secondary_field)
        if shared_count < _MIN_SHARED_WORDS:
            continue
        if score >= best_score:
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
            }

        return result
    except Exception:
        return {}
