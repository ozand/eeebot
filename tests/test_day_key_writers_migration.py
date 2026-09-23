"""ADR-029 (#1831) step 2: cycle_ledger.py's rotation naming +
state_access.py's archive-window selection move onto
nanobot.runtime.day_key together.

Four properties this file exists to prove (matching the four conditions
the PR must satisfy):

1. The writer marks a transition day dynamically, at the moment its
   day-key boundary actually moves -- never for a brand-new ledger (which
   was never on the UTC scheme), never twice, never backdated.
2. A reader (state_access.ledger_window) correctly reads a MIXED set of
   archives -- some named under the old UTC scheme, some under the new
   local scheme -- in one query.
3. That reader does not silently drop an archive at the exact boundary a
   naive flat-24h-UTC assumption would have miscounted.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from nanobot.runtime import cycle_ledger, day_key, state_access

#: Fixed, DST-free offset chosen to exercise the DANGEROUS direction (a
#: host BEHIND UTC): here, a naive flat-UTC reader UNDER-estimates an
#: archive's true end and would silently drop it near the boundary. MSK
#: (the real host, +3h ahead) only ever over-reads in that scenario --
#: safe but untested by it -- so this offset is what actually proves
#: condition 3, independent of which host happens to run this migration.
NEG5 = timezone(timedelta(hours=-5))


def _row(phase: str, ts: str, **extra) -> dict:
    return {"phase": phase, "ts": ts, **extra}


# ---------------------------------------------------------------------------
# Condition 1: the writer marks the transition day dynamically.
# ---------------------------------------------------------------------------


def test_rotate_names_archive_by_local_mtime_day_not_utc(tmp_path, monkeypatch):
    """The exact bug this migration fixes: rotation must name an archive
    by the file's LOCAL calendar day, not the UTC one -- these differ for
    the same real instant whenever the local offset pushes it across
    midnight."""
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    active = ledger_dir / "cycles.jsonl"
    active.write_text(json.dumps(_row("outcome", "2026-06-15T20:00:00Z")) + "\n", encoding="utf-8")

    # Real instant (UTC) is 2026-06-16T03:30Z -- a UTC-midnight reading
    # says "2026-06-16"; the SAME instant read in NEG5 says "2026-06-15".
    monkeypatch.setattr(
        cycle_ledger, "_file_mtime_local", lambda _path: datetime(2026, 6, 15, 22, 30, tzinfo=NEG5)
    )

    cycle_ledger._rotate_and_prune(ledger_dir, active, "2026-06-16", 90)

    assert not active.exists()
    assert (ledger_dir / "cycles-2026-06-15.jsonl.gz").exists(), "must use the LOCAL day, not UTC"
    assert not (ledger_dir / "cycles-2026-06-16.jsonl.gz").exists()


def test_append_event_fresh_ledger_records_no_transition_marker(tmp_path):
    """A ledger with NO prior history was never on the UTC scheme -- its
    first-ever write must not fabricate a transition day."""
    state_dir = tmp_path / "state"
    cycle_ledger.append_event(state_dir, {"phase": "started", "cycle_id": "c1"})

    ledger_dir = state_dir / "ledger"
    lines = (ledger_dir / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
    phases = [json.loads(line)["phase"] for line in lines]
    assert "day_boundary_transition" not in phases
    # The cutover marker is still recorded (this ledger IS on the local
    # scheme from birth) -- just without a fabricated transition row.
    assert day_key.load_cutover(day_key.cutover_marker_path(ledger_dir)) is not None


def test_append_event_marks_transition_day_once_for_existing_ledger(tmp_path, monkeypatch):
    """A ledger with genuine prior (pre-migration) history gets exactly
    ONE ``day_boundary_transition`` row, written at the instant the first
    post-migration call happens -- and never a second one on a later call."""
    state_dir = tmp_path / "state"
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "cycles.jsonl").write_text(
        json.dumps(_row("outcome", "2026-01-09T12:00:00Z", cycle_id="old")) + "\n", encoding="utf-8"
    )
    # Genuine prior history -- an already-rotated archive, not merely an
    # active file (a bare active file proves nothing: any brand-new
    # ledger has one moments after its first write too).
    import gzip

    with gzip.open(ledger_dir / "cycles-2026-01-08.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_row("outcome", "2026-01-08T12:00:00Z", cycle_id="older")) + "\n")

    # This IS the moment the writer first runs under the new local-day
    # code: real instant 2026-01-10T20:00:00Z, expressed in NEG5 (so
    # local day_key() below reads "2026-01-10"). Pin the active file's
    # mtime-day to the SAME day for both calls below so this test is only
    # about the transition marker, not about rotation timing (that has
    # its own dedicated test) -- otherwise the file's REAL (test-machine)
    # mtime day would trigger an unrelated rotation between the two calls
    # and carry the marker row into an archive instead of staying put.
    cutover_now = datetime(2026, 1, 10, 15, 0, tzinfo=NEG5)
    monkeypatch.setattr(cycle_ledger, "_file_mtime_local", lambda _path: cutover_now)
    cycle_ledger.append_event(state_dir, {"phase": "started", "cycle_id": "new"}, now=cutover_now)

    marker_path = day_key.cutover_marker_path(ledger_dir)
    recorded = day_key.load_cutover(marker_path)
    assert recorded == cutover_now.astimezone(timezone.utc)

    lines = (ledger_dir / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    transitions = [r for r in rows if r.get("phase") == "day_boundary_transition"]
    assert len(transitions) == 1, "exactly one transition marker, not zero, not many"
    assert transitions[0]["day_key"] == "2026-01-10"
    assert transitions[0]["hours"] == 29.0  # 24 - (-5)

    # A second call, same ledger: must NOT duplicate the marker.
    cycle_ledger.append_event(
        state_dir, {"phase": "outcome", "cycle_id": "new"}, now=cutover_now + timedelta(hours=1)
    )
    lines = (ledger_dir / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    transitions = [r for r in rows if r.get("phase") == "day_boundary_transition"]
    assert len(transitions) == 1, "must not fire twice"


# ---------------------------------------------------------------------------
# Conditions 2 & 3: state_access reads mixed UTC/local archives, correctly,
# at the exact boundary.
# ---------------------------------------------------------------------------


def test_ledger_window_reads_mixed_utc_and_local_archives(tmp_path):
    """One query, one window: an OLD UTC-keyed archive and a NEW
    local-keyed archive both surface their rows -- not a reasoning
    argument, an actual mixed-file read."""
    state_dir = tmp_path / "state"
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True)

    cutover_utc = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)
    day_key.record_cutover_if_absent(day_key.cutover_marker_path(ledger_dir), cutover_utc)

    import gzip

    # OLD scheme archive: entirely before cutover, UTC-day-keyed.
    with gzip.open(ledger_dir / "cycles-2026-01-05.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_row("outcome", "2026-01-05T10:00:00Z", cycle_id="old-row")) + "\n")

    # NEW scheme archive: an ordinary local day well after the cutover.
    with gzip.open(ledger_dir / "cycles-2026-01-12.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_row("outcome", "2026-01-12T10:00:00Z", cycle_id="new-row")) + "\n")

    (ledger_dir / "cycles.jsonl").write_text(
        json.dumps(_row("outcome", "2026-01-13T10:00:00Z", cycle_id="active-row")) + "\n", encoding="utf-8"
    )

    result = state_access.ledger_window(
        state_dir,
        since_ts="2026-01-01T00:00:00Z",
        local_tz=NEG5,
        now=datetime(2026, 1, 14, 0, 0, tzinfo=timezone.utc),  # keep "since" inside retention
    )
    cycle_ids = {row["cycle_id"] for row in result.rows}
    assert cycle_ids == {"old-row", "new-row", "active-row"}
    assert result.status == "complete"


def test_ledger_window_boundary_no_silent_drop_across_transition(tmp_path):
    """Condition 3: the boundary case, not the middle of the day.

    With offset -5h, the transition day is 29h long -- 5h LONGER than a
    naive flat-24h-UTC reading of the same filename would assume. Pick
    ``since`` inside that 5h gap: a naive reader (the old, unfixed
    ``_ledger_sources``) computes the archive's end as
    ``day + 1 day`` = 2026-01-11T00:00:00Z, which is BEFORE ``since`` --
    so it would exclude the archive outright, silently losing the row
    inside it. The fixed reader must include it.
    """
    state_dir = tmp_path / "state"
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True)

    cutover_utc = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)
    day_key.record_cutover_if_absent(day_key.cutover_marker_path(ledger_dir), cutover_utc)

    # Sanity-check the boundary math this test depends on before trusting
    # the higher-level assertions below.
    naive_end = datetime(2026, 1, 11, 0, 0, tzinfo=timezone.utc)
    _true_start, true_end = day_key.archive_day_bounds("2026-01-10", cutover_utc, local_tz=NEG5)
    assert true_end == datetime(2026, 1, 11, 10, 0, tzinfo=timezone.utc)
    assert true_end > naive_end, "the whole point: true end is LATER than a naive 24h reading"

    since_ts = "2026-01-11T05:00:00Z"  # strictly between naive_end and true_end
    assert naive_end < datetime.fromisoformat(since_ts.replace("Z", "+00:00")) < true_end

    import gzip

    with gzip.open(ledger_dir / "cycles-2026-01-10.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_row("outcome", "2026-01-11T07:00:00Z", cycle_id="boundary-row")) + "\n")
    (ledger_dir / "cycles.jsonl").write_text("", encoding="utf-8")

    result = state_access.ledger_window(state_dir, since_ts=since_ts, local_tz=NEG5)
    cycle_ids = {row["cycle_id"] for row in result.rows}
    assert "boundary-row" in cycle_ids, "silently dropped at the transition-day boundary"
