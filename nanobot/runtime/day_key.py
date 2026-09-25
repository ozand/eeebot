"""ADR-029 (#1831), step 1 of 3: the ONE canonical helper for deriving a
calendar-day key from the HOST's LOCAL wall clock, replacing the ad-hoc
``date.today()`` / UTC-midnight day boundaries scattered across
``%Y-%m-%d``-keyed writers (see the PR #1831 census comment for the full
22-site list, three modules).

Not the same concept as :mod:`nanobot.runtime.day_clock`: that module's
``DAY_BOUNDARY_HOUR_UTC`` is ADR-026's deliberately-UTC "day is a cycle"
boundary (deep sleep, day actions) and is explicitly out of scope for this
migration. :mod:`nanobot.runtime.day_diary` has its own known bug
(``day_diary._today()``, tracked separately as #1877) -- do not migrate it
from here; a different pane owns that fix.

Step 1 shipped :func:`day_key` / :func:`local_offset` / :func:`is_transition_day`
/ :func:`transition_day_hours` -- no caller migrated yet.
Step 2 (this PR) moves the WRITERS together -- ``cycle_ledger.py``'s
rotation naming and ``state_access.py:119``'s archive-window selection
(``cycle_ledger`` is the only writer for ``cycles-*.jsonl.gz``, so this is
the one pairing that must move in lockstep; ``state_access.py:295``
(``run_window``) parses ``runs-*.jsonl.gz``, written by
``nanobot.crash_record`` -- a writer NOT migrated here, so that line is
untouched on purpose, still 100% UTC-consistent with its own writer; see
this PR's own body/issue comment for that scope note). Adds the cutover
marker mechanism this step needs: :func:`cutover_marker_path` /
:func:`load_cutover` / :func:`record_cutover_if_absent` -- one JSON
marker per ledger directory, written exactly once, at the instant a
writer first runs under the new local-day scheme (never backdated, never
hand-edited), plus :func:`archive_day_bounds` for a reader (here,
``state_access``'s own archive-selection, not a step-3 reader) to
correctly bound both UTC- and local-keyed archives around that instant.
Step 3 (third PR, gated on proof this PR's old UTC-keyed rows still read
correctly): move the remaining readers.

Transition day -- mandatory, designed here rather than deferred: the one
calendar day spanning the cutover from a UTC-anchored day boundary to
this host-local one is short or long by exactly the host's UTC offset at
cutover time (21h or 27h for a 3-hour offset -- the host is MSK, UTC+3,
no DST since Russia dropped it in 2014). Nothing here hardcodes "MSK" or
"3 hours": :func:`local_offset` reads whatever offset the host's own
clock reports, so the formula holds for any host. A writer migrated in
step 2 must record :func:`is_transition_day` / :func:`transition_day_hours`
alongside its ``day_key`` for that one row, so a trend reader (step 3)
can exclude it structurally instead of relying on someone remembering
which date the migration PR landed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Format every day key in this codebase's migrated writers/readers uses.
DAY_KEY_FORMAT = "%Y-%m-%d"

#: UTC instant the cutover from UTC-midnight to host-local-midnight day
#: boundaries actually deploys. ``None`` in this step-1 PR -- no writer
#: has cut over yet, so :func:`is_transition_day` and
#: :func:`transition_day_hours` correctly report "no transition day
#: exists" until step 2's PR sets this to the real deploy instant. Real
#: writers (step 2+) do not set this module global at all -- each ledger
#: directory has its OWN cutover, persisted via :func:`record_cutover_if_absent`
#: and loaded with :func:`load_cutover`, then threaded through explicitly
#: (see :data:`_UNSET`). This global exists only for this module's own
#: tests and any single-process caller with exactly one ledger.
CUTOVER_UTC: "datetime | None" = None

#: Sentinel distinguishing "caller didn't pass cutover_utc, use the module
#: global" from "caller explicitly passed None" (a ledger that legitimately
#: has no cutover yet) -- ``None`` itself cannot mean both.
_UNSET = object()

#: Filename of the per-ledger marker recording the one cutover instant.
CUTOVER_MARKER_FILENAME = "day_key_cutover.json"


def cutover_marker_path(ledger_dir: "Path | str") -> Path:
    """Path to the cutover marker inside *ledger_dir* (the same directory
    a writer -- e.g. ``cycle_ledger``'s ``ledger_dir`` -- rotates archives
    in). One marker per ledger directory: each writer migrates on its own
    schedule, so there is no single global cutover across the codebase.
    """
    return Path(ledger_dir) / CUTOVER_MARKER_FILENAME


def load_cutover(marker_path: "Path | str") -> "datetime | None":
    """Read the recorded cutover instant, or ``None`` if no marker exists
    yet (this ledger's writer has not migrated) or it is unreadable
    (fail-open: a corrupt marker reads the same as "not migrated yet",
    never crashes a caller)."""
    try:
        raw = json.loads(Path(marker_path).read_text(encoding="utf-8"))
        value = raw["cutover_utc"]
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def record_cutover_if_absent(
    marker_path: "Path | str", now: "datetime | None" = None
) -> "tuple[datetime, bool]":
    """Idempotently record the cutover instant for one ledger directory.

    This IS the "writer marks the transition day at the moment it
    happens, not retroactively or by hand" mechanism (ADR-029 condition
    1): the FIRST call from a given writer that finds no marker at
    *marker_path* writes *now* (default: the real current UTC instant) as
    the cutover and returns ``(now, True)`` -- every later call, from this
    process or any other, finds the marker already there and returns the
    ORIGINAL recorded instant with ``(cutover, False)``, never overwriting
    it. A caller uses the ``True`` flag as its one signal to emit that
    day's transition marker (see ``cycle_ledger.append_event``) -- exactly
    once, ever, for that ledger.

    Fail-open and best-effort: on any write error, still returns
    ``(now, True)`` for THIS call (the caller can still act on the
    transition), but the marker itself may not have been durably written --
    a acceptable trade-off matching every other best-effort writer in this
    codebase (see :mod:`nanobot.runtime.cycle_ledger`'s own module
    docstring) since losing this one marker only means a later call
    retries the same detection, not silent data loss.
    """
    existing = load_cutover(marker_path)
    if existing is not None:
        return existing, False
    moment = now if now is not None else datetime.now(timezone.utc)
    try:
        path = Path(marker_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"cutover_utc": moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")})
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass
    return moment, True


def local_offset(now: "datetime | None" = None) -> timedelta:
    """Host's current UTC offset (e.g. ``timedelta(hours=3)`` for MSK).

    *now* defaults to the system clock (``datetime.now().astimezone()``).
    Pass an aware *now* in tests to pin an offset without needing to run
    on a host actually set to that timezone.
    """
    moment = now if now is not None else datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    offset = moment.utcoffset()
    return offset if offset is not None else timedelta(0)


def day_key(now: "datetime | None" = None, *, local_tz: Any = None) -> str:
    """Host-local calendar day key, e.g. "2026-09-22".

    *now* defaults to the system's local clock
    (datetime.now().astimezone(local_tz), aware). Pass an aware *now* in
    tests to pin a specific host timezone, e.g.
    datetime(2026, 1, 1, 23, 30, tzinfo=ZoneInfo("Europe/Moscow")) --
    this is the one parameter every migrated writer/reader must thread
    through so its own tests can do the same without depending on the
    machine running them. A naive *now* is used as-is (Python's own
    convention: a naive datetime is already "local").

    Timezone offset rule (decided by offset value, never object identity):
    - Zero-offset datetimes (UTC, regardless of whether represented by
      timezone.utc, ZoneInfo("UTC"), or timezone(timedelta(0))):
      converted to *local_tz* if specified, otherwise converted to host-local
      via astimezone().
    - Non-zero-offset datetimes: converted to *local_tz* if specified;
      otherwise formatted as-is (treated as an already-localized moment).
    """
    if now is None:
        moment = datetime.now().astimezone(local_tz)
    elif now.tzinfo is None:
        moment = now
    elif local_tz is not None:
        moment = now.astimezone(local_tz)
    elif now.utcoffset() == timedelta(0):
        moment = now.astimezone()
    else:
        moment = now
    return moment.strftime(DAY_KEY_FORMAT)


def is_transition_day(key: str, *, local_tz: Any = None, cutover_utc: Any = _UNSET) -> bool:
    """True if *key* (a :func:`day_key` string) is the one calendar day
    the UTC-to-local cutover falls inside -- checked against BOTH the old
    UTC-anchored key and the new local-anchored key for the cutover
    instant, since a writer or reader mid-migration may be looking at
    either numbering for that instant.

    *cutover_utc* defaults to :data:`CUTOVER_UTC` (module global, used by
    this module's own step-1 tests); real callers in step 2+ load the
    actual per-ledger cutover from a persisted marker
    (:func:`load_cutover`) and pass it explicitly here -- there is exactly
    one migration event per ledger directory, not one global for the
    whole process. Always ``False`` when the resolved cutover is ``None``
    (no writer in that ledger has cut over yet).

    *local_tz* is the host's local timezone (e.g. ``ZoneInfo("Europe/
    Moscow")``); defaults to the system's own (``astimezone()`` with no
    argument) exactly like :func:`day_key`'s default. Pass it explicitly
    in tests so the result does not depend on the machine running them.
    """
    cutover = CUTOVER_UTC if cutover_utc is _UNSET else cutover_utc
    if cutover is None:
        return False
    utc_key = cutover.astimezone(timezone.utc).strftime(DAY_KEY_FORMAT)
    local_moment = cutover.astimezone(local_tz) if local_tz is not None else cutover.astimezone()
    local_key = local_moment.strftime(DAY_KEY_FORMAT)
    return key in (utc_key, local_key)


def transition_day_hours(
    offset: "timedelta | None" = None, *, cutover_utc: Any = _UNSET
) -> float:
    """True wall-clock length, in hours, of the one calendar day a
    UTC-to-local cutover spans.

    A cutover shifts the day boundary by the host's UTC offset: switching
    to a boundary that lands *earlier* in UTC terms than the one it
    replaces (host ahead of UTC, e.g. MSK's +3h) shortens that one day;
    switching to a *later* boundary (host behind UTC) lengthens it. Both
    directions collapse to one formula: ``24 - offset_hours``.

    *offset* defaults to :func:`local_offset`. *cutover_utc* defaults to
    :data:`CUTOVER_UTC` like :func:`is_transition_day` -- see its
    docstring for why real callers pass a per-ledger value explicitly.
    Returns ``24.0`` exactly when the resolved cutover is ``None``.
    """
    cutover = CUTOVER_UTC if cutover_utc is _UNSET else cutover_utc
    if cutover is None:
        return 24.0
    offset = offset if offset is not None else local_offset()
    return 24.0 - offset.total_seconds() / 3600.0


def archive_day_bounds(
    day: str, cutover_utc: "datetime | None", local_tz: Any = None
) -> "tuple[datetime, datetime]":
    """UTC ``[start, end)`` instants that archive day-key *day* spans --
    the one function :func:`nanobot.runtime.state_access`'s window logic
    needs to correctly select ``cycles-<day>.jsonl.gz`` archives across a
    mixed UTC-keyed / local-keyed rotation history (step 2, condition 2/3:
    the old and new naming must both resolve to the correct real-time
    span, including the one short/long transition day).

    *day* was assigned by the writer using WHICHEVER scheme was active at
    the moment it rotated that file -- never re-decided here. A day whose
    entire UTC-anchored span (``day`` 00:00 UTC through +24h) ends at or
    before *cutover_utc* was necessarily rotated under the OLD UTC scheme
    (the new scheme's day-naming, once live, is never used to mint a UTC
    key again). Every other day -- including the one that contains
    *cutover_utc* -- was rotated under the NEW host-local scheme, so its
    bounds are computed in *local_tz* (default: system local, like every
    other function here), using :func:`transition_day_hours` instead of a
    flat 24h for the specific day :func:`is_transition_day` flags.

    *cutover_utc* is ``None`` for a ledger no writer has migrated yet --
    every day is then treated as an ordinary UTC day, matching pre-#1831
    behaviour exactly.
    """
    naive = datetime.strptime(day, DAY_KEY_FORMAT)
    utc_start = naive.replace(tzinfo=timezone.utc)
    utc_end = utc_start + timedelta(hours=24)
    if cutover_utc is None and local_tz is None:
        return utc_start, utc_end
    if cutover_utc is not None and utc_end <= cutover_utc:
        return utc_start, utc_end
    tz = local_tz if local_tz is not None else datetime.now().astimezone().tzinfo
    local_start = naive.replace(tzinfo=tz)
    if is_transition_day(day, local_tz=tz, cutover_utc=cutover_utc):
        local_next_midnight = local_start + timedelta(days=1)
        offset = local_offset(local_next_midnight)
        if offset >= timedelta(0):
            return utc_start, local_next_midnight
        else:
            return local_start, local_start + timedelta(hours=transition_day_hours(offset, cutover_utc=cutover_utc))
    return local_start, local_start + timedelta(days=1)
