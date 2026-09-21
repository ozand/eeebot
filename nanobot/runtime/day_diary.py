"""ADR-028 rule 1: the day diary's path, file format, and append discipline.

Foundation for #1811 (write-at-cycle-open), #1812 (the read instruction),
and #1813 (the reflector's month fold). This issue (#1810) fixes only the
path, the file's shape, and the write discipline that makes ~90 cycles a
day safe to append to one file without clobbering each other.

Rule 1 in full: one file per day, appended through a unique marker, so
``edit_file``'s own uniqueness requirement (a fragment replacement that
refuses when ``old_text`` matches more than once -- see
``EditFileTool._find_match``) enforces the safe append, in the tool,
rather than by instruction. ``write_file`` against a diary path drops
everything it does not rewrite, which for a diary is the whole day, so it
is refused outright -- see :mod:`nanobot.agent.tools.filesystem`'s
``WriteFileTool``.

ADR-028 rule 4 (never in the prompt): nothing here is read by
``nanobot.agent.context`` or ``nanobot.agent.subagent`` -- this module has
no import of, or dependency from, either. A diary file's CONTENT is read
only via ``read_file``, at the loop's own initiative (#1812), never
assembled into the system prompt.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

#: The one directory this whole module is about. A relative, POSIX-style
#: prefix -- diary files live in the instance repository, versioned like
#: any other loop-written path, not under ``state/``.
DIARY_DIR = "diary"

#: Unique marker line a well-formed day file ends with exactly once.
#: HTML-comment-shaped and states its own purpose, so a cycle recording an
#: entry would never plausibly write this line as prose -- the one
#: property AC 3 needs (the marker must not plausibly occur inside an
#: entry). ``edit_file``'s ambiguity check (count > 1) is what actually
#: enforces "exactly once"; this string is chosen to make an accidental
#: second occurrence implausible, not merely unlikely.
DIARY_MARKER = "<!-- diary: append new entries above this line -->"

#: #1852 (ADR-031 rule 5): the planning session's output lives in its own
#: bounded region, replaced (not appended) on each run, positioned right
#: after the intro paragraph and BEFORE the growing entries list -- so the
#: current plan is at a fixed, known spot near the top of the file rather
#: than requiring a scan of however many entries the day has accumulated
#: (#1844 acceptance: "readable ... without reading the whole file").
#: Written directly by the bridge (:func:`set_plan_block`), never through
#: the loop's own ``edit_file`` -- the planning session's final response is
#: read text, not a file it produces; see ``roles/planner.md``.
PLAN_BEGIN = "<!-- diary: plan begin -->"
PLAN_END = "<!-- diary: plan end -->"
_NO_PLAN_YET = "(no plan recorded yet)"


def _today() -> date:
    """ADR-029 (#1831) migration point -- the ONE clock call this whole
    module uses to decide "what day is it".

    Currently UTC. ADR-029 found systemd timers and ``bridge.py``'s own
    ``date.today()`` running on LOCAL time (MSK) while the ledger,
    telemetry, and ``action_index`` rotation/naming run on UTC -- the two
    halves disagree on the date for 3 hours a day (21:00-24:00 UTC ==
    00:00-03:00 MSK). The diary is one of the ``%Y-%m-%d``-keyed writers
    #1831's migration must revisit (see PR body's census entry for this
    line). :func:`diary_relpath` and :func:`new_day_file` both call this
    function rather than each deciding their own clock, so that migration
    is a one-line edit here -- not a hunt across every caller.
    """
    return datetime.now(timezone.utc).date()


def diary_relpath(day: "date | None" = None) -> str:
    """Workspace-relative path for *day*'s diary file: ``diary/YYYY-MM-DD.md``
    (ADR-028 rule 1). *day* defaults to :func:`_today` -- see its docstring
    for which clock that is and why it is centralized there."""
    day = day or _today()
    return f"{DIARY_DIR}/{day.isoformat()}.md"


def is_diary_path(path: Path, workspace: Path) -> bool:
    """True if *path*, resolved against *workspace*, falls under ``diary/``.

    Used by :class:`~nanobot.agent.tools.filesystem.WriteFileTool` to
    refuse ``write_file`` there unconditionally -- a whole-file rewrite
    that does not carry the day's prior entries forward is exactly the
    hazard rule 1 exists to prevent, regardless of what the caller
    intended to write.
    """
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return False
    return rel.parts[:1] == (DIARY_DIR,)


def new_day_file(day: "date | None" = None) -> str:
    """Header + exactly one marker line -- the shape a fresh day file must
    have, built once here so a cycle never free-hands the format (AC 2).

    States, in the file itself, the distinction the format doc owes a
    reader: an entry records intent -- what a cycle is attempting and
    why -- not a report of what happened. The cycle ledger already holds
    that (see ``nanobot.runtime.state_access``).

    *day* defaults to :func:`_today` -- see its docstring for which clock
    (ADR-029/#1831 migration point).
    """
    day = day or _today()
    return (
        f"# Diary — {day.isoformat()}\n\n"
        "Entries below record intent: what a cycle is attempting and why, "
        "written at the start of its turn. This is not a report of what "
        "was done -- the ledger already holds that.\n\n"
        f"{PLAN_BEGIN}\n{_NO_PLAN_YET}\n{PLAN_END}\n\n"
        f"{DIARY_MARKER}\n"
    )


def append_entry(content: str, entry: str) -> str:
    """Replace the diary marker with ``entry`` followed by the marker again.

    A pure function over file content, exercising the same fragment-
    replace shape ``edit_file`` performs against :data:`DIARY_MARKER` --
    provided here so this module's own tests can drive many sequential
    appends directly. The loop's real route to this same result is the
    generic ``edit_file`` tool, whose own ambiguity check (refuses when
    ``old_text`` matches more than once) is what makes a doubled marker a
    refusal rather than a silent double write; this function mirrors that
    same one-marker requirement rather than re-deciding it.

    Raises :class:`ValueError` if the marker is not present exactly once --
    the file is malformed and appending to it would be a guess, not a
    safe operation.
    """
    count = content.count(DIARY_MARKER)
    if count != 1:
        raise ValueError(
            f"expected exactly one diary marker, found {count}"
        )
    return content.replace(DIARY_MARKER, entry.rstrip("\n") + "\n" + DIARY_MARKER)


def set_plan_block(content: str, plan_text: str) -> str:
    """Replace everything between :data:`PLAN_BEGIN` and :data:`PLAN_END`
    with ``plan_text`` -- REPLACE, not append: only the latest planning
    session's output is worth a reader's attention, and the position stays
    fixed so a reader never has to find it among a growing entries list.

    Raises :class:`ValueError` when either marker is missing, either occurs
    more than once, or ``PLAN_END`` does not follow ``PLAN_BEGIN`` -- the
    file is malformed and replacing a region in it would be a guess.
    """
    if content.count(PLAN_BEGIN) != 1 or content.count(PLAN_END) != 1:
        raise ValueError(
            f"expected exactly one plan-block marker pair, found "
            f"{content.count(PLAN_BEGIN)} begin / {content.count(PLAN_END)} end"
        )
    start = content.index(PLAN_BEGIN) + len(PLAN_BEGIN)
    end = content.index(PLAN_END)
    if end < start:
        raise ValueError("plan-block end marker precedes its begin marker")
    return content[:start] + "\n" + plan_text.strip() + "\n" + content[end:]
