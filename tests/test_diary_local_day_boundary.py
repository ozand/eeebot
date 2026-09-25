"""ADR-029 (#1831, #1958): unit tests verifying diary writer and reader
boundary consistency on host-local time.

Covers:
- 00:00-03:00 MSK and 23:30 MSK boundary consistency (day_diary and diary_fitness
  resolve the exact same date-stamped file).
- Transition day (cutover 2026-09-24T10:03:03Z) does not split a local day into
  two diary files.
- Integration between day_diary writer and diary_fitness reader.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import day_diary, diary_fitness
from nanobot.runtime.day_key import CUTOVER_UTC

MSK = timezone(timedelta(hours=3))


def test_early_morning_msk_boundary_agreement():
    """Between 00:00 and 03:00 MSK (21:00-24:00 UTC previous calendar date),
    day_diary._today() and diary_fitness._today() both resolve to the local MSK date,
    yielding the exact same date-stamped diary file.
    """
    # 01:30 MSK on 2026-09-25 is 22:30 UTC on 2026-09-24
    dt_msk = datetime(2026, 9, 25, 1, 30, tzinfo=MSK)
    dt_utc = datetime(2026, 9, 24, 22, 30, tzinfo=timezone.utc)

    # 1. Day derivation agreement
    assert day_diary._today(dt_msk) == date(2026, 9, 25)
    assert day_diary._today(dt_utc) == date(2026, 9, 25)
    assert diary_fitness._today(dt_msk) == "2026-09-25"
    assert diary_fitness._today(dt_utc) == "2026-09-25"

    # 2. File relpath and content header agreement
    expected_path = "diary/2026-09-25.md"
    assert day_diary.diary_relpath(now=dt_msk) == expected_path
    assert day_diary.diary_relpath(now=dt_utc) == expected_path
    assert day_diary.new_day_file(now=dt_msk).startswith("# Diary — 2026-09-25\n\n")
    assert day_diary.new_day_file(now=dt_utc).startswith("# Diary — 2026-09-25\n\n")


def test_late_evening_msk_boundary_agreement():
    """At 23:30 MSK, day_diary and diary_fitness both resolve to the current local day."""
    dt_msk = datetime(2026, 9, 25, 23, 30, tzinfo=MSK)
    dt_utc = datetime(2026, 9, 25, 20, 30, tzinfo=timezone.utc)

    assert day_diary._today(dt_msk) == date(2026, 9, 25)
    assert diary_fitness._today(dt_msk) == "2026-09-25"
    assert day_diary.diary_relpath(now=dt_msk) == "diary/2026-09-25.md"
    assert day_diary.diary_relpath(now=dt_utc) == "diary/2026-09-25.md"


def test_boundary_sweep_across_midnight_msk():
    """Sweeping 1-minute steps across midnight MSK transitions exactly at 00:00 MSK."""
    start = datetime(2026, 9, 24, 23, 50, tzinfo=MSK)
    for minute in range(21):
        dt = start + timedelta(minutes=minute)
        expected_date = "2026-09-24" if minute < 10 else "2026-09-25"
        expected_file = f"diary/{expected_date}.md"

        assert day_diary._today(dt).isoformat() == expected_date
        assert diary_fitness._today(dt) == expected_date
        assert day_diary.diary_relpath(now=dt) == expected_file


def test_transition_day_does_not_split_local_day():
    """On cutover day (2026-09-24), the cutover occurred at 10:03:03Z (13:03:03 MSK).
    All points before, during, and after cutover across the entire local day resolve to
    the exact same date-stamped file 'diary/2026-09-24.md' without splitting.
    """
    cutover_ts = CUTOVER_UTC or datetime(2026, 9, 24, 10, 3, 3, tzinfo=timezone.utc)
    sample_timestamps = [
        datetime(2026, 9, 23, 21, 15, tzinfo=timezone.utc),  # 00:15 MSK (pre-cutover early morning)
        datetime(2026, 9, 24, 2, 0, tzinfo=timezone.utc),    # 05:00 MSK
        datetime(2026, 9, 24, 8, 30, tzinfo=timezone.utc),   # 11:30 MSK (shortly pre-cutover)
        cutover_ts - timedelta(seconds=1),                    # 1s before cutover
        cutover_ts,                                           # exact cutover point
        cutover_ts + timedelta(seconds=1),                    # 1s after cutover
        datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc),   # 18:00 MSK (post-cutover afternoon)
        datetime(2026, 9, 24, 20, 50, tzinfo=timezone.utc),  # 23:50 MSK (post-cutover late night)
    ]
    for ts in sample_timestamps:
        assert day_diary._today(ts) == date(2026, 9, 24)
        assert diary_fitness._today(ts) == "2026-09-24"
        assert day_diary.diary_relpath(now=ts) == "diary/2026-09-24.md"
        assert day_diary.new_day_file(now=ts).startswith("# Diary — 2026-09-24\n\n")


def test_record_cycle_diary_read_matches_day_diary_relpath(tmp_path: Path):
    """Integrated check: a cycle running in the 00:00-03:00 MSK window writes and reads
    the day diary; record_cycle_diary_read correctly recognizes today's read.
    """
    cycle_time = datetime(2026, 9, 25, 1, 15, tzinfo=MSK)
    state_dir = tmp_path / "state"

    # Simulated cycle actions:
    written_path = day_diary.diary_relpath(now=cycle_time)
    assert written_path == "diary/2026-09-25.md"

    # Instrument read of today's diary
    day_read = day_diary._today(now=cycle_time).isoformat()
    reads = [{"day": day_read, "position": 2}]

    row = diary_fitness.record_cycle_diary_read(
        state_dir,
        cycle_id="c_boundary_test",
        reads=reads,
        wrote=True,
        now=cycle_time,
    )

    assert row["diary_read"] is True
    assert row["tool_call_position"] == 2
    assert row["day"] == "2026-09-25"
    assert row["diary_written"] is True


def test_diary_relpath_and_new_day_file_accept_str_and_date():
    """Verify polymorphic day parameter handling in day_diary."""
    d_obj = date(2026, 9, 25)
    d_str = "2026-09-25"

    assert day_diary.diary_relpath(d_obj) == "diary/2026-09-25.md"
    assert day_diary.diary_relpath(d_str) == "diary/2026-09-25.md"
    assert day_diary.new_day_file(d_obj).startswith("# Diary — 2026-09-25\n\n")
    assert day_diary.new_day_file(d_str).startswith("# Diary — 2026-09-25\n\n")
