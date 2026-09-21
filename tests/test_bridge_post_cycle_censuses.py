"""#1768 Part 2 wiring: production actually calls the census writers.

``_write_post_cycle_censuses`` is the one call site ``_evaluate_candidate``
uses every cycle (extracted specifically so this is unit-testable — the
surrounding cycle machinery has no end-to-end test anywhere in this suite:
nothing calls ``bridge._main_impl_body``/``_evaluate_candidate`` directly,
so a full functional cycle run is not a reachable bar here). This proves the
call happens, not that either writer works internally (already covered by
``tests/test_lesson_citation_census_writer.py`` and
``tests/test_skill_hygiene_gate.py``'s zero-read-census tests).

The bridge imports both writers with a deferred, in-function ``from X import
Y`` (so a census-writer import failure can never break module load) --
monkeypatching the attribute on the SOURCE module (``skill_fitness``/
``lesson_v2``) works because that import statement re-resolves the name at
call time, every call.
"""
from __future__ import annotations

from pathlib import Path

from nanobot.runtime import bridge


def test_diary_marker_failure_leaves_a_cycle_ledger_error(tmp_path: Path, monkeypatch) -> None:
    import json
    import nanobot.runtime.diary_fitness as diary_fitness

    monkeypatch.setattr(diary_fitness, "record_cycle_diary_read", lambda *args, **kwargs: {})
    row = bridge._record_diary_fitness_marker(tmp_path / "state", "cycle-broken")

    assert row == {}
    ledger = tmp_path / "state" / "ledger" / "cycles.jsonl"
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert events[-1]["phase"] == "diary_fitness_error"
    assert events[-1]["cycle_id"] == "cycle-broken"
    assert "writer returned no row" in events[-1]["reason"]


def test_diary_marker_records_pre_spawn_cycle_without_reads(tmp_path: Path, monkeypatch) -> None:
    import json
    import nanobot.runtime.diary_fitness as diary_fitness

    monkeypatch.setattr(
        diary_fitness,
        "record_cycle_diary_read",
        lambda state_dir, **kwargs: {"cycle_id": kwargs["cycle_id"], "diary_read": False},
    )
    row = bridge._record_diary_fitness_marker(tmp_path / "state", "cycle-skipped")

    assert row == {"cycle_id": "cycle-skipped", "diary_read": False}
    assert not (tmp_path / "state" / "ledger" / "cycles.jsonl").exists()


def test_both_census_writers_are_called_with_the_right_arguments(tmp_path: Path, monkeypatch) -> None:
    import nanobot.runtime.diary_fitness as diary_fitness
    import nanobot.runtime.lesson_v2 as lesson_v2
    import nanobot.runtime.planning_fitness as planning_fitness
    import nanobot.runtime.skill_fitness as skill_fitness
    import nanobot.runtime.trajectory as trajectory

    skill_calls: list[tuple[Path, Path]] = []
    lesson_calls: list[Path] = []
    diary_calls: list[Path] = []
    trajectory_calls: list[tuple[Path, Path]] = []
    planning_calls: list[Path] = []
    skill_rate_calls: list[Path] = []

    def fake_skill_write(state_dir: Path, selfevo_repo: Path) -> dict:
        skill_calls.append((state_dir, selfevo_repo))
        return {"ok": True, "written": 0, "path": str(state_dir)}

    def fake_skill_rate_write(state_dir: Path, **kwargs: object) -> dict:
        skill_rate_calls.append(state_dir)
        return {"ok": True, "written": True, "path": str(state_dir)}

    def fake_lesson_write(state_dir: Path, **kwargs: object) -> dict:
        lesson_calls.append(state_dir)
        return {"ok": True, "written": 0, "path": str(state_dir)}

    def fake_diary_write(state_dir: Path, **kwargs: object) -> dict:
        diary_calls.append(state_dir)
        return {"ok": True, "written": True, "path": str(state_dir)}

    def fake_trajectory_write(state_dir: Path, selfevo_repo: Path, **kwargs: object) -> dict:
        trajectory_calls.append((state_dir, selfevo_repo))
        return {"ok": True, "written": True, "path": str(state_dir)}

    def fake_planning_write(state_dir: Path, **kwargs: object) -> dict:
        planning_calls.append(state_dir)
        return {"ok": True, "written": True, "path": str(state_dir)}

    monkeypatch.setattr(skill_fitness, "write_zero_read_census", fake_skill_write)
    monkeypatch.setattr(skill_fitness, "write_skill_read_rate", fake_skill_rate_write)
    monkeypatch.setattr(lesson_v2, "write_lesson_citation_census", fake_lesson_write)
    monkeypatch.setattr(diary_fitness, "write_diary_read_rate", fake_diary_write)
    monkeypatch.setattr(trajectory, "write_trajectory_report", fake_trajectory_write)
    monkeypatch.setattr(planning_fitness, "write_planning_overhead_rate", fake_planning_write)

    state_dir = tmp_path / "state"
    selfevo_repo = tmp_path / "repo"
    bridge._write_post_cycle_censuses(state_dir, selfevo_repo)

    assert skill_calls == [(state_dir, selfevo_repo)]
    assert lesson_calls == [state_dir]
    assert diary_calls == [state_dir]
    assert trajectory_calls == [(state_dir, selfevo_repo)]
    assert planning_calls == [state_dir]
    assert skill_rate_calls == [state_dir]


