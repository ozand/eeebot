"""ADR-029 (#1831) step 3: readers of day derivations migrated to host-local time.

Tests:
1. Window across transition boundary: archive with UTC-key + archive with MSK-key
   + active file -- no row lost and no row duplicated.
2. Reader of transition day (journal_story): reads the transition day span cleanly.
3. Reader of normal MSK day: 00:00-03:00 MSK (21:00-24:00 UTC) belongs to the
   local day, not yesterday.
4. default_day() derives yesterday in the host-local calendar, preventing false quiet days.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nanobot.runtime import day_key, state_access
from scripts.journal_story import default_day, load_day_journal, select_beats

MSK = ZoneInfo("Europe/Moscow")


def _row(phase: str, ts: str, **extra) -> dict:
    return {"phase": phase, "ts": ts, **extra}


def _setup_boundary_ledger(tmp_path: Path):
    """Sets up:
    - cutover at 2026-09-24T10:03:03Z (MSK transition day)
    - cycles-2026-09-23.jsonl.gz (UTC-keyed archive before cutover)
    - cycles-2026-09-24.jsonl.gz (transition day archive, MSK-keyed)
    - cycles.jsonl (active file, post-cutover normal MSK day 2026-09-25)
    """
    state_dir = tmp_path / "state"
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True)

    cutover_utc = datetime(2026, 9, 24, 10, 3, 3, tzinfo=timezone.utc)
    day_key.record_cutover_if_absent(day_key.cutover_marker_path(ledger_dir), cutover_utc)

    # 1. UTC-keyed archive (before cutover): Sep 23 15:00 UTC
    utc_rows = [
        _row("outcome", "2026-09-23T15:00:00Z", cycle_id="c-utc-1", task_title="UTC task 1"),
        _row("outcome", "2026-09-23T20:00:00Z", cycle_id="c-utc-2", task_title="UTC task 2"),
    ]
    with gzip.open(ledger_dir / "cycles-2026-09-23.jsonl.gz", "wt", encoding="utf-8") as fh:
        for r in utc_rows:
            print(json.dumps(r), file=fh)

    # 2. Transition day archive (2026-09-24 MSK): spans 00:00-21:00 UTC on Sep 24
    trans_rows = [
        _row("day_boundary_transition", "2026-09-24T10:03:03Z", day_key="2026-09-24", hours=21.0),
        _row("outcome", "2026-09-24T12:00:00Z", cycle_id="c-trans-1", task_title="Trans task 1"),
        _row("outcome", "2026-09-24T18:00:00Z", cycle_id="c-trans-2", task_title="Trans task 2"),
        _row("outcome", "2026-09-24T20:45:00Z", cycle_id="c-trans-3", task_title="Trans task 3 (23:45 MSK)"),
    ]
    with gzip.open(ledger_dir / "cycles-2026-09-24.jsonl.gz", "wt", encoding="utf-8") as fh:
        for r in trans_rows:
            print(json.dumps(r), file=fh)

    # 3. Active file: normal MSK day (2026-09-25 MSK: starts 2026-09-24T21:00:00Z)
    live_rows = [
        _row("outcome", "2026-09-24T21:15:00Z", cycle_id="c-msk-1", task_title="MSK task 1 (00:15 MSK)"),
        _row("outcome", "2026-09-25T02:00:00Z", cycle_id="c-msk-2", task_title="MSK task 2 (05:00 MSK)"),
    ]
    with open(ledger_dir / "cycles.jsonl", "wt", encoding="utf-8") as fh:
        for r in live_rows:
            print(json.dumps(r), file=fh)

    return state_dir


def test_ledger_window_spanning_transition_boundary_no_rows_lost_or_duplicated(tmp_path):
    """Window across boundary: UTC-archive + MSK-archive + active file.
    Asserts no row is lost and no row is duplicated.
    """
    state_dir = _setup_boundary_ledger(tmp_path)

    # Window from Sep 23 12:00 UTC through Sep 25 (covers all 3 sources)
    window = state_access.ledger_window(
        state_dir,
        since_ts="2026-09-23T12:00:00Z",
        local_tz=MSK,
        now=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
    )
    assert window.status == "complete"
    assert window.files_read == 3

    # All 7 cycle_ids must be surfaced
    cycle_ids = [r["cycle_id"] for r in window.rows if "cycle_id" in r]
    expected_ids = ["c-utc-1", "c-utc-2", "c-trans-1", "c-trans-2", "c-trans-3", "c-msk-1", "c-msk-2"]

    # 1. No row lost: all expected IDs present
    assert set(cycle_ids) == set(expected_ids), f"Lost rows: {set(expected_ids) - set(cycle_ids)}"

    # 2. No row duplicated: exactly 7 rows with cycle_id
    assert len(cycle_ids) == len(expected_ids), f"Duplicate rows detected: {cycle_ids}"


def test_journal_story_transition_day_reads_exact_local_span(tmp_path):
    """load_day_journal on transition day (2026-09-24):
    Reads transition day archive; does not bleed into UTC day 23 or MSK day 25.
    """
    state_dir = _setup_boundary_ledger(tmp_path)

    journal = load_day_journal(state_dir, "2026-09-24", local_tz=MSK)
    assert journal["status"] == "complete"
    assert "cycles-2026-09-24.jsonl.gz" in journal["files_read"]

    beats = select_beats(journal["rows"], day="2026-09-24", local_tz=MSK)
    beat_cycles = [b["source"]["cycle_id"] for b in beats]

    # Only transition day tasks, exactly 3
    assert beat_cycles == ["c-trans-1", "c-trans-2", "c-trans-3"]
    assert "c-utc-2" not in beat_cycles
    assert "c-msk-1" not in beat_cycles


def test_journal_story_normal_msk_day_includes_midnight_to_3am_local(tmp_path):
    """Normal MSK day 2026-09-25:
    Row at 2026-09-24T21:15:00Z is 00:15 MSK on Sep 25.
    Must belong to day 2026-09-25, NOT yesterday (2026-09-24).
    """
    state_dir = _setup_boundary_ledger(tmp_path)

    journal = load_day_journal(state_dir, "2026-09-25", local_tz=MSK)
    assert journal["status"] == "complete"

    beats = select_beats(journal["rows"], day="2026-09-25", local_tz=MSK)
    beat_cycles = [b["source"]["cycle_id"] for b in beats]

    assert "c-msk-1" in beat_cycles, "c-msk-1 (00:15 MSK) must be included in 2026-09-25"
    assert "c-msk-2" in beat_cycles, "c-msk-2 (05:00 MSK) must be included in 2026-09-25"
    assert "c-trans-3" not in beat_cycles, "c-trans-3 (23:45 MSK Sep 24) must not enter Sep 25"


def test_default_day_derives_yesterday_in_host_local_calendar():
    """default_day() at 01:30 MSK (22:30 UTC yesterday) still names yesterday MSK,
    preventing the narrator false quiet day bug (#1831).
    """
    # 01:30 MSK on 2026-09-25 is 2026-09-24T22:30:00Z in UTC.
    early_morning_msk = datetime(2026, 9, 24, 22, 30, tzinfo=timezone.utc)

    # In host-local calendar (MSK), current date is 2026-09-25, so yesterday is 2026-09-24:
    yesterday = default_day(early_morning_msk, local_tz=MSK)
    assert yesterday == "2026-09-24", f"Expected 2026-09-24, got {yesterday}"

    # Normal narrator timer execution at 03:30 MSK:
    narrator_time_msk = datetime(2026, 9, 25, 0, 30, tzinfo=timezone.utc)
    yesterday_narrator = default_day(narrator_time_msk, local_tz=MSK)
    assert yesterday_narrator == "2026-09-24"
