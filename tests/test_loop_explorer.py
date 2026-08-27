"""Tests for #781: the static loop-explorer visualization.

Covers: rotation-aware event building (current cycles.jsonl + one .gz
archive) with correct ordering/grouping, demand-chain grouping, the
confirmed join against demand/completed.json, the last-N event bound,
empty/missing-state degradation, self-contained HTML rendering (color
classes, detail data, NO external resources), ANSI rendering (colored /
NO_COLOR plain ASCII / empty-model message), the watermark-gated
update_explorer (no-op, regeneration on append, fail-open), and the CLI
self-test.
"""
from __future__ import annotations

import gzip
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import loop_explorer

NOW = datetime.now(timezone.utc)
REPO_ROOT = Path(__file__).resolve().parent.parent


def _iso(minutes_ago: int = 0) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def _write_ledger(state_dir: Path, rows: list[dict], filename: str = "cycles.jsonl") -> None:
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / filename).write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def _write_gz_ledger(state_dir: Path, rows: list[dict], day: str) -> None:
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(ledger_dir / f"cycles-{day}.jsonl.gz", "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _write_completed(state_dir: Path, entries: dict) -> None:
    demand_dir = state_dir / "demand"
    demand_dir.mkdir(parents=True, exist_ok=True)
    (demand_dir / "completed.json").write_text(
        json.dumps({"schema_version": "demand-completed-v1", "entries": entries}),
        encoding="utf-8",
    )


def _fixture_state(state_dir: Path) -> None:
    """A representative state dir: one archived cycle, current-file events
    of every class, completed sidecar with one confirmed entry, and three
    scorecard history snapshots."""
    # Rotated archive: an older successful cycle (unconfirmed).
    _write_gz_ledger(
        state_dir,
        [
            {"phase": "proposed", "cycle_id": "c0", "task_title": "archived win",
             "demand_id": "priority-old", "ts": _iso(600)},
            {"phase": "dedup", "cycle_id": "c0", "decision": "proceeded",
             "matched_against": None, "ts": _iso(599)},
            {"phase": "outcome", "cycle_id": "c0", "outcome": "success", "reason": None,
             "files_changed": ["scripts/old.py"], "ts": _iso(598)},
        ],
        day="2026-07-10",
    )
    _write_ledger(
        state_dir,
        [
            {"phase": "idle", "reason": "no_demand", "ts": _iso(500)},
            {"phase": "proposer_skip", "reason": "nothing valuable", "ts": _iso(400)},
            {"phase": "proposer_reject", "reason": "self_dedup", "task_title": "dup task",
             "demand_id": "priority-aaa", "matched_against": "done: dup task", "ts": _iso(300)},
            # Confirmed success cycle for priority-aaa.
            {"phase": "proposed", "cycle_id": "c1", "task_title": "fix the widget",
             "demand_id": "priority-aaa", "ts": _iso(200)},
            {"phase": "dedup", "cycle_id": "c1", "decision": "proceeded",
             "matched_against": None, "ts": _iso(199)},
            {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "reason": None,
             "files_changed": ["scripts/widget.py"], "ts": _iso(198)},
            # A dedup-skipped cycle.
            {"phase": "proposed", "cycle_id": "c2", "task_title": "again the widget",
             "demand_id": "priority-aaa", "ts": _iso(100)},
            {"phase": "dedup", "cycle_id": "c2", "decision": "skipped_duplicate",
             "matched_against": "done:fix the widget", "ts": _iso(99)},
            {"phase": "outcome", "cycle_id": "c2", "outcome": "skipped-duplicate",
             "reason": None, "files_changed": [], "ts": _iso(99)},
            # A failed cycle, no demand_id.
            {"phase": "proposed", "cycle_id": "c3", "task_title": "risky change", "ts": _iso(50)},
            {"phase": "outcome", "cycle_id": "c3", "outcome": "failed",
             "reason": "no_commit", "files_changed": [], "ts": _iso(49)},
        ],
    )
    _write_completed(
        state_dir,
        {
            "priority-aaa": {"cycle_id": "c1", "ts": _iso(198),
                             "files_changed": ["scripts/widget.py"], "confirmed": True},
        },
    )
    scorecard_dir = state_dir / "scorecard"
    scorecard_dir.mkdir(parents=True, exist_ok=True)
    with open(scorecard_dir / "history.jsonl", "w", encoding="utf-8") as fh:
        for minutes, integrations, rate in ((300, 1, 0.5), (150, 2, 0.3), (10, 3, 0.1)):
            fh.write(
                json.dumps(
                    {
                        "computed_at_utc": _iso(minutes),
                        "loop": {"integrations": integrations, "repeat_failure_rate": rate},
                        "cost": {"tokens_per_integration": 1000 * integrations},
                        "heldout": {"heldout_gap": 0.0},
                    }
                )
                + "\n"
            )


# ─── build_model ────────────────────────────────────────────────────────────


def test_build_model_orders_and_groups_across_rotation(tmp_path: Path) -> None:
    _fixture_state(tmp_path)
    model = loop_explorer.build_model(tmp_path)

    events = model["events"]
    # 1 archived cycle + idle + skip + reject + 3 current cycles = 7 events.
    assert len(events) == 7
    assert model["window"]["n_events"] == 7
    # Chronological order across the .gz/current rotation boundary.
    assert [e["type"] for e in events] == [
        "cycle", "idle", "proposer_skip", "proposer_reject", "cycle", "cycle", "cycle",
    ]
    assert events[0]["title"] == "archived win"
    assert [e["class"] for e in events] == [
        "success", "idle", "noop", "reject", "confirmed", "skip", "failed",
    ]

    # Cycle event detail fields.
    c1 = next(e for e in events if e.get("cycle_id") == "c1")
    assert c1["demand_id"] == "priority-aaa"
    assert c1["dedup_decision"] == "proceeded"
    assert c1["files_changed"] == ["scripts/widget.py"]
    assert c1["confirmed"] is True
    c2 = next(e for e in events if e.get("cycle_id") == "c2")
    assert c2["matched_against"] == "done:fix the widget"
    assert c2["outcome"] == "skipped-duplicate"
    c3 = next(e for e in events if e.get("cycle_id") == "c3")
    assert c3["reason"] == "no_commit"
    assert c3["confirmed"] is False


def test_build_model_demand_chains_and_confirmed_join(tmp_path: Path) -> None:
    _fixture_state(tmp_path)
    model = loop_explorer.build_model(tmp_path)
    chains = {c["demand_id"]: c for c in model["chains"]}
    assert set(chains) == {"priority-aaa", "priority-old"}
    aaa = chains["priority-aaa"]
    # reject + c1 + c2 all belong to the priority-aaa chain, time-ordered.
    assert [e["type"] for e in aaa["events"]] == ["proposer_reject", "cycle", "cycle"]
    assert aaa["completed"] is True
    assert aaa["confirmed"] is True
    old = chains["priority-old"]
    assert old["completed"] is False
    assert old["confirmed"] is False
    # Newest chain first.
    assert model["chains"][0]["demand_id"] == "priority-aaa"


def test_build_model_scorecard_series_bounded(tmp_path: Path) -> None:
    _fixture_state(tmp_path)
    model = loop_explorer.build_model(tmp_path)
    series = model["scorecard_series"]
    assert len(series) == 3
    assert [s["integrations"] for s in series] == [1, 2, 3]
    assert series[-1]["repeat_failure_rate"] == 0.1
    assert series[-1]["heldout_gap"] == 0.0
    assert series[-1]["tokens_per_integration"] == 3000

    # Bounded to the last 100 history entries.
    scorecard_dir = tmp_path / "scorecard"
    with open(scorecard_dir / "history.jsonl", "w", encoding="utf-8") as fh:
        for i in range(150):
            fh.write(
                json.dumps({"computed_at_utc": _iso(150 - i), "loop": {"integrations": i}}) + "\n"
            )
    model = loop_explorer.build_model(tmp_path)
    assert len(model["scorecard_series"]) == 100
    assert model["scorecard_series"][-1]["integrations"] == 149


def test_build_model_bounded_to_max_events(tmp_path: Path) -> None:
    rows = [
        {"phase": "idle", "reason": "no_demand", "ts": _iso(1000 - i)} for i in range(250)
    ]
    _write_ledger(tmp_path, rows)
    model = loop_explorer.build_model(tmp_path)
    assert len(model["events"]) == 200
    # The NEWEST 200 are kept.
    assert model["events"][-1]["ts"] == _iso(751)


def test_build_model_empty_and_missing_state(tmp_path: Path) -> None:
    model = loop_explorer.build_model(tmp_path / "missing")
    assert model["events"] == []
    assert model["chains"] == []
    assert model["scorecard_series"] == []
    assert model["window"]["n_events"] == 0

    # Corrupt everything: still no crash, empty-ish model.
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "cycles.jsonl").write_text("not json\n{broken", encoding="utf-8")
    (tmp_path / "demand").mkdir()
    (tmp_path / "demand" / "completed.json").write_text("garbage", encoding="utf-8")
    model = loop_explorer.build_model(tmp_path)
    assert model["events"] == []


