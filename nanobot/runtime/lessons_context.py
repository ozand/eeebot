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
fields directly. Reflector-promoted v2 cards instead arrive in
``lessons.yaml`` through the curator staging path as a top-level DICT —
``{'lessons': [...]}`` — and their entries carry the canonical selection
fields alongside provenance. The bridge now queues candidates in the
reflector journal; it does not write this file directly. The entries carry
NO legacy ``category`` field, while
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
from typing import Any, Callable

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

# #1728: minimum lexical score (``2 * primary_shared + secondary_shared``,
# see ``_score_entry``) for a card to be emitted into the executor prompt.
# 4 means "two title/identifier words, or one title word plus two body
# words" -- set from the 7-day replay attached to PR for #1728, where
# ``_MIN_SHARED_WORDS`` alone let two generic body words ("cycle", "check")
# pass an unrelated lesson on every prompt. Exposed on the request as
# ``selection_provenance.<slot>.threshold`` next to the achieved ``score``.
_MIN_SCORE = 4

# #1728: corpus-generic word suppression (see ``_generic_words``). A word
# present in >= 25% of the scanned cards of a slot's corpus is dropped from
# the task word set for that slot; corpora under 20 cards are left alone.
_GENERIC_DF = 0.25
_GENERIC_MIN_CORPUS = 20

# #1728 reason vocabulary carried on ``selection_provenance.<slot>.reason``
# for both the pitfall slot (``errors``) and the lesson slot (``lessons``).
REASON_SELECTED = "selected"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_EMPTY_PREVENTION = "empty_prevention"
REASON_INFRA_CLASS = "infra_class"
REASON_NO_CANDIDATES = "no_candidates"

# #1728: infrastructure-class cards describe gateway weather, not a pitfall
# of the task. The structured error recorder (``bridge._write_structured_error``,
# #1687) writes the cycle's ``_rollback_reason`` verbatim into BOTH
# ``category`` and ``reason`` (older recorder cards carry ``reason`` only),
# so both fields are matched. Exact labels the recorder writes today:
# ``executor_llm_error`` (bridge.py ~3461), ``push_rejected`` /
# ``push_pending`` (integration result reasons, bridge.py ~923-927, carried
# into ``_rollback_reason`` via ``_integ['reason']``). Substrings cover any
# gateway/timeout/llm_unavailable-flavoured class, including manual cards
# such as ``timeout-desync``. Excluded cards stay in ``errors.yaml`` for the
# dashboard and the scorecard; this only removes them from executor-facing
# selection.
_INFRA_CLASSES_EXACT = frozenset({"executor_llm_error", "push_rejected", "push_pending"})
_INFRA_CLASS_SUBSTRINGS = ("gateway", "timeout", "llm_unavailable")
_CLASS_FIELDS = ("category", "reason")

# #1728: words that appear in nearly every proposal title / instruction
# text and carry no task identity. The proposer prefixes every request
# title with "Implement and commit:"; the rest are >=4-letter function
# words the ``[A-Za-z]{4,}`` extractor would otherwise count as overlap.
_STOPWORDS = frozenset({
    "implement", "commit", "that", "this", "with", "from", "into", "when",
    "then", "than", "have", "will", "should", "must", "only", "also", "each",
    "their", "there", "these", "those", "which", "while", "where", "after",
    "before", "about", "over", "under", "does", "done", "been", "being",
    "make", "made", "using", "used", "onto", "your", "them", "they", "would",
    "could", "every", "some", "such", "more", "most", "less", "very", "just",
    "like", "between", "through", "without", "within", "already", "still",
    "both", "either", "against", "instead", "rather",
})
# Path segments that name a directory or file type, not a task: present in
# almost every ``target_path`` / lesson ``id`` and therefore no evidence.
_PATH_NOISE = frozenset({
    "lessons", "scripts", "tests", "test", "nanobot", "runtime", "docs",
    "skills", "https", "http", "json", "yaml", "html", "readme",
})