def test_a_broken_skill_writer_does_not_prevent_the_lesson_writer_from_running(
    tmp_path: Path, monkeypatch,
) -> None:
    """Each writer is independently fail-open: a broken skill census must
    not silently swallow the lesson census too."""
    import nanobot.runtime.lesson_v2 as lesson_v2
    import nanobot.runtime.skill_fitness as skill_fitness

    lesson_calls: list[Path] = []

    def broken_skill_write(state_dir: Path, selfevo_repo: Path) -> dict:
        raise OSError("skill census sidecar unreadable")

    def fake_lesson_write(state_dir: Path, **kwargs: object) -> dict:
        lesson_calls.append(state_dir)
        return {"ok": True, "written": 0, "path": str(state_dir)}

    monkeypatch.setattr(skill_fitness, "write_zero_read_census", broken_skill_write)
    monkeypatch.setattr(lesson_v2, "write_lesson_citation_census", fake_lesson_write)

    state_dir = tmp_path / "state"
    bridge._write_post_cycle_censuses(state_dir, tmp_path / "repo")  # must not raise

    assert lesson_calls == [state_dir]


def test_a_broken_lesson_writer_does_not_raise_into_the_cycle(tmp_path: Path, monkeypatch) -> None:
    import nanobot.runtime.lesson_v2 as lesson_v2

    def broken_lesson_write(state_dir: Path, **kwargs: object) -> dict:
        raise OSError("citation ledger unreadable")

    monkeypatch.setattr(lesson_v2, "write_lesson_citation_census", broken_lesson_write)
    bridge._write_post_cycle_censuses(tmp_path / "state", tmp_path / "repo")  # must not raise


def test_a_broken_diary_writer_does_not_raise_into_the_cycle(tmp_path: Path, monkeypatch) -> None:
    import nanobot.runtime.diary_fitness as diary_fitness

    def broken_diary_write(state_dir: Path, **kwargs: object) -> dict:
        raise OSError("diary cycle-scan ledger unreadable")

    monkeypatch.setattr(diary_fitness, "write_diary_read_rate", broken_diary_write)
    bridge._write_post_cycle_censuses(tmp_path / "state", tmp_path / "repo")  # must not raise


def test_a_broken_trajectory_writer_does_not_raise_into_the_cycle(tmp_path: Path, monkeypatch) -> None:
    import nanobot.runtime.trajectory as trajectory

    def broken_trajectory_write(state_dir: Path, selfevo_repo: Path, **kwargs: object) -> dict:
        raise OSError("ledger unreadable")

    monkeypatch.setattr(trajectory, "write_trajectory_report", broken_trajectory_write)
    bridge._write_post_cycle_censuses(tmp_path / "state", tmp_path / "repo")  # must not raise


def test_a_broken_planning_overhead_writer_does_not_raise_into_the_cycle(tmp_path: Path, monkeypatch) -> None:
    import nanobot.runtime.planning_fitness as planning_fitness

    def broken_planning_write(state_dir: Path, **kwargs: object) -> dict:
        raise OSError("planning cycle-scan ledger unreadable")

    monkeypatch.setattr(planning_fitness, "write_planning_overhead_rate", broken_planning_write)
    bridge._write_post_cycle_censuses(tmp_path / "state", tmp_path / "repo")  # must not raise


def test_a_broken_skill_read_rate_writer_does_not_raise_into_the_cycle(tmp_path: Path, monkeypatch) -> None:
    import nanobot.runtime.skill_fitness as skill_fitness

    def broken_skill_rate_write(state_dir: Path, **kwargs: object) -> dict:
        raise OSError("skill cycle-scan ledger unreadable")

    monkeypatch.setattr(skill_fitness, "write_skill_read_rate", broken_skill_rate_write)
    bridge._write_post_cycle_censuses(tmp_path / "state", tmp_path / "repo")  # must not raise