# ─── render_html ────────────────────────────────────────────────────────────


def test_render_html_strip_classes_and_detail_data(tmp_path: Path) -> None:
    _fixture_state(tmp_path)
    model = loop_explorer.build_model(tmp_path)
    page = loop_explorer.render_html(model)

    # One strip block per event, with the correct color classes present.
    assert page.count('class="b ') == 7
    for cls in ("confirmed", "success", "skip", "failed", "reject", "noop", "idle"):
        assert f'class="b {cls}"' in page
    # Detail data (embedded model JSON) carries titles/ids/files/reasons.
    assert "fix the widget" in page
    assert "priority-aaa" in page
    assert "scripts/widget.py" in page
    assert "no_commit" in page
    assert "done:fix the widget" in page
    # Chains + confirmed badge + scorecard charts.
    assert "demand chains" in page
    assert ">confirmed</span>" in page
    assert "<svg" in page
    assert "eeebot loop explorer" in page


def test_render_html_is_fully_self_contained(tmp_path: Path) -> None:
    _fixture_state(tmp_path)
    page = loop_explorer.render_html(loop_explorer.build_model(tmp_path))
    # Strict: no external URL anywhere — not even in comments.
    assert not re.search(r"https?://", page)
    assert "http" not in page
    assert "<link" not in page
    assert not re.search(r"<script[^>]*\bsrc=", page)
    assert "@import" not in page
    assert 'src="' not in page


