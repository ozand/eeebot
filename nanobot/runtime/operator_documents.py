"""ADR-034 rules 2 and 3: one resolver per operator document.

Three documents, three roots, three resolvers — each the ONLY path any
reader may use to obtain its document:

    charter               <release root>/goals.md
    operator priorities   state/goals/goal_text.json
    derived priorities    state/goals/derived_priorities.json

No resolver here implements a fallback chain (the instance-repo lookups,
``state_dir/goals.md``, and ``RELEASE_ROOT/host/eeepc/etc/goal_text.json``
paths ADR-034 condemns are simply never constructed). Every resolver
returns one of ``text``/``absent``/``unreadable`` with a machine reason.
A size cap is enforced before any read: over the cap a document is
``unreadable``/``unavailable`` with reason ``oversize``, never truncated.
A whitespace-only ``goal_text.json`` resolves ``absent`` (empty_file) at the
document level, hence ``unavailable`` for the priority list — a stray blank
file is not the same as a document that validly lists no priorities (that
is ``empty``, ADR-034 rule 3).

ADR-034's privacy rule is about *surfaces* (status, logs, error messages,
the dashboard), not about the runtime readers ADR-034 A2-A4 migrate here —
rule 5 requires the operator's actual priority text to reach the executor,
proposer and planner prompts. So the resolvers below return the parsed
document *content* to their callers (structured — number/title/instructions,
never the raw JSON blob) for those readers to build on, while:

- every content-bearing dataclass field is ``field(repr=False)``, so no
  accidental ``print()``/f-string/``logger.info("%s", res)``/assertion-diff
  of a resolution object can render the operator's private wording; and
- :func:`operator_priorities_status` returns state/reason/counts ONLY — the
  thin view a status surface (dashboard, logs) is meant to call, structurally
  incapable of carrying entry content because its type has none.

The operator-priority resolver also collapses to the four rule-3 list
states (``present``/``all_completed``/``empty``/``unavailable``), reusing
the existing done-detection filtering
(:func:`nanobot.runtime.goal_text_utils.filter_completed_priorities_from_goal_text`)
so "every listed priority is completed" is judged the same way the
subagent-prompt injection already judges it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: ADR-034 rule 3: general operator documents over this many bytes resolve
#: ``unreadable`` with reason ``oversize`` rather than being read at all.
DOCUMENT_SIZE_CAP_BYTES = 65536
#: Charter cap is defined in decoded characters for prompt construction; UTF-8
#: can use up to four bytes per character, so this bounds the read safely.
CHARTER_MAX_CHARS = 8000
CHARTER_READ_CAP_BYTES = CHARTER_MAX_CHARS * 4

STATE_TEXT = "text"
STATE_ABSENT = "absent"
STATE_UNREADABLE = "unreadable"

PRIORITY_PRESENT = "present"
PRIORITY_ALL_COMPLETED = "all_completed"
PRIORITY_EMPTY = "empty"
PRIORITY_UNAVAILABLE = "unavailable"

SOURCE_OPERATOR = "operator"
SOURCE_DERIVED = "derived"

_CHARTER_FILENAME = "goals.md"
_PRIORITY_TARGETS_MARKER = "Current priority targets:"

# Same regex family as demand._PRIORITY_PATTERN / llm_proposer._PRIORITY_PATTERN /
# goal_text_utils.filter_completed_priorities_from_goal_text — one entry per
# "(A) Priority N — Title: instructions" line.
_PRIORITY_ENTRY_PATTERN = re.compile(
    r"\([A-Za-z]\)\s*Priority\s+(\d+)\s*[—-]\s*(.+?):\s*(.+?)(?=\n\([A-Za-z]\)|\Z)",
    re.DOTALL,
)

# #1640: same "Priority N" ANYWHERE (the structured "Current priority
# targets:" section AND the free-form "Completed (do not repeat):"
# sentence alike) goal_review._existing_priority_numbers scans — the
# operator's Completed prose has no fixed shape a label-only check can
# rely on, so the number is the only thing both shapes share.
_PRIORITY_NUMBER_ANYWHERE_RE = re.compile(r"Priority\s+(\d+)\b")


@dataclass(frozen=True)
class DocumentResolution:
    """Result of resolving the charter document."""

    state: str  # text | absent | unreadable
    reason: str = ""
    text: str = field(default="", repr=False)


@dataclass(frozen=True)
class PriorityEntry:
    """One priority, structured — never the raw document. ``vector``/
    ``added_utc``/``direction`` are populated for ``source="derived"``
    entries (goal_review's existing fields); operator entries leave them
    at their default (the inline ``(V1)``/``(V2)`` tag, if any, stays
    embedded in ``title`` exactly as the pre-ADR-034 code rendered it)."""

    number: int
    source: str  # "operator" | "derived"
    title: str = field(repr=False)
    instructions: str = field(repr=False)
    vector: str = ""
    added_utc: str = ""
    direction: str = ""


@dataclass(frozen=True)
class PriorityResolution:
    """Full resolution of the operator priority list (rule 3's four states),
    including its content for the A2/A3/A4 readers that build prompts from
    it. Content fields are ``repr=False``; a status surface should use
    :func:`operator_priorities_status` instead of touching this type."""

    state: str  # present | all_completed | empty | unavailable
    reason: str = ""
    open_count: int = 0
    completed_count: int = 0
    open_entries: "tuple[PriorityEntry, ...]" = field(default_factory=tuple, repr=False)
    completed_entries: "tuple[PriorityEntry, ...]" = field(default_factory=tuple, repr=False)


@dataclass(frozen=True)
class PriorityStatus:
    """State/reason/counts only — what a status surface (dashboard, logs) is
    meant to call. Carries no entry content, so it cannot leak the
    operator's private wording by construction."""

    state: str
    reason: str = ""
    open_count: int = 0
    completed_count: int = 0


@dataclass(frozen=True)
class DerivedPrioritiesResolution:
    """Result of resolving the derived-priorities document, with its content
    for A2/A3/A4 readers. ``entries`` is ``repr=False``."""

    state: str  # text | absent | unreadable
    reason: str = ""
    entries: "tuple[PriorityEntry, ...]" = field(default_factory=tuple, repr=False)
    mtime_utc: "str | None" = None


@dataclass(frozen=True)
class OperatorDocumentMetadata:
    """``goal_text.json``'s identity/freshness metadata — ``goal_id`` and the
    file's mtime — for readers that need those, not the priority list
    (``active_goal_id``, the proposal-artifact writer, the derived-view's
    freshness report). Never the document's text."""

    state: str  # text | absent | unreadable
    reason: str = ""
    goal_id: str = ""
    mtime_utc: "str | None" = None


def resolve_charter(release_root: "Path | str | None") -> DocumentResolution:
    """ADR-034 rule 2: the charter's one root is ``<release root>/goals.md``.

    No other path is ever consulted, including any instance-repo copy."""
    if release_root is None:
        return DocumentResolution(state=STATE_ABSENT, reason="no_release_root")
    return _resolve_text_file(
        Path(release_root) / _CHARTER_FILENAME,
        max_bytes=CHARTER_READ_CAP_BYTES,
        max_chars=CHARTER_MAX_CHARS,
    )


def derived_priorities_path(state_dir: "Path | str") -> Path:
    """ADR-034 rule 2: the ONE path to ``derived_priorities.json`` — owned by
    this resolver module, not by any reader (including
    :mod:`nanobot.runtime.goal_review`, whose own
    ``read_derived_priorities`` delegates to :func:`resolve_derived_priorities`
    below rather than constructing this path itself). The writer
    (``goal_review._write_derived_priorities``) is a separate concern —
    rule 2 is about readers — and keeps its own path for now."""
    return Path(state_dir) / "goals" / "derived_priorities.json"


def resolve_derived_priorities(state_dir: "Path | str") -> DerivedPrioritiesResolution:
    """ADR-034 rule 2: derived priorities' one root is
    ``state/goals/derived_priorities.json``, located and cap-checked ONLY
    here. Returns its priority entries, labeled ``source="derived"``
    (ADR-034 rule 4 provenance), never the raw JSON blob. Entry validation
    (label/body/vector-in-{V1,V2}/number>0) lives here, not in
    :mod:`goal_review` — its ``read_derived_priorities`` is the thin
    dict-shaped adapter over this resolver, not the other way around."""
    path = derived_priorities_path(state_dir)
    doc = _resolve_text_file(path)
    mtime = _mtime_utc(path)
    if doc.state != STATE_TEXT:
        return DerivedPrioritiesResolution(state=doc.state, reason=doc.reason, mtime_utc=mtime)

    try:
        data = json.loads(doc.text)
    except Exception:
        return DerivedPrioritiesResolution(state=STATE_UNREADABLE, reason="malformed_json", mtime_utc=mtime)
    if not isinstance(data, dict):
        return DerivedPrioritiesResolution(state=STATE_UNREADABLE, reason="malformed_json", mtime_utc=mtime)

    raw_entries = data.get("priorities")
    entries: list[PriorityEntry] = []
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label") or "").strip()
            body = str(entry.get("body") or "").strip()
            vector = str(entry.get("vector") or "").strip().upper()
            try:
                number = int(entry.get("number") or 0)
            except (TypeError, ValueError):
                number = 0
            if not label or not body or vector not in ("V1", "V2") or number <= 0:
                continue
            entries.append(
                PriorityEntry(
                    number=number,
                    source=SOURCE_DERIVED,
                    title=label,
                    instructions=body,
                    vector=vector,
                    added_utc=str(entry.get("added_utc") or ""),
                    direction=str(entry.get("direction") or "").strip(),
                )
            )
    return DerivedPrioritiesResolution(state=STATE_TEXT, entries=tuple(entries), mtime_utc=mtime)


def resolve_derived_priorities_split(
    state_dir: "Path | str",
    *,
    selfevo_repo_root: "Path | str | None" = None,
) -> "tuple[tuple[PriorityEntry, ...], tuple[PriorityEntry, ...]]":
    """ADR-034 rule 4: the derived-priorities list's OWN open/completed
    split — never inferred from the operator's merged text, and never
    losing ``source="derived"`` to do it. Returns ``(open, completed)``.

    Completion is judged by rendering the resolved entries into the same
    ``"(<letter>) Priority N — Title: instructions"`` shape the operator
    document uses, then running the SAME done-detection
    (:func:`goal_text_utils.filter_completed_priorities_from_goal_text` —
    completed-demand sidecar first, then git-log heuristics) independently
    against that synthetic text. The rendering is discarded immediately;
    only which numbers survived filtering is kept, so the returned entries
    are the original, fully-populated ``PriorityEntry`` objects (vector/
    added_utc/direction intact) — never reconstructed from the synthetic
    text. Fail-open: any error returns ``(all entries, ())`` — i.e. every
    derived priority stays open, matching this module's fail-open bias
    toward not silently hiding outstanding work."""
    res = resolve_derived_priorities(state_dir)
    if res.state != STATE_TEXT or not res.entries:
        return (), ()
    try:
        from nanobot.runtime.goal_text_utils import filter_completed_priorities_from_goal_text

        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        rendered = "\n".join(
            f"({letters[i % 26]}) Priority {e.number} — {e.title}: {e.instructions}"
            for i, e in enumerate(res.entries)
        )
        text = _PRIORITY_TARGETS_MARKER + "\n" + rendered
        repo_root = Path(selfevo_repo_root) if selfevo_repo_root is not None else None
        filtered = filter_completed_priorities_from_goal_text(
            text, repo_root, state_dir=Path(state_dir)
        )
        filtered_idx = filtered.find(_PRIORITY_TARGETS_MARKER)
        filtered_section = (
            filtered[filtered_idx + len(_PRIORITY_TARGETS_MARKER):] if filtered_idx != -1 else ""
        )
        open_numbers = {
            int(m.group(1)) for m in _PRIORITY_ENTRY_PATTERN.finditer(filtered_section)
        }
        open_entries = tuple(e for e in res.entries if e.number in open_numbers)
        completed_entries = tuple(e for e in res.entries if e.number not in open_numbers)
        return open_entries, completed_entries
    except Exception:
        return res.entries, ()


def resolve_operator_priorities(
    state_dir: "Path | str",
    *,
    selfevo_repo_root: "Path | str | None" = None,
) -> PriorityResolution:
    """ADR-034 rules 2 and 3: operator priorities' one root is
    ``state/goals/goal_text.json``. Returns the four rule-3 list states
    plus, for ``present``/``all_completed``, the structured open and
    completed entries (``source="operator"``) A2/A3/A4 readers need to
    build the executor/proposer/planner prompts and the demand items —
    never the document's raw text.

    ``selfevo_repo_root`` is passed through to the existing done-detection
    filtering (git-log evidence); omit it to judge completion from the
    completed-demand sidecar alone.
    """
    doc, data = _resolve_operator_document(state_dir)
    if data is None:
        return PriorityResolution(state=PRIORITY_UNAVAILABLE, reason=doc.reason)

    raw_text = str(data.get("text") or "")
    marker_idx = raw_text.find(_PRIORITY_TARGETS_MARKER)
    if marker_idx == -1:
        return PriorityResolution(state=PRIORITY_EMPTY, reason="no_priority_section")
    section = raw_text[marker_idx + len(_PRIORITY_TARGETS_MARKER):]
    original_entries = _parse_entries(section)
    if not original_entries:
        return PriorityResolution(state=PRIORITY_EMPTY, reason="no_entries")

    from nanobot.runtime.goal_text_utils import filter_completed_priorities_from_goal_text

    repo_root = Path(selfevo_repo_root) if selfevo_repo_root is not None else None
    filtered = filter_completed_priorities_from_goal_text(
        raw_text, repo_root, state_dir=Path(state_dir)
    )
    filtered_idx = filtered.find(_PRIORITY_TARGETS_MARKER)
    filtered_section = (
        filtered[filtered_idx + len(_PRIORITY_TARGETS_MARKER):] if filtered_idx != -1 else ""
    )
    open_entries = _parse_entries(filtered_section)
    open_numbers = {e.number for e in open_entries}
    completed_entries = tuple(e for e in original_entries if e.number not in open_numbers)

    if not open_entries:
        return PriorityResolution(
            state=PRIORITY_ALL_COMPLETED,
            completed_count=len(completed_entries),
            completed_entries=completed_entries,
        )
    return PriorityResolution(
        state=PRIORITY_PRESENT,
        open_count=len(open_entries),
        completed_count=len(completed_entries),
        open_entries=tuple(open_entries),
        completed_entries=completed_entries,
    )


def resolve_operator_priority_numbers(state_dir: "Path | str") -> "frozenset[int]":
    """ADR-034 rule 4 (#1640): every "Priority N" number mentioned ANYWHERE
    in the operator's raw text — the structured "Current priority
    targets:" section AND the free-form "Completed (do not repeat):"
    sentence alike (mirrors goal_review._existing_priority_numbers, the
    retired merged_goal_text/active_derived_priorities pair's own scan).
    For cross-document dedup only: a derived entry whose number already
    appears here must never be presented as new/open — the operator's
    Completed prose has no fixed shape a label-only check can rely on, so
    the number is the only thing both shapes share. Returns a bare set of
    ints, never the text itself; absent/unreadable/empty resolves to the
    empty set (fail-open toward NOT hiding a derived entry)."""
    _doc, data = _resolve_operator_document(state_dir)
    if data is None:
        return frozenset()
    raw_text = str(data.get("text") or "")
    return frozenset(int(n) for n in _PRIORITY_NUMBER_ANYWHERE_RE.findall(raw_text))


def resolve_operator_priorities_metadata(state_dir: "Path | str") -> OperatorDocumentMetadata:
    """``goal_id`` and the file's mtime — for readers that need the
    document's identity/freshness, not its priority list content."""
    path = Path(state_dir) / "goals" / "goal_text.json"
    mtime = _mtime_utc(path)
    doc, data = _resolve_operator_document(state_dir)
    if data is None:
        return OperatorDocumentMetadata(state=doc.state, reason=doc.reason, mtime_utc=mtime)
    goal_id = data.get("goal_id")
    return OperatorDocumentMetadata(
        state=STATE_TEXT,
        goal_id=goal_id.strip() if isinstance(goal_id, str) else "",
        mtime_utc=mtime,
    )


def _resolve_operator_document(state_dir: "Path | str") -> "tuple[DocumentResolution, dict | None]":
    """Shared read+parse of ``goal_text.json`` at the document level (not the
    priority-list level) for :func:`resolve_operator_priorities_metadata`
    (and, until ADR-034 A3 removed it, ``resolve_operator_priorities_text``).
    Returns the resolution and, when it parsed as a JSON object, the parsed
    dict — never the priority list's own four states."""
    doc = _resolve_text_file(Path(state_dir) / "goals" / "goal_text.json")
    if doc.state != STATE_TEXT:
        return doc, None
    try:
        data = json.loads(doc.text)
    except Exception:
        return DocumentResolution(state=STATE_UNREADABLE, reason="malformed_json"), None
    if not isinstance(data, dict):
        return DocumentResolution(state=STATE_UNREADABLE, reason="malformed_json"), None
    return doc, data


def operator_priorities_status(
    state_dir: "Path | str",
    *,
    selfevo_repo_root: "Path | str | None" = None,
) -> PriorityStatus:
    """State/reason/counts only — for status surfaces (dashboard, logs).
    Never carries entry content, so it cannot leak the operator's private
    wording even if printed or logged directly."""
    res = resolve_operator_priorities(state_dir, selfevo_repo_root=selfevo_repo_root)
    return PriorityStatus(
        state=res.state,
        reason=res.reason,
        open_count=res.open_count,
        completed_count=res.completed_count,
    )


def _parse_entries(section: str) -> list[PriorityEntry]:
    entries: list[PriorityEntry] = []
    for m in _PRIORITY_ENTRY_PATTERN.finditer(section):
        num_str, title, instructions = m.group(1), m.group(2).strip(), m.group(3).strip()
        try:
            number = int(num_str)
        except ValueError:
            continue
        entries.append(
            PriorityEntry(number=number, source=SOURCE_OPERATOR, title=title, instructions=instructions)
        )
    return entries


def _mtime_utc(path: Path) -> "str | None":
    """ISO-8601 UTC mtime of ``path``, ``None`` when absent or unstatable."""
    try:
        if not path.is_file():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _resolve_text_file(
    path: Path, *, max_bytes: int = DOCUMENT_SIZE_CAP_BYTES,
    max_chars: int | None = None,
) -> DocumentResolution:
    """Shared read: absent / unreadable (incl. oversize) / text.

    Never truncates, and never lets the document's own bytes reach the
    returned reason — every reason here is a fixed keyword.
    """
    try:
        exists = path.is_file()
    except Exception:
        return DocumentResolution(state=STATE_UNREADABLE, reason="stat_failed")
    if not exists:
        return DocumentResolution(state=STATE_ABSENT, reason="no_file")
    try:
        size = path.stat().st_size
    except Exception:
        return DocumentResolution(state=STATE_UNREADABLE, reason="stat_failed")
    if size > max_bytes:
        return DocumentResolution(state=STATE_UNREADABLE, reason="oversize")
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return DocumentResolution(state=STATE_UNREADABLE, reason="read_failed")
    if max_chars is not None and len(text) > max_chars:
        return DocumentResolution(state=STATE_UNREADABLE, reason="oversize")
    if not text.strip():
        return DocumentResolution(state=STATE_ABSENT, reason="empty_file")
    return DocumentResolution(state=STATE_TEXT, text=text)