_TITLE_CAP = 200
_TEXT_CAP = 400
_QUOTED_HYPOTHESIS_RE = re.compile(r'"([^"\n]+)"')
# Related hint cap: at most this many slugs rendered in a card (#1095).
_RELATED_HINT_CAP = 3
_CANDIDATE_PROVENANCE_CAP = 3
_GENERIC_PROVENANCE_CAP = 8

_WORD_RE = re.compile(r"[A-Za-z]{4,}")
# #1728: identifier / path-stem tokenisation -- split on ``/ _ - .`` and
# whitespace. ``_IDENT_IN_PROSE_RE`` picks only identifier-shaped tokens
# (containing one of those separators) out of free text such as
# ``instructions``, so prose words do not flood the task word set.
_IDENT_SPLIT_RE = re.compile(r"[/_\-.\s]+")
_IDENT_IN_PROSE_RE = re.compile(r"[A-Za-z0-9]+(?:[/_\-.][A-Za-z0-9]+)+")


def _extract_words(text: Any) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(str(text or ""))}


def _identifier_words(text: Any) -> set[str]:
    """Path-stem / identifier tokens of ``text`` (#1728).

    ``scripts/inspect_cycle_diff.py`` -> ``{inspect, cycle, diff}``: split on
    ``/ _ - .`` and whitespace, lowercased, alphanumeric, >=4 chars, minus
    directory / file-type noise. Digits are kept (``adr021``) so version and
    issue identifiers can match; ``v2``-style tokens fall under the length
    floor.
    """
    out: set[str] = set()
    for piece in _IDENT_SPLIT_RE.split(str(text or "")):
        piece = piece.lower()
        if len(piece) >= 4 and piece.isalnum():
            out.add(piece)
    return out - _PATH_NOISE - _STOPWORDS


def _identifiers_in_prose(text: Any) -> set[str]:
    """Identifier tokens embedded in free text (``prevent_repeat_failures.py``
    inside a sentence), tokenised by ``_identifier_words``. Plain prose
    words are NOT included -- ``instructions`` is long and generic."""
    out: set[str] = set()
    for token in _IDENT_IN_PROSE_RE.findall(str(text or "")):
        out |= _identifier_words(token)
    return out


def _task_words(task_title: str, target_path: str = "", instructions: str = "") -> set[str]:
    """The task-side word set every candidate is scored against (#1728).

    Title words (the pre-#1728 set, minus ``_STOPWORDS``) plus path-stem /
    identifier tokens from ``target_path`` and from identifier-shaped
    tokens inside ``instructions``.
    """
    words = _extract_words(f"{task_title} {target_path}") - _STOPWORDS - _PATH_NOISE
    words |= _identifier_words(target_path)
    words |= _identifiers_in_prose(instructions)
    return words


def _generic_words(entries: list[dict[str, Any]], secondary_field: str) -> set[str]:
    """Words so common in this corpus that sharing them proves nothing (#1728).

    A word carried by at least ``_GENERIC_DF`` of the scanned cards (title +
    category + ``secondary_field``) is removed from the task word set for
    that slot. On the eeepc corpus of 2026-09-18 that is ``read``,
    ``lessons``, ``before``, ``cycle``, ``file``, ``test`` for the lesson
    slot -- the words that let one unrelated lesson clear the old
    shared-word threshold on every prompt. Only applied when the corpus is
    at least ``_GENERIC_MIN_CORPUS`` cards: a two-card corpus makes every
    word "generic" and would blank the selector.
    """
    if len(entries) < _GENERIC_MIN_CORPUS:
        return set()
    counts: dict[str, int] = {}
    for entry in entries:
        words = _extract_words(
            f"{entry.get('title', '')} {entry.get('category', '')} {entry.get(secondary_field, '')}"
        )
        for word in words:
            counts[word] = counts.get(word, 0) + 1
    floor = _GENERIC_DF * len(entries)
    return {word for word, count in counts.items() if count >= floor}


def _card_class_labels(entry: dict[str, Any]) -> list[str]:
    return [
        str(entry.get(field) or "").strip().lower()
        for field in _CLASS_FIELDS
        if str(entry.get(field) or "").strip()
    ]