def test_render_html_empty_model(tmp_path: Path) -> None:
    page = loop_explorer.render_html(loop_explorer.build_model(tmp_path / "missing"))
    assert "no loop events recorded yet" in page
    assert "not enough scorecard history yet" in page
    assert "http" not in page


# ─── render_ansi ────────────────────────────────────────────────────────────


def test_render_ansi_colored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    _fixture_state(tmp_path)
    out = loop_explorer.render_ansi(loop_explorer.build_model(tmp_path))
    assert "\x1b[" in out
    assert "legend:" in out
    assert "fix the widget" in out
    assert "[confirmed]" in out
    assert "scorecard:" in out
    assert "integrations=3" in out


def test_render_ansi_no_color_plain_ascii(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    _fixture_state(tmp_path)
    out = loop_explorer.render_ansi(loop_explorer.build_model(tmp_path))
    assert "\x1b[" not in out
    assert out.isascii()
    # The strip line uses the per-class ASCII chars: S . n R C k X in order.
    assert "S.nRCkX" in out


def test_render_ansi_empty_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    out = loop_explorer.render_ansi(loop_explorer.build_model(tmp_path / "missing"))
    assert "no loop events recorded yet" in out


def test_build_model_reads_scorecard_archive_series(tmp_path: Path) -> None:
    _fixture_state(tmp_path)
    state_dir = tmp_path
    history_dir = state_dir / "scorecard"
    archive_dir = history_dir / "archive"
    archive_dir.mkdir(parents=True)
    import gzip

    with gzip.open(archive_dir / "history-20260801T000000Z.jsonl.gz", "wt", encoding="utf-8") as gz_fh:
        gz_fh.write(
            json.dumps(
                {
                    "schema_version": "scorecard-v1",
                    "computed_at_utc": _iso(minutes_ago=14400),
                    "loop": {"integrations": 1, "repeat_failure_rate": 0.0},
                    "cost": {"tokens_per_integration": 500.0},
                    "heldout": {"heldout_gap": 0.0},
                }
            )
            + "\n"
        )
    model = loop_explorer.build_model(state_dir)
    assert len(model["scorecard_series"]) >= 1
    assert model["scorecard_series"][0]["integrations"] == 1


# ─── update_explorer ────────────────────────────────────────────────────────


def test_update_explorer_writes_then_noops(tmp_path: Path) -> None:
    _fixture_state(tmp_path)
    out = loop_explorer.update_explorer(tmp_path)
    assert out == tmp_path / "explorer" / "index.html"
    assert out.is_file()
    assert "eeebot loop explorer" in out.read_text(encoding="utf-8")
    # Unchanged ledger + within 30 min → watermark no-op.
    assert loop_explorer.update_explorer(tmp_path) is None


def test_update_explorer_regenerates_on_ledger_append(tmp_path: Path) -> None:
    _fixture_state(tmp_path)
    assert loop_explorer.update_explorer(tmp_path) is not None
    with open(tmp_path / "ledger" / "cycles.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"phase": "idle", "reason": "no_demand", "ts": _iso(1)}) + "\n")
    out = loop_explorer.update_explorer(tmp_path)
    assert out is not None


def test_update_explorer_regenerates_after_30_minutes(tmp_path: Path) -> None:
    _fixture_state(tmp_path)
    assert loop_explorer.update_explorer(tmp_path) is not None
    later = datetime.now(timezone.utc) + timedelta(minutes=31)
    assert loop_explorer.update_explorer(tmp_path, now=later) is not None


def test_update_explorer_fail_open(tmp_path: Path) -> None:
    # A state "dir" that is actually a file: never raises, returns a path or
    # None (build_model degrades to an empty model on unreadable state).
    bogus = tmp_path / "not-a-dir"
    bogus.write_text("x", encoding="utf-8")
    result = loop_explorer.update_explorer(bogus)
    assert result is None


def test_update_explorer_empty_state_still_writes_page(tmp_path: Path) -> None:
    out = loop_explorer.update_explorer(tmp_path)
    assert out is not None and out.is_file()
    assert "no loop events recorded yet" in out.read_text(encoding="utf-8")


# ─── scorecard wiring ───────────────────────────────────────────────────────


def test_scorecard_recompute_refreshes_explorer(tmp_path: Path) -> None:
    from nanobot.runtime import scorecard

    _fixture_state(tmp_path)
    scorecard.compute_scorecard(tmp_path, None, force=True)
    assert (tmp_path / "explorer" / "index.html").is_file()


# ─── CLI ────────────────────────────────────────────────────────────────────


def test_cli_self_test_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "loop_explorer_cli.py"), "--test"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
