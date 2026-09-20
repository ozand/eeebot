"""#1820 -- the narrator is built and nothing runs it.

Measured on host ``eeepc`` 2026-09-20, before this change:

    systemctl list-timers       11 eeebot-* timers, none for the narrator
    state/story/                one file, 2026-09-15.json, written 09-17 by hand
    state/llm_calls/            component "narrator" appears once, 2026-09-17

So ADR-026's deliverable had no producer on any schedule, and the charter's
top rung -- "something reached a person outside this machine" -- was
unreachable by construction rather than by difficulty.

Three things are tested here, and they are the three ways this could ship
and still not work:

1. The unit exists in the repository AND is shaped like the units the deploy
   already installs, because the deploy copies ``host/eeepc/systemd/*`` and
   enables new timers -- a malformed unit is a silent no-op.
2. Every path writes a run journal row, so "the job ran and the day was
   quiet" is distinguishable from "the job never ran". An artifact alone
   cannot answer that: a quiet day's artifact and a never-narrated day's
   absence are the same shape as a write failure.
3. The stage is recorded rather than omitted, and ``observed`` is never
   inferred from anything (ADR-026).
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.journal_story import (
    PRODUCER,
    RUNS_SUBPATH,
    STAGE_NONE,
    STAGE_UNKNOWN,
    default_day,
    main,
    run_narrator_job,
    stage_record,
)

REPO = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO / "host" / "eeepc" / "systemd"
SERVICE = UNIT_DIR / "eeebot-narrator.service"
TIMER = UNIT_DIR / "eeebot-narrator.timer"


def _ledger(state_dir: Path, day: str, cycles: int) -> None:
    ledger = state_dir / "ledger"
    ledger.mkdir(parents=True, exist_ok=True)
    rows = [
        json.dumps(
            {
                "phase": "outcome",
                "cycle_id": f"cycle-{index}",
                "outcome": "success",
                "task_title": f"task {index}",
                "ts": f"{day}T0{index}:00:00Z",
            }
        )
        for index in range(1, cycles + 1)
    ]
    (ledger / "cycles.jsonl").write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")


def _runs(state_dir: Path) -> list[dict]:
    path = state_dir.joinpath(*RUNS_SUBPATH)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. the unit
# ---------------------------------------------------------------------------

def test_the_unit_pair_exists():
    """The whole issue: the job existed, the schedule did not."""
    assert SERVICE.is_file()
    assert TIMER.is_file()


def test_the_service_matches_the_conventions_of_the_units_beside_it():
    """The deploy copies every file in this directory and enables new timers,
    so a unit that does not follow the house shape installs and misbehaves
    rather than failing loudly."""
    text = SERVICE.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    assert "User=eeepc-agent" in text
    assert "Group=eeepc-agent" in text
    # The narrator calls a model; without litellm.env it dies in the gateway
    # and reports it as a model failure (#986's first-run incident).
    assert "EnvironmentFile=/etc/eeepc-agent/litellm.env" in text
    # ProtectSystem=strict makes the whole filesystem read-only; state is the
    # one place this job writes.
    assert "ProtectSystem=strict" in text
    assert "ReadWritePaths=/var/lib/eeepc-agent/self-evolving-agent/state" in text
    assert "scripts/journal_story.py" in text
    assert "--state-root /var/lib/eeepc-agent/self-evolving-agent/state" in text


def test_the_timer_runs_daily_and_survives_a_powered_off_host():
    text = TIMER.read_text(encoding="utf-8")
    assert "Unit=eeebot-narrator.service" in text
    assert "WantedBy=timers.target" in text
    # Persistent: the host is not up 24/7, and a missed day must still be
    # narrated rather than skipped in silence.
    assert "Persistent=true" in text
    assert "OnCalendar=" in text


def test_the_unit_passes_no_day_so_it_can_never_narrate_an_open_day():
    """A hardcoded ``--day`` in the unit would freeze on one date; a relative
    one computed in shell would be timezone-dependent. The default in the job
    is yesterday UTC, which is closed at every local hour."""
    exec_lines = [
        line for line in SERVICE.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    ]
    assert exec_lines
    assert all("--day" not in line for line in exec_lines), exec_lines


def test_yesterday_utc_is_the_default():
    from datetime import datetime, timezone

    assert default_day(datetime(2026, 9, 20, 3, 30, tzinfo=timezone.utc)) == "2026-09-19"
    # 00:01 UTC -- the earliest the boundary could bite -- still yields a
    # closed day.
    assert default_day(datetime(2026, 9, 20, 0, 1, tzinfo=timezone.utc)) == "2026-09-19"


# ---------------------------------------------------------------------------
# 2. a quiet day and a day nobody narrated are different states
# ---------------------------------------------------------------------------

def test_a_day_with_no_beats_leaves_a_run_row(tmp_path: Path):
    """AC 4. The artifact for a quiet day already existed; what was missing
    was any record that the attempt happened at all."""
    _ledger(tmp_path, "2026-09-19", 0)
    result = run_narrator_job(tmp_path, "2026-09-19")

    assert result["status"] == "ok"
    assert result["beats"] == []
    rows = _runs(tmp_path)
    assert len(rows) == 1
    assert rows[0]["day"] == "2026-09-19"
    assert rows[0]["status"] == "ok"
    assert rows[0]["beats"] == 0
    assert rows[0]["producer"] == PRODUCER
    # No model was called for a day with nothing to narrate.
    assert rows[0]["model"] is None


def test_a_day_nobody_narrated_leaves_nothing_at_all(tmp_path: Path):
    """The other half of the same distinction -- without this the assertion
    above proves only that a file exists, not that it means anything."""
    _ledger(tmp_path, "2026-09-19", 3)
    assert _runs(tmp_path) == []
    assert not (tmp_path / "story" / "2026-09-19.json").exists()


def test_a_gateway_failure_is_journalled_not_silent(tmp_path: Path):
    """AC 6. A run that reached the model and failed must not read as a quiet
    day: same absence of narration, entirely different fact."""
    _ledger(tmp_path, "2026-09-19", 3)

    def _dead(messages, model):
        raise RuntimeError("gateway refused the connection")

    result = run_narrator_job(tmp_path, "2026-09-19", llm=_dead)

    assert result["status"] == "rejected"
    rows = _runs(tmp_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"
    assert rows[0]["beats"] == 3
    assert any("gateway refused" in violation for violation in rows[0]["violations"])


def test_a_rejected_narration_is_journalled_too(tmp_path: Path):
    """A model that answered but could not be traced to a beat is a third
    outcome again, and the row says which of the three it was."""
    _ledger(tmp_path, "2026-09-19", 3)

    def _untraceable(messages, model):
        return [{"text": "the loop finally fixed everything", "cites": []}]

    result = run_narrator_job(tmp_path, "2026-09-19", llm=_untraceable)

    assert result["status"] == "rejected"
    rows = _runs(tmp_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"
    assert rows[0]["violations"]


def test_every_run_appends_rather_than_replacing(tmp_path: Path):
    """Two days, two rows: the journal answers "which days were narrated",
    which one file per day cannot once a day is overwritten."""
    _ledger(tmp_path, "2026-09-19", 0)
    run_narrator_job(tmp_path, "2026-09-19")
    run_narrator_job(tmp_path, "2026-09-18")
    assert [row["day"] for row in _runs(tmp_path)] == ["2026-09-19", "2026-09-18"]


# ---------------------------------------------------------------------------
# 3. the stage is recorded, and never inferred
# ---------------------------------------------------------------------------

def test_the_stage_is_recorded_on_the_artifact(tmp_path: Path):
    """AC 5. "Stage reached: none" is a measurement, and the one ADR-026
    predicts for the earliest days. Omitting it would make the gap
    uncountable."""
    _ledger(tmp_path, "2026-09-19", 0)
    result = run_narrator_job(tmp_path, "2026-09-19")

    assert result["stage"]["reached"] == STAGE_NONE
    written = json.loads((tmp_path / "story" / "2026-09-19.json").read_text(encoding="utf-8"))
    assert written["stage"]["reached"] == STAGE_NONE


def test_observation_is_unknown_and_never_false_or_zero():
    """ADR-026: an observation is never inferred, and ``unknown`` is never
    rendered as zero or as success. ``False`` here would be a claim nobody
    measured -- the same error as reporting a missing reading as 0."""
    stage = stage_record()
    assert stage["published"] == STAGE_UNKNOWN
    assert stage["observed"] == STAGE_UNKNOWN
    assert stage["observed"] is not False
    assert stage["observed"] != 0
    # ``rendered`` is the one stage this module can speak to, and the honest
    # answer is no: it writes narration text, not a sequence.
    assert stage["rendered"] is False


def test_the_run_row_carries_the_stage(tmp_path: Path):
    """So "how many days reached no stage" is answerable by reading one file
    instead of opening every artifact."""
    _ledger(tmp_path, "2026-09-19", 0)
    run_narrator_job(tmp_path, "2026-09-19")
    assert _runs(tmp_path)[0]["stage_reached"] == STAGE_NONE


# ---------------------------------------------------------------------------
# ADR-016 rule 1: the barrier this issue must not cross
# ---------------------------------------------------------------------------

def test_the_job_has_no_reader_for_any_channel_figure():
    """The call-graph constraint, asserted on the source rather than trusted:
    a narrator that could see its own view count has an incentive this design
    refuses to give it. The unit must not hand it one either."""
    source = (REPO / "scripts" / "journal_story.py").read_text(encoding="utf-8").lower()
    unit = SERVICE.read_text(encoding="utf-8").lower()
    for needle in ("view_count", "views", "subscriber", "watch_time", "youtube", "analytics"):
        assert needle not in unit, needle
    for needle in ("view_count", "subscriber", "watch_time", "youtube", "analytics"):
        assert needle not in source, needle


# ---------------------------------------------------------------------------
# the entry point the unit actually calls
# ---------------------------------------------------------------------------

def test_main_runs_the_job_and_reports_the_day(tmp_path: Path, capsys):
    _ledger(tmp_path, "2026-09-19", 0)
    code = main(["--state-root", str(tmp_path), "--day", "2026-09-19"])
    assert code == 0
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["day"] == "2026-09-19"
    assert printed["stage_reached"] == STAGE_NONE
    assert (tmp_path / "story" / "2026-09-19.json").is_file()


def test_main_exits_non_zero_when_the_narration_was_rejected(tmp_path: Path, monkeypatch, capsys):
    """systemd must record a failed run. A quiet day exiting 0 and a dead
    gateway exiting 0 would make ``systemctl status`` useless as evidence."""
    _ledger(tmp_path, "2026-09-19", 3)
    monkeypatch.setenv("LITELLM_BASE_URL", "")
    monkeypatch.setenv("LITELLM_API_KEY", "")
    code = main(["--state-root", str(tmp_path), "--day", "2026-09-19"])
    capsys.readouterr()
    assert code == 1
    assert _runs(tmp_path)[0]["status"] == "rejected"