def _is_infra_class(entry: dict[str, Any]) -> bool:
    """True when the card's class (``category`` or ``reason``) names an
    infrastructure failure rather than a pitfall of the task (#1728)."""
    for label in _card_class_labels(entry):
        if label in _INFRA_CLASSES_EXACT:
            return True
        if any(marker in label for marker in _INFRA_CLASS_SUBSTRINGS):
            return True
    return False


def _teaches_nothing(entry: dict[str, Any]) -> bool:
    """True when ``prevention`` or ``root_cause`` is empty: the card is
    recorded, not taught (#1728). Recorder cards (#1687) carry
    ``hypothesis``/``result``/``generalized_insight`` boilerplate and no
    ``root_cause``/``prevention`` at all; the renderer prints exactly those
    two fields, so such a card would render two empty lines."""
    return not (
        str(entry.get("prevention") or "").strip()
        and str(entry.get("root_cause") or "").strip()
    )


def _card_exclusion_reason(entry: dict[str, Any]) -> str | None:
    """Executor-facing exclusion for an error card, or ``None`` when the card
    may be offered. Class exclusion is reported first: it is the more
    specific explanation when both apply (recorder infra cards are also
    prevention-less)."""
    if _is_infra_class(entry):
        return REASON_INFRA_CLASS
    if _teaches_nothing(entry):
        return REASON_EMPTY_PREVENTION
    return None


def _cap(text: Any, limit: int) -> str:
    return str(text or "")[:limit]


def _related_hint_for(entry: dict[str, Any]) -> str:
    """Return compact related string for a card (capped, empty when none). (#1095)"""
    slugs = [
        s for s in (entry.get("related") or [])
        if isinstance(s, str) and s.strip()
    ][:_RELATED_HINT_CAP]
    if not slugs:
        return ""
    return ", ".join(slugs)
_MAX_FILE_AGE_DAYS = 90


def _safe_load_yaml(path: Path) -> list[dict[str, Any]]:
    """Minimal, standalone re-implementation of lessons.py's loader.

    Accepts three on-disk shapes: a bare top-level list (legacy manual
    cards, e.g. today's ``errors.yaml``); a top-level dict wrapping the
    list under a ``'lessons'`` key or an ``'errors'`` key (the LIVE
    reflector-promoted v2 shape for ``lessons.yaml``, and a
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

    reflector-promoted v2 entries carry
    ``hypothesis``/``result``/``generalized_insight``/``task_id`` and NO
    ``title``/``category``/``approach``/``reusable_insight`` at all.
    Legacy ``LessonsDB``-authored cards (today's ``errors.yaml``, and any
    pre-#912 coordinator-written ``lessons.yaml``) already carry the
    canonical fields directly and pass through unchanged — this only fills
    gaps, never overwrites an existing value.
    """
    normalized = dict(entry)
    if not normalized.get("title") and normalized.get("hypothesis"):
        hypothesis = str(normalized["hypothesis"])
        quoted = _QUOTED_HYPOTHESIS_RE.search(hypothesis)
        title = quoted.group(1) if quoted else hypothesis
        normalized["title"] = _cap(title, _TITLE_CAP)
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


def _recurrence_bonus(entry: dict[str, Any]) -> int:
    """Return a small bounded bonus for independently repeated evidence.

    ``seen_count`` records sightings and ``distinct_days`` prevents repeated
    observations on one day from looking like durable proof. Missing or
    malformed metadata contributes no bonus; the lexical selector remains
    usable for legacy cards. The cap keeps recurrence from becoming a new
    retrieval engine or overwhelming task-word relevance.
    """
    seen = entry.get("seen_count")
    days = entry.get("distinct_days")
    if (
        isinstance(seen, bool)
        or isinstance(days, bool)
        or not isinstance(seen, int)
        or not isinstance(days, int)
        or days < 2
        or seen < 1
    ):
        return 0
    independent_days = days - 1
    extra_sightings = max(0, seen - days)
    return min(2, independent_days + extra_sightings)


