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
from pathlib import Path

#: ADR-034 rule 3: over this many bytes, a document resolves ``unreadable``
#: with reason ``oversize`` rather than being read at all.
DOCUMENT_SIZE_CAP_BYTES = 65536

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


@dataclass(frozen=True)
class DocumentResolution:
    """Result of resolving the charter document."""

    state: str  # text | absent | unreadable
    reason: str = ""
    text: str = field(default="", repr=False)


@dataclass(frozen=True)
class PriorityEntry:
    """One priority, structured — never the raw document."""

    number: int
    source: str  # "operator" | "derived"
    title: str = field(repr=False)
    instructions: str = field(repr=False)


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


def resolve_charter(release_root: "Path | str | None") -> DocumentResolution:
    """ADR-034 rule 2: the charter's one root is ``<release root>/goals.md``.

    No other path is ever consulted, including any instance-repo copy."""
    if release_root is None:
        return DocumentResolution(state=STATE_ABSENT, reason="no_release_root")
    return _resolve_text_file(Path(release_root) / _CHARTER_FILENAME)


def resolve_derived_priorities(state_dir: "Path | str") -> DerivedPrioritiesResolution:
    """ADR-034 rule 2: derived priorities' one root is
    ``state/goals/derived_priorities.json``. Returns its priority entries,
    labeled ``source="derived"`` (ADR-034 rule 4 provenance), never the raw
    JSON blob."""
    doc = _resolve_text_file(Path(state_dir) / "goals" / "derived_priorities.json")
    if doc.state != STATE_TEXT:
        return DerivedPrioritiesResolution(state=doc.state, reason=doc.reason)

    try:
        data = json.loads(doc.text)
    except Exception:
        return DerivedPrioritiesResolution(state=STATE_UNREADABLE, reason="malformed_json")
    if not isinstance(data, dict):
        return DerivedPrioritiesResolution(state=STATE_UNREADABLE, reason="malformed_json")

    raw_entries = data.get("priorities")
    entries: list[PriorityEntry] = []
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label") or "").strip()
            body = str(entry.get("body") or "").strip()
            try:
                number = int(entry.get("number") or 0)
            except (TypeError, ValueError):
                number = 0
            if not label or not body or number <= 0:
                continue
            entries.append(
                PriorityEntry(number=number, source=SOURCE_DERIVED, title=label, instructions=body)
            )
    return DerivedPrioritiesResolution(state=STATE_TEXT, entries=tuple(entries))


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
    doc = _resolve_text_file(Path(state_dir) / "goals" / "goal_text.json")
    if doc.state != STATE_TEXT:
        return PriorityResolution(state=PRIORITY_UNAVAILABLE, reason=doc.reason)

    try:
        data = json.loads(doc.text)
    except Exception:
        return PriorityResolution(state=PRIORITY_UNAVAILABLE, reason="malformed_json")
    if not isinstance(data, dict):
        return PriorityResolution(state=PRIORITY_UNAVAILABLE, reason="malformed_json")

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


def _resolve_text_file(path: Path) -> DocumentResolution:
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
    if size > DOCUMENT_SIZE_CAP_BYTES:
        return DocumentResolution(state=STATE_UNREADABLE, reason="oversize")
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return DocumentResolution(state=STATE_UNREADABLE, reason="read_failed")
    if not text.strip():
        return DocumentResolution(state=STATE_ABSENT, reason="empty_file")
    return DocumentResolution(state=STATE_TEXT, text=text)
