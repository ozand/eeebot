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

Step 1 (this module, this PR): the helper only. No caller is migrated
yet.
Step 2 (next PR): move the WRITERS together --
``cycle_ledger.py:135-137,149,179`` and ``state_access.py:119,295`` --
since ``ledger_window`` breaks if only one side moves.
Step 3 (third PR, gated on proof step 2's old UTC-keyed rows still read
correctly): move the readers.

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

from datetime import datetime, timedelta, timezone
from typing import Any

#: Format every day key in this codebase's migrated writers/readers uses.
DAY_KEY_FORMAT = "%Y-%m-%d"

#: UTC instant the cutover from UTC-midnight to host-local-midnight day
#: boundaries actually deploys. ``None`` in this step-1 PR -- no writer
#: has cut over yet, so :func:`is_transition_day` and
#: :func:`transition_day_hours` correctly report "no transition day
#: exists" until step 2's PR sets this to the real deploy instant.
CUTOVER_UTC: "datetime | None" = None


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


def day_key(now: "datetime | None" = None) -> str:
    """Host-local calendar day key, e.g. ``"2026-09-22"``.

    *now* defaults to the system's local clock
    (``datetime.now().astimezone()``, aware). Pass an aware *now* in
    tests to pin a specific host timezone, e.g.
    ``datetime(2026, 1, 1, 23, 30, tzinfo=ZoneInfo("Europe/Moscow"))`` --
    this is the one parameter every migrated writer/reader must thread
    through so its own tests can do the same without depending on the
    machine running them. A naive *now* is used as-is (Python's own
    convention: a naive datetime is already "local").
    """
    moment = now if now is not None else datetime.now().astimezone()
    return moment.strftime(DAY_KEY_FORMAT)


def is_transition_day(key: str, *, local_tz: Any = None) -> bool:
    """True if *key* (a :func:`day_key` string) is the one calendar day
    the UTC-to-local cutover falls inside -- checked against BOTH the old
    UTC-anchored key and the new local-anchored key for
    :data:`CUTOVER_UTC`, since a writer or reader mid-migration may be
    looking at either numbering for that instant. Always ``False`` while
    :data:`CUTOVER_UTC` is unset (this step-1 PR).

    *local_tz* is the host's local timezone (e.g. ``ZoneInfo("Europe/
    Moscow")``); defaults to the system's own (``astimezone()`` with no
    argument) exactly like :func:`day_key`'s default. Pass it explicitly
    in tests so the result does not depend on the machine running them.
    """
    if CUTOVER_UTC is None:
        return False
    utc_key = CUTOVER_UTC.astimezone(timezone.utc).strftime(DAY_KEY_FORMAT)
    local_moment = CUTOVER_UTC.astimezone(local_tz) if local_tz is not None else CUTOVER_UTC.astimezone()
    local_key = local_moment.strftime(DAY_KEY_FORMAT)
    return key in (utc_key, local_key)


def transition_day_hours(offset: "timedelta | None" = None) -> float:
    """True wall-clock length, in hours, of the one calendar day a
    UTC-to-local cutover spans.

    A cutover shifts the day boundary by the host's UTC offset: switching
    to a boundary that lands *earlier* in UTC terms than the one it
    replaces (host ahead of UTC, e.g. MSK's +3h) shortens that one day;
    switching to a *later* boundary (host behind UTC) lengthens it. Both
    directions collapse to one formula: ``24 - offset_hours``.

    *offset* defaults to :func:`local_offset`. Returns ``24.0`` exactly
    when no cutover is configured (:data:`CUTOVER_UTC` is ``None``),
    matching :func:`is_transition_day` reporting no transition day yet.
    """
    if CUTOVER_UTC is None:
        return 24.0
    offset = offset if offset is not None else local_offset()
    return 24.0 - offset.total_seconds() / 3600.0
