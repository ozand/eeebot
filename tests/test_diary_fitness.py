"""ADR-028 rule 5 (#1812): the diary-read counter.

Reuses the exact sidecar shape #939 Part C proved for skills
(one unconditional marker row per cycle, a bounded ``.jsonl``, a
rolling-window census computed from those rows) -- see the module
docstring in ``nanobot.runtime.diary_fitness`` for why this is not a
second scan-rate mechanism.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import diary_fitness


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def test_record_cycle_diary_read_marks_todays_read_and_its_position(tmp_path: Path, monkeypatch):
    fixed_now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(diary_fitness, "datetime", _FixedDatetime)

    row = diary_fitness.record_cycle_diary_read(
        tmp_path / "state",
        cycle_id="c1",
        reads=[{"day": "2026-09-19", "position": 3}, {"day": "2026-09-20", "position": 5}],
    )
    assert row["diary_read"] is True
    assert row["tool_call_position"] == 5
    assert row["day"] == "2026-09-20"
    assert row["days_read"] == ["2026-09-19", "2026-09-20"]


def test_record_cycle_diary_read_first_position_wins_when_read_twice(tmp_path: Path):
    row = diary_fitness.record_cycle_diary_read(
        tmp_path / "state",
        cycle_id="c1",
        reads=[
            {"day": diary_fitness._today(), "position": 7},
            {"day": diary_fitness._today(), "position": 2},
        ],
    )
    assert row["tool_call_position"] == 2


def test_record_cycle_diary_read_is_unconditional_marker_even_with_no_reads(tmp_path: Path):
    """A cycle that read nothing must still leave a row -- distinguishable
    from a cycle where this was never called at all (instrumentation off,
    an older release, a crash)."""
    row = diary_fitness.record_cycle_diary_read(tmp_path / "state", cycle_id="c1", reads=[])
    assert row["diary_read"] is False
    assert row["tool_call_position"] is None
    assert row["days_read"] == []

    rows = diary_fitness._read_cycle_scans(tmp_path / "state")
    assert rows is not None and len(rows) == 1


def test_record_cycle_diary_read_ignores_a_read_of_someone_elses_day(tmp_path: Path):
    row = diary_fitness.record_cycle_diary_read(
        tmp_path / "state", cycle_id="c1", reads=[{"day": "2020-01-01", "position": 1}],
    )
    assert row["diary_read"] is False
    assert row["tool_call_position"] is None
    assert row["days_read"] == ["2020-01-01"]


def test_diary_read_rate_reports_no_data_when_sidecar_absent(tmp_path: Path):
    result = diary_fitness.diary_read_rate(tmp_path / "state")
    assert result["ok"] is False
    assert result["reason"] == "reads_unavailable"
    assert result["rate"] is None


def test_diary_read_rate_computes_fraction_over_the_window(tmp_path: Path):
    state = tmp_path / "state"
    for i, read in enumerate([True, True, False]):
        diary_fitness.record_cycle_diary_read(
            state, cycle_id=f"c{i}",
            reads=[{"day": diary_fitness._today(), "position": 1}] if read else [],
        )
    result = diary_fitness.diary_read_rate(state, now=datetime.now(timezone.utc))
    assert result["ok"] is True
    assert result["cycles_in_window"] == 3
    assert result["reads_in_window"] == 2
    assert result["rate"] == 2 / 3


def test_diary_read_rate_excludes_rows_outside_the_window(tmp_path: Path):
    state = tmp_path / "state"
    old_ts = _iso(datetime.now(timezone.utc) - timedelta(days=45))
    sidecar_dir = state / "diary_fitness"
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "cycle_scans.jsonl").write_text(
        '{"cycle_id": "old", "ts": "%s", "day": "2020-01-01", "diary_read": true, '
        '"tool_call_position": 1, "days_read": ["2020-01-01"]}\n' % old_ts,
        encoding="utf-8",
    )
    diary_fitness.record_cycle_diary_read(state, cycle_id="fresh", reads=[])
    result = diary_fitness.diary_read_rate(state)
    assert result["cycles_in_window"] == 1
    assert result["reads_in_window"] == 0


def test_write_diary_read_rate_persists_the_census(tmp_path: Path):
    state = tmp_path / "state"
    diary_fitness.record_cycle_diary_read(
        state, cycle_id="c1", reads=[{"day": diary_fitness._today(), "position": 1}],
    )
    result = diary_fitness.write_diary_read_rate(state)
    assert result["ok"] is True
    assert result["written"] is True
    payload_path = state / diary_fitness.CENSUS_REL
    assert payload_path.is_file()

    import json
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["schema"] == diary_fitness.CENSUS_SCHEMA
    assert payload["reads_in_window"] == 1


# ---------------------------------------------------------------------------
# #1844 -- diary_written, the write-side sibling of diary_read.
# ---------------------------------------------------------------------------


def test_record_cycle_diary_read_records_diary_written_true(tmp_path: Path):
    row = diary_fitness.record_cycle_diary_read(
        tmp_path / "state", cycle_id="c1", reads=[], wrote=True,
    )
    assert row["diary_written"] is True


def test_record_cycle_diary_read_defaults_diary_written_false(tmp_path: Path):
    """A cycle that never passes ``wrote`` -- an older release, or a cycle
    that touched nothing -- must not be silently counted as a write."""
    row = diary_fitness.record_cycle_diary_read(tmp_path / "state", cycle_id="c1", reads=[])
    assert row["diary_written"] is False


def test_diary_write_rate_reports_no_data_when_sidecar_absent(tmp_path: Path):
    result = diary_fitness.diary_write_rate(tmp_path / "state")
    assert result["ok"] is False
    assert result["reason"] == "writes_unavailable"
    assert result["write_rate"] is None


def test_diary_write_rate_computes_fraction_over_the_window(tmp_path: Path):
    state = tmp_path / "state"
    for i, wrote in enumerate([True, True, False]):
        diary_fitness.record_cycle_diary_read(state, cycle_id=f"c{i}", reads=[], wrote=wrote)
    result = diary_fitness.diary_write_rate(state, now=datetime.now(timezone.utc))
    assert result["ok"] is True
    assert result["cycles_in_window"] == 3
    assert result["writes_in_window"] == 2
    assert result["write_rate"] == 2 / 3


def test_write_diary_read_rate_persists_the_write_rate_alongside_the_read_rate(tmp_path: Path):
    """#1844 AC: the write rate is measured from the first day, published
    in the same census file rather than a second, likely-unread one."""
    state = tmp_path / "state"
    diary_fitness.record_cycle_diary_read(
        state, cycle_id="c1", reads=[{"day": diary_fitness._today(), "position": 1}], wrote=True,
    )
    result = diary_fitness.write_diary_read_rate(state)
    assert result["ok"] is True

    import json
    payload = json.loads((state / diary_fitness.CENSUS_REL).read_text(encoding="utf-8"))
    assert payload["reads_in_window"] == 1
    assert payload["writes_in_window"] == 1
    assert payload["write_rate"] == 1.0


def test_record_cycle_diary_read_fails_open_on_bad_state_dir(tmp_path: Path):
    """A write error must never raise into the caller -- same fail-open
    contract as every other writer in this module."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    row = diary_fitness.record_cycle_diary_read(blocker, cycle_id="c1", reads=[])
    assert row == {}
