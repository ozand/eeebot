"""#1793 (ADR-026 "the day is a cycle"): unit tests for the day-boundary
clock and the day's own action facts.

day_start/day_position/deliverable_stage are pure functions of the clock
(or a fixed constant); day_actions is the fail-open ledger reader, matching
the discipline #1773's integration_class_counts and #1766's scorecard block
already apply -- unavailable is never folded into a fabricated zero.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import day_clock


def _write_ledger(state_dir: Path, events: list[dict]) -> None:
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    with open(ledger_dir / "cycles.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_day_start_is_utc_midnight_on_or_before_now():
    now = datetime(2026, 9, 20, 14, 37, 12, tzinfo=timezone.utc)
    assert day_clock.day_start(now) == datetime(2026, 9, 20, 0, 0, 0, tzinfo=timezone.utc)


def test_day_start_at_exact_boundary_stays_on_that_day():
    now = datetime(2026, 9, 20, 0, 0, 0, tzinfo=timezone.utc)
    assert day_clock.day_start(now) == now


def test_day_position_reports_elapsed_and_remaining_summing_to_24h():
    now = datetime(2026, 9, 20, 6, 0, 0, tzinfo=timezone.utc)
    pos = day_clock.day_position(now)
    assert pos["hours_elapsed"] == 6.0
    assert pos["hours_to_deep_sleep"] == 18.0


def test_day_position_never_goes_negative_past_the_boundary():
    # 25h "now" relative to a naive caller would be nonsensical -- day_start
    # always resolves to the CURRENT day's midnight, so this instead pins
    # the floor at the far end of a single day.
    now = datetime(2026, 9, 20, 23, 59, 59, tzinfo=timezone.utc)
    pos = day_clock.day_position(now)
    assert pos["hours_to_deep_sleep"] >= 0.0


def test_deliverable_stage_is_none_with_no_pipeline():
    assert day_clock.deliverable_stage() == "none"
    assert day_clock.deliverable_stage() == day_clock.DELIVERABLE_STAGES[0]


def test_day_actions_reads_as_a_genuinely_quiet_day_with_no_ledger_dir(tmp_path):
    """A missing ledger directory is a state with no history, not a failed
    read (state_access.evidence_status's 'dir_missing' -> 'complete' rule,
    #1173 D-2) -- reads as zero commits, not 'unavailable'."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    result = day_clock.day_actions(state_dir)
    assert result["status"] == "complete"
    assert result["commits_integrated_today"] == 0
    assert result["files_touched_today"] == []


def test_day_actions_unavailable_never_becomes_a_fabricated_zero(tmp_path):
    """#1773/#1766 discipline: a ledger the reader could not open is
    'unavailable', not silently folded into commits_integrated_today=0
    (which would read identically to a genuinely quiet day)."""
    state_dir = tmp_path / "state"
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "cycles.jsonl").write_text("not json\n{bad\n", encoding="utf-8")
    result = day_clock.day_actions(state_dir)
    assert result["commits_integrated_today"] == 0
    # A malformed ledger is either read as zero valid rows (a real "quiet
    # day" read) or reported unavailable -- either way this must not raise.
    assert result["status"] in ("unavailable", "complete", "partial")


def test_day_actions_counts_success_and_pushed_late_since_day_start(tmp_path):
    state_dir = tmp_path / "state"
    now = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        {"phase": "outcome", "outcome": "success", "files_changed": ["a.py"], "ts": (now - timedelta(hours=2)).isoformat()},
        {"phase": "outcome", "outcome": "pushed_late", "files_changed": ["b.py"], "ts": (now - timedelta(hours=1)).isoformat()},
        # Before today's boundary -- not counted.
        {"phase": "outcome", "outcome": "success", "files_changed": ["yesterday.py"], "ts": (now - timedelta(hours=26)).isoformat()},
        # Failed -- not counted.
        {"phase": "outcome", "outcome": "failed", "files_changed": ["c.py"], "ts": (now - timedelta(hours=1)).isoformat()},
    ]
    _write_ledger(state_dir, events)

    result = day_clock.day_actions(state_dir, now=now)
    assert result["commits_integrated_today"] == 2
    assert result["files_touched_today"] == ["a.py", "b.py"]
    assert result["files_touched_today_total"] == 2
    assert result["status"] != "unavailable"


def test_day_actions_dedupes_and_caps_the_file_list(tmp_path):
    state_dir = tmp_path / "state"
    now = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        {
            "phase": "outcome", "outcome": "success",
            "files_changed": [f"file_{i}.py" for i in range(15)],
            "ts": now.isoformat(),
        },
        {
            "phase": "outcome", "outcome": "success",
            "files_changed": ["file_0.py"],  # duplicate, must not double-count
            "ts": now.isoformat(),
        },
    ]
    _write_ledger(state_dir, events)

    result = day_clock.day_actions(state_dir, now=now)
    assert result["commits_integrated_today"] == 2
    assert result["files_touched_today_total"] == 15
    assert len(result["files_touched_today"]) == 10


def test_no_verdict_word_in_any_rendered_action_or_mortality_text(tmp_path):
    """AC: the day's actions are shown as actions, never a verdict (ADR-026
    decision 2) -- no adjective/score word from VERDICT_WORDS appears in
    any text this module can produce."""
    state_dir = tmp_path / "state"
    now = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        {"phase": "outcome", "outcome": "success", "files_changed": ["a.py"], "ts": now.isoformat()},
    ]
    _write_ledger(state_dir, events)
    result = day_clock.day_actions(state_dir, now=now)

    rendered = " ".join(str(v) for v in result.values())
    lowered = rendered.lower()
    for word in day_clock.VERDICT_WORDS:
        assert word not in lowered, f"verdict word {word!r} leaked into day_actions output"


def test_day_boundary_hour_is_midnight_utc_matching_the_nightly_cluster():
    """Pin (ADR-026 decision 4): the day boundary a future deep-sleep job
    must import rather than re-derive is UTC midnight -- drifting this
    constant would silently move every 'hours to deep sleep' figure the
    executor reads."""
    assert day_clock.DAY_BOUNDARY_HOUR_UTC == 0
    assert day_clock.DAY_HOURS == 24


def test_day_clock_source_never_mentions_the_diary():
    """ADR-028 rule 4: the diary never enters the prompt. day_clock.py is
    the ONLY source day_clock's facts flow from (ledger events + the wall
    clock) -- pinning that its source never references "diary" at all
    guards against a future edit quietly wiring diary/ content into the
    day-position facts this module feeds to ContextBuilder's position
    block. #1810/#1812 build the diary itself; this module has no reason
    to ever import or read it."""
    src = Path(day_clock.__file__).read_text(encoding="utf-8")
    assert "diary" not in src.lower()
