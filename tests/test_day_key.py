"""ADR-029 (#1831) step 1: nanobot.runtime.day_key.

The one property that actually distinguishes this helper from the
UTC-midnight behaviour it replaces: for MSK's three nightly hours
(21:00-24:00 UTC == 00:00-03:00 MSK), a UTC-anchored day key and a
host-local (MSK) day key must disagree. A test that only checks
``day_key()`` echoes back whatever ``now`` it was given, without ever
comparing against the UTC alternative, would pass identically whether
the helper used local time or UTC -- it would not prove ADR-029's whole
point. See day_diary.py's own docstring for the same 3-hour discrepancy,
independently documented before this migration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from nanobot.runtime import day_key as dk

MSK = ZoneInfo("Europe/Moscow")


def _utc_day_key(moment: datetime) -> str:
    """The OLD behaviour being replaced: UTC-midnight day boundary."""
    return moment.astimezone(timezone.utc).strftime(dk.DAY_KEY_FORMAT)


@pytest.mark.parametrize(
    "utc_hour,utc_minute",
    [(21, 30), (22, 0), (23, 45)],
)
def test_day_key_diverges_from_utc_in_msk_night_hours(utc_hour, utc_minute):
    """21:00-24:00 UTC is already the next calendar day in MSK (UTC+3).
    A host-local day key for these instants must differ from the
    UTC-anchored key -- the exact discrepancy ADR-029 exists to fix."""
    moment_utc = datetime(2026, 1, 1, utc_hour, utc_minute, tzinfo=timezone.utc)
    moment_msk = moment_utc.astimezone(MSK)

    local_key = dk.day_key(moment_msk)
    utc_key = _utc_day_key(moment_utc)

    assert local_key == "2026-01-02"
    assert utc_key == "2026-01-01"
    assert local_key != utc_key


def test_day_key_agrees_with_utc_outside_the_night_window():
    """Sanity check: outside the 3-hour MSK-only window, both boundaries
    land on the same calendar day, so the two functions are not simply
    always different by construction."""
    moment_utc = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    moment_msk = moment_utc.astimezone(MSK)

    assert dk.day_key(moment_msk) == _utc_day_key(moment_utc) == "2026-01-01"


def test_day_key_naive_now_used_as_is():
    naive = datetime(2026, 3, 4, 10, 0)
    assert dk.day_key(naive) == "2026-03-04"


def test_day_key_defaults_to_system_clock():
    before = datetime.now().astimezone()
    result = dk.day_key()
    after = datetime.now().astimezone()
    assert before.strftime(dk.DAY_KEY_FORMAT) <= result <= after.strftime(dk.DAY_KEY_FORMAT)


def test_local_offset_reads_the_supplied_moment():
    moscow_noon = datetime(2026, 6, 1, 12, 0, tzinfo=MSK)
    assert dk.local_offset(moscow_noon) == timedelta(hours=3)

    utc_noon = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert dk.local_offset(utc_noon) == timedelta(0)


def test_transition_day_hours_no_cutover_configured():
    """Step 1 ships with CUTOVER_UTC unset -- every day is an ordinary
    24h day until step 2 actually sets the cutover instant."""
    assert dk.CUTOVER_UTC is None
    assert dk.transition_day_hours() == 24.0
    assert dk.transition_day_hours(timedelta(hours=3)) == 24.0


def test_transition_day_hours_short_day_when_host_ahead_of_utc(monkeypatch):
    """Host ahead of UTC (MSK, +3h) switching from a UTC boundary to a
    local one that lands earlier in UTC terms shortens the one
    transition day to 21h."""
    monkeypatch.setattr(dk, "CUTOVER_UTC", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert dk.transition_day_hours(timedelta(hours=3)) == 21.0


def test_transition_day_hours_long_day_when_host_behind_utc(monkeypatch):
    """Host behind UTC (-3h) gives the other side of the same formula:
    a 27h transition day."""
    monkeypatch.setattr(dk, "CUTOVER_UTC", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert dk.transition_day_hours(timedelta(hours=-3)) == 27.0


def test_is_transition_day_false_when_no_cutover_configured():
    assert dk.CUTOVER_UTC is None
    assert dk.is_transition_day("2026-01-01") is False
    assert dk.is_transition_day("2026-01-02") is False


def test_is_transition_day_flags_both_old_and_new_keys(monkeypatch):
    """A cutover at 2026-01-01 22:00 UTC == 2026-01-02 01:00 MSK: both
    the old UTC-anchored key (2026-01-01) and the new local key
    (2026-01-02) must be flagged -- a caller mid-migration may be
    keying rows either way for that instant."""
    cutover = datetime(2026, 1, 1, 22, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dk, "CUTOVER_UTC", cutover)

    assert dk.is_transition_day("2026-01-01", local_tz=MSK) is True
    assert dk.is_transition_day("2026-01-02", local_tz=MSK) is True
    assert dk.is_transition_day("2025-12-31", local_tz=MSK) is False
    assert dk.is_transition_day("2026-01-03", local_tz=MSK) is False
