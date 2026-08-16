"""Tests for #844: BRIDGE-lane stepping-stone diversity archive.

A small (<=5) archive of gate-passing BRIDGE candidate variants, keyed on a
behavior signature, surfaced by the LLM proposer as optional stepping-stones.
Proposer-steering ONLY — never touches the gate, fitness, confirm, or
FITNESS_SIDECARS.
"""
from __future__ import annotations

import json

from nanobot.runtime import llm_proposer
from nanobot.runtime.archive import (
    _STEPPING_STONES_MAX,
    _stepping_stone_signature,
    read_stepping_stones,
    record_stepping_stone,
)


# ─── _stepping_stone_signature ──────────────────────────────────────────────

def test_signature_prefers_scripts_py_path():
    files = ["docs/notes.md", "scripts/foo_bar.py", "tests/test_foo.py"]
    assert _stepping_stone_signature(files) == "scripts/foo_bar.py"


def test_signature_falls_back_to_first_sorted_path_when_no_scripts_py():
    files = ["docs/z.md", "docs/a.md"]
    assert _stepping_stone_signature(files) == "docs/a.md"


def test_signature_picks_lowest_scripts_py_path_when_multiple():
    files = ["scripts/zeta.py", "scripts/alpha.py"]
    assert _stepping_stone_signature(files) == "scripts/alpha.py"


def test_signature_misc_on_empty():
    assert _stepping_stone_signature([]) == "misc"
    assert _stepping_stone_signature(None) == "misc"


def test_signature_misc_on_garbage():
    assert _stepping_stone_signature(object()) == "misc"
    assert _stepping_stone_signature(42) == "misc"  # not iterable → caught, fail-open
    assert _stepping_stone_signature(["", "   "]) == "misc"  # blank paths filtered out


# ─── record_stepping_stone / read_stepping_stones round-trip ────────────────

def test_round_trip_writes_expected_file(tmp_path):
    state_dir = tmp_path / "state"
    record_stepping_stone(
        state_dir, "cycle-1", ["scripts/foo.py"], "did a thing", now=1000.0,
    )
    path = state_dir / "steering" / "stepping_stones.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["signature"] == "scripts/foo.py"
    assert data[0]["summary"] == "did a thing"
    assert data[0]["cycle_id"] == "cycle-1"

    stones = read_stepping_stones(state_dir)
    assert len(stones) == 1
    assert stones[0]["signature"] == "scripts/foo.py"


def test_newest_per_signature_wins(tmp_path):
    state_dir = tmp_path / "state"
    record_stepping_stone(
        state_dir, "cycle-1", ["scripts/foo.py"], "first version", now=1000.0,
    )
    record_stepping_stone(
        state_dir, "cycle-2", ["scripts/foo.py"], "second version", now=2000.0,
    )
    stones = read_stepping_stones(state_dir)
    assert len(stones) == 1
    assert stones[0]["cycle_id"] == "cycle-2"
    assert stones[0]["summary"] == "second version"


def test_cap_at_max_distinct_signatures_evicts_oldest(tmp_path):
    state_dir = tmp_path / "state"
    for i in range(_STEPPING_STONES_MAX + 1):
        record_stepping_stone(
            state_dir, f"cycle-{i}", [f"scripts/area_{i}.py"], f"summary {i}",
            now=1000.0 + i,
        )
    stones = read_stepping_stones(state_dir)
    assert len(stones) == _STEPPING_STONES_MAX
    signatures = {s["signature"] for s in stones}
    # oldest (area_0, ts=1000.0) evicted; newest _STEPPING_STONES_MAX remain
    assert "scripts/area_0.py" not in signatures
    assert f"scripts/area_{_STEPPING_STONES_MAX}.py" in signatures


def test_idempotent_on_same_cycle_id(tmp_path):
    state_dir = tmp_path / "state"
    record_stepping_stone(
        state_dir, "cycle-1", ["scripts/foo.py"], "v1", now=1000.0,
    )
    record_stepping_stone(
        state_dir, "cycle-1", ["scripts/bar.py"], "v1 again", now=1001.0,
    )
    stones = read_stepping_stones(state_dir)
    # same cycle_id replaces rather than accumulates
    assert len(stones) == 1
    assert stones[0]["signature"] == "scripts/bar.py"


def test_fail_open_on_none_files_changed(tmp_path):
    state_dir = tmp_path / "state"
    # Should not raise even with None files_changed / summary
    record_stepping_stone(state_dir, "cycle-1", None, None, now=1000.0)
    stones = read_stepping_stones(state_dir)
    assert len(stones) == 1
    assert stones[0]["signature"] == "misc"


def test_read_fail_open_on_missing_state_dir(tmp_path):
    missing = tmp_path / "does_not_exist_at_all"
    assert read_stepping_stones(missing) == []


def test_read_fail_open_on_corrupt_json(tmp_path):
    state_dir = tmp_path / "state"
    path = state_dir / "steering" / "stepping_stones.json"
    path.parent.mkdir(parents=True)
    path.write_text("not valid json {{{", encoding="utf-8")
    assert read_stepping_stones(state_dir) == []


def test_record_fail_open_never_raises(tmp_path):
    # A state_dir that is actually a file (not a directory) should not raise;
    # record_stepping_stone swallows the resulting error.
    bad_state_dir = tmp_path / "not_a_dir"
    bad_state_dir.write_text("i am a file", encoding="utf-8")
    record_stepping_stone(bad_state_dir, "cycle-1", ["scripts/foo.py"], "x")
    # No exception raised is the assertion; read should fail-open too.
    assert read_stepping_stones(bad_state_dir) == []


# ─── llm_proposer._stepping_stones_section ──────────────────────────────────

def test_stepping_stones_section_empty_archive(tmp_path):
    state_dir = tmp_path / "state"
    assert llm_proposer._stepping_stones_section(state_dir) == ""


def test_stepping_stones_section_populated(tmp_path):
    state_dir = tmp_path / "state"
    record_stepping_stone(
        state_dir, "cycle-42", ["scripts/widget.py"], "built a widget",
        now=1000.0,
    )
    section = llm_proposer._stepping_stones_section(state_dir)
    assert "scripts/widget.py" in section
    assert "built a widget" in section
    assert "cycle-42" in section


# ─── sidecar path is NOT a fitness sidecar ───────────────────────────────────

def test_stepping_stones_sidecar_not_in_fitness_sidecars():
    from nanobot.runtime import scorecard

    assert "steering/stepping_stones.json" not in scorecard.FITNESS_SIDECARS