def _score_entry(
    task_words: set[str], entry: dict[str, Any], secondary_field: str
) -> tuple[int, int, int]:
    """Return ``(lexical_score, shared_word_count, recurrence_bonus)``.

    ``title`` + ``category`` count double; ``secondary_field``
    (``root_cause`` for errors, ``approach`` for lessons, already
    normalized onto the entry by ``_normalize_entry``) counts once.
    Recurrence is returned separately so callers can use it only to break an
    exact lexical tie. Both the live selector and selector-provenance path call
    this function, so they cannot disagree about recurrence weighting.
    """
    primary_words = _extract_words(f"{entry.get('title', '')} {entry.get('category', '')}")
    # #1728: a card's own identifiers (``lessons/inspect_cycle_diff.md``,
    # a ``path``/``target_path``/``task_id`` naming the file it is about)
    # are title-grade evidence of what it is about.
    for field in ("id", "path", "target_path", "task_id"):
        primary_words |= _identifier_words(entry.get(field, ""))
    secondary_words = _extract_words(entry.get(secondary_field, ""))
    primary_shared = task_words & primary_words
    secondary_shared = task_words & secondary_words
    shared_count = len(primary_shared | secondary_shared)
    lexical_score = 2 * len(primary_shared) + len(secondary_shared)
    return lexical_score, shared_count, _recurrence_bonus(entry)


def _meets_threshold(score: int, shared_count: int) -> bool:
    """The single relevance gate both selector paths apply (#1728)."""
    return shared_count >= _MIN_SHARED_WORDS and score >= _MIN_SCORE


def _best_card(
    entries: list[dict[str, Any]], task_words: set[str], secondary_field: str
) -> dict[str, Any] | None:
    """Select the best lexical match, using recurrence only as a tie-breaker.

    ``entries`` is newest-first (see ``_capped_entries``). Recurrence is
    consulted only after both lexical score and shared-word count tie; it
    cannot crowd out a materially stronger or more specific lexical match
    merely because it has a larger proof count. Exact rank ties still resolve
    to the earliest (newest) entry. A recurrent card is never preferred over
    a card with a more specific lexical match.
    """
    if not task_words:
        return None
    best: dict[str, Any] | None = None
    best_rank = (-1, -1, -1)
    for entry in entries:
        score, shared_count, recurrence = _score_entry(task_words, entry, secondary_field)
        if not _meets_threshold(score, shared_count):
            continue
        rank = (score, shared_count, recurrence)
        if rank > best_rank:
            best_rank = rank
            best = entry
    return best


