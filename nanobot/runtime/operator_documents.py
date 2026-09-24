"""ADR-034 rules 2 and 3: one resolver per operator document.

Three documents, three roots, three resolvers — each the ONLY path any
reader may use to obtain its document:

    charter               <release root>/goals.md
    operator priorities   state/goals/goal_text.json
    derived priorities    state/goals/derived_priorities.json

No resolver here implements a fallback chain (the instance-repo lookups,
``state_dir/goals.md``, and ``RELEASE_ROOT/host/eeepc/etc/goal_text.json``
paths ADR-034 condemns are simply never constructed). Every resolver
returns one of ``text``/``absent``/``unreadable`` with a machine reason —
never the document's own bytes in that reason, in a log line, or in a
raised exception. A size cap is enforced before any read: over the cap a
document is ``unreadable`` with reason ``oversize``, never truncated.

The operator-priority resolver additionally collapses to the four rule-3
list states (``present``/``all_completed``/``empty``/``unavailable``),
reusing the existing done-detection filtering
(:func:`nanobot.runtime.goal_text_utils.filter_completed_priorities_from_goal_text`)
so "every listed priority is completed" is judged the same way the
subagent-prompt injection already judges it. It never returns the
document's raw priority text — goal_text.json is the operator's private
(0600) document.

No reader is migrated to these resolvers here — that is ADR-034 A2, in a
separate PR. This module lands the resolvers and nothing else.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
    """Result of resolving the charter or the derived-priorities document."""

    state: str  # text | absent | unreadable
    text: str = ""
    reason: str = ""


@dataclass(frozen=True)
class PriorityResolution:
    """Result of resolving the operator priority list (rule 3's four states)."""

    state: str  # present | all_completed | empty | unavailable
    reason: str = ""
    open_count: int = 0
    completed_count: int = 0


def resolve_charter(release_root: "Path | str | None") -> DocumentResolution:
    """ADR-034 rule 2: the charter's one root is ``<release root>/goals.md``.

    No other path is ever consulted, including any instance-repo copy."""
    if release_root is None:
        return DocumentResolution(state=STATE_ABSENT, reason="no_release_root")
    return _resolve_text_file(Path(release_root) / _CHARTER_FILENAME)


def resolve_derived_priorities(state_dir: "Path | str") -> DocumentResolution:
    """ADR-034 rule 2: derived priorities' one root is
    ``state/goals/derived_priorities.json``."""
    doc = _resolve_text_file(Path(state_dir) / "goals" / "derived_priorities.json")
    if doc.state != STATE_TEXT:
        return doc
    try:
        data = json.loads(doc.text)
    except Exception:
        return DocumentResolution(state=STATE_UNREADABLE, reason="malformed_json")
    if not isinstance(data, dict):
        return DocumentResolution(state=STATE_UNREADABLE, reason="malformed_json")
    return doc


def resolve_operator_priorities(
    state_dir: "Path | str",
    *,
    selfevo_repo_root: "Path | str | None" = None,
) -> PriorityResolution:
    """ADR-034 rules 2 and 3: operator priorities' one root is
    ``state/goals/goal_text.json``. Returns one of the four rule-3 list
    states, never the document's raw text or the wording of any priority.

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
    original_entries = list(_PRIORITY_ENTRY_PATTERN.finditer(section))
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
    open_count = len(list(_PRIORITY_ENTRY_PATTERN.finditer(filtered_section)))
    completed_count = len(original_entries) - open_count

    if open_count == 0:
        return PriorityResolution(state=PRIORITY_ALL_COMPLETED, completed_count=completed_count)
    return PriorityResolution(
        state=PRIORITY_PRESENT, open_count=open_count, completed_count=completed_count
    )


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