def _best_card_with_provenance(
    entries: list[dict[str, Any]],
    task_words: set[str],
    secondary_field: str,
    exclude: Callable[[dict[str, Any]], str | None] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Run the same ranking as ``_best_card`` while collecting bounded evidence.

    ``exclude`` (#1728) names cards that may not be offered to the executor
    even when relevant (``_card_exclusion_reason`` for the pitfall slot); it
    returns the exclusion reason or ``None``. The provenance carries
    ``score`` (the selected card's, or the best score seen when nothing was
    selected, so the ledger shows how close the corpus came), ``threshold``
    (``_MIN_SCORE``) and ``reason`` -- one of ``selected``,
    ``below_threshold``, ``empty_prevention``, ``infra_class``,
    ``no_candidates``. When the only above-threshold cards were excluded,
    ``reason`` is the top-ranked excluded card's exclusion reason.
    """
    base = {
        "candidates": [],
        "candidate_count": 0,
        "selected_id": None,
        "tie_count": 0,
        "tie_discarded": False,
        "score": None,
        "threshold": _MIN_SCORE,
        "excluded_count": 0,
    }
    if not task_words or not entries:
        return None, {**base, "status": "empty_corpus", "reason": REASON_NO_CANDIDATES}
    candidates: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    best_rank = (-1, -1, -1)
    tie_count = 0
    top_score_seen = 0
    excluded_count = 0
    top_excluded: tuple[tuple[int, int, int], str] | None = None
    for entry in entries:
        score, shared_count, recurrence = _score_entry(task_words, entry, secondary_field)
        top_score_seen = max(top_score_seen, score)
        if not _meets_threshold(score, shared_count):
            continue
        rank = (score, shared_count, recurrence)
        exclusion = exclude(entry) if exclude is not None else None
        if exclusion is not None:
            excluded_count += 1
            if top_excluded is None or rank > top_excluded[0]:
                top_excluded = (rank, exclusion)
            continue
        card_id = str(entry.get("id") or "").strip()
        if card_id:
            candidates.append({"id": card_id, "score": score, "shared_words": shared_count})
        if rank > best_rank:
            best_rank = rank
            best = entry
            tie_count = 1 if card_id else 0
        elif rank == best_rank and card_id:
            tie_count += 1
    if best is not None:
        status, reason, score_out = "present", REASON_SELECTED, best_rank[0]
    elif top_excluded is not None:
        status, reason, score_out = "excluded", top_excluded[1], top_excluded[0][0]
    else:
        status, reason, score_out = "below_threshold", REASON_BELOW_THRESHOLD, top_score_seen
    selected_id = str(best.get("id") or "").strip() if best else None
    return best, {
        **base,
        "status": status,
        "reason": reason,
        "score": score_out,
        "candidates": candidates[:_CANDIDATE_PROVENANCE_CAP],
        "candidate_count": len(candidates),
        "excluded_count": excluded_count,
        "selected_id": selected_id,
        "tie_count": tie_count,
        "tie_discarded": tie_count > 1,
    }


def _select_cards(
    lessons_dir: Path, task_words: set[str]
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any] | None, dict[str, Any], bool]:
    """The one selector both public entry points run (#1728).

    Returns ``(error, error_provenance, lesson, lesson_provenance,
    lesson_from_archive)``. Keeping ``build_lessons_context`` (the request
    writer) and ``selection_provenance`` (the bridge's reconstruction when a
    request carries no context) on the same code path is what makes the
    reconstructed provenance a faithful stand-in for the recorded one.
    """
    error_entries = _capped_entries(lessons_dir / "errors.yaml")
    error_generic = _generic_words(error_entries, "root_cause")
    error_words = task_words - error_generic
    error, error_data = _best_card_with_provenance(
        error_entries, error_words, "root_cause", exclude=_card_exclusion_reason
    )
    error_data["generic_suppressed"] = sorted(task_words & error_generic)[:_GENERIC_PROVENANCE_CAP]
    from nanobot.runtime.lesson_index import find_index_matches, read_index
    index_path = lessons_dir / "index.md"
    live_index_entries = read_index(index_path)
    yaml_lessons = _capped_entries(lessons_dir / "lessons.yaml")
    lesson_generic = _generic_words(yaml_lessons + live_index_entries, "approach")
    lesson_words = task_words - lesson_generic
    # Keep the live index as the cheap first path, but consult retained
    # archives when the requested task does not match any live row. This
    # avoids making an unrelated live row suppress a cited archived lesson.
    live_best = _best_card(live_index_entries, lesson_words, "approach")
    archive_entries: list[dict[str, Any]] = []
    if not live_best:
        archive_entries = find_index_matches(
            index_path,
            predicate=lambda entry: _meets_threshold(
                *_score_entry(lesson_words, entry, "approach")[:2]
            ),
        )
    lesson_entries = yaml_lessons + live_index_entries + archive_entries
    lesson, lesson_data = _best_card_with_provenance(lesson_entries, lesson_words, "approach")
    lesson_data["generic_suppressed"] = sorted(task_words & lesson_generic)[:_GENERIC_PROVENANCE_CAP]
    from_archive = bool(lesson is not None and lesson in archive_entries and lesson not in live_index_entries)
    return error, error_data, lesson, lesson_data, from_archive


def _provenance_envelope(
    source: str, error: dict[str, Any] | None, error_data: dict[str, Any],
    lesson: dict[str, Any] | None, lesson_data: dict[str, Any],
) -> dict[str, Any]:
    """Shape shared by the recorded (request) and reconstructed (bridge)
    provenance. Slot names: ``errors`` is the pitfall slot, ``lessons`` the
    lesson slot; each carries ``score``/``threshold``/``reason`` (#1728).
    ``selected_ids`` stays for ``lesson_v2.lesson_zero_citation_census``."""
    return {
        "source": source,
        "status": "present" if error or lesson else "empty",
        "selected_ids": [
            str(card.get("id")) for card in (error, lesson)
            if card and card.get("id")
        ][:3],
        "errors": error_data,
        "lessons": lesson_data,
    }


def selection_provenance(
    selfevo_repo: Path | None, task_title: str, target_path: str = "", instructions: str = ""
) -> dict[str, Any]:
    """Return bounded selector provenance using the same input corpus."""
    try:
        if not selfevo_repo or os.environ.get(ENABLED_ENV, "1").strip().lower() in _FALSY:
            return {"source": "reconstructed", "status": "empty", "selected_ids": []}
        words = _task_words(task_title, target_path, instructions)
        error, error_data, lesson, lesson_data, _from_archive = _select_cards(
            Path(selfevo_repo) / "lessons", words
        )
        return _provenance_envelope("reconstructed", error, error_data, lesson, lesson_data)
    except Exception:
        return {"source": "reconstructed", "status": "unavailable", "selected_ids": []}


def build_lessons_context(
    selfevo_repo: Path | None, task_title: str, target_path: str = "", instructions: str = ""
) -> dict[str, Any]:
    """Return ``{}``, or up to one ``relevant_error`` + one ``relevant_lesson``
    card selected from the instance repo's ``lessons/errors.yaml`` and
    ``lessons/lessons.yaml``, shaped exactly as ``bridge.py``'s
    ``build_task`` renderer expects (see module docstring), plus
    ``selection_provenance`` (#1728) -- ``errors`` (pitfall slot) and
    ``lessons`` (lesson slot) each with ``score``, ``threshold`` and
    ``reason`` so the ledger shows why a section was or was not present.
    ``selection_provenance`` is present whenever selection ran, including
    when neither card qualified; the renderer ignores it.

    ``instructions`` (#1728) is optional: identifier-shaped tokens in it
    (``prevent_repeat_failures.py``) join the task word set. The proposer
    does not pass it yet.

    Never raises: any failure (kill-switch off, no repo, no task words, an
    unexpected exception) returns ``{}``, matching the field's behavior
    before #912.
    """
    try:
        raw_enabled = os.environ.get(ENABLED_ENV, "1").strip().lower()
        if raw_enabled in _FALSY:
            return {}
        if not selfevo_repo:
            return {}
        task_words = _task_words(task_title, target_path, instructions)
        if not task_words:
            return {}

        err, error_provenance, less, lesson_provenance, from_archive = _select_cards(
            Path(selfevo_repo) / "lessons", task_words
        )

        result: dict[str, Any] = {}
        if err:
            err_card: dict[str, Any] = {
                "id": err.get("id"),
                "title": _cap(err.get("title"), _TITLE_CAP),
                "root_cause": _cap(err.get("root_cause"), _TEXT_CAP),
                "prevention": _cap(err.get("prevention"), _TEXT_CAP),
            }
            # Add related hint if present (#1095): one compact line, absent when empty.
            _err_related = _related_hint_for(err)
            if _err_related:
                err_card["related"] = _err_related
            result["relevant_error"] = err_card

        if less:
            less_card: dict[str, Any] = {
                "id": less.get("id"),
                "title": _cap(less.get("title"), _TITLE_CAP),
                "approach": _cap(less.get("approach"), _TEXT_CAP),
                "reusable_insight": _cap(less.get("reusable_insight"), _TEXT_CAP),
                **({"source": "lesson_index_archive"} if from_archive else {}),
                **({"problem": _cap(less.get("problem"), _TEXT_CAP),
                    "solution": _cap(less.get("solution"), _TEXT_CAP)}
                   if less.get("problem") and less.get("solution") else {}),
            }
            # Add related hint if present (#1095): one compact line, absent when empty.
            _less_related = _related_hint_for(less)
            if _less_related:
                less_card["related"] = _less_related
            result["relevant_lesson"] = less_card

        result["selection_provenance"] = _provenance_envelope(
            "executor_prompt_context", err, error_provenance, less, lesson_provenance
        )
        return result
    except Exception:
        return {}
