"""ADR-036 D1.1 follow-up (ozand/eeebot-ops-dashboard#378): the writer side
of state/public/derived_view.json, which the public dashboard projects.

1. Every derived_priorities row carries provenance "self-derived" (the
   dashboard publishes a label/direction only under that EXPLICIT tag).
2. The charter tag: source "release_goals_md" is set ONLY after the release
   goals.md was read; "merged" is always False; the charter text never
   comes from goal_text.json.

All inputs are synthetic fixture text under tmp_path; RELEASE_ROOT always
points at a tmp directory, so no real goal file is ever read.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.runtime import demand

GOAL_TEXT_CANARY = "GOALTEXT-CANARY-5e1d operator private wording"
RELEASE_CHARTER = "# RELEASE CHARTER\nWe build a self-improving system (synthetic fixture).\n"
INSTANCE_COPY = "# INSTANCE-REPO GOALS COPY\nmust never be consulted (synthetic fixture).\n"


def _state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    (state_dir / "goals").mkdir(parents=True)
    return state_dir


def _goal_text(state_dir: Path) -> None:
    (state_dir / "goals" / "goal_text.json").write_text(json.dumps({"text": (
        "Current priority targets:\n"
        f"(A) Priority 11 — {GOAL_TEXT_CANARY} (V2): do the thing\n"
    )}), encoding="utf-8")


def _derived(state_dir: Path, entries: list[dict]) -> None:
    (state_dir / "goals" / "derived_priorities.json").write_text(
        json.dumps({"schema_version": "derived-priorities-v1", "priorities": entries}), encoding="utf-8")


def _release_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, charter: str | None) -> Path:
    root = tmp_path / "release"
    root.mkdir()
    if charter is not None:
        (root / "goals.md").write_text(charter, encoding="utf-8")
    monkeypatch.setenv("RELEASE_ROOT", str(root))
    return root


_ENTRIES = [
    {"label": "Night reflections batch (V1)", "vector": "V1", "body": "b", "number": 19,
     "direction": "reflection", "added_utc": "2026-09-15T21:36:00Z"},
    {"label": "Cheaper dedup (V2)", "vector": "V2", "body": "b", "number": 20,
     "added_utc": "2026-09-16T08:00:00Z"},
]


# --- 1. provenance on every derived_priorities row -------------------------------

def test_every_derived_priority_row_carries_self_derived_provenance(tmp_path, monkeypatch) -> None:
    state_dir = _state(tmp_path)
    _release_root(tmp_path, monkeypatch, RELEASE_CHARTER)
    _derived(state_dir, _ENTRIES)

    view = json.loads(Path(demand.publish_derived_view(state_dir, None)["path"]).read_text(encoding="utf-8"))

    rows = view["derived_priorities"]
    assert [row["number"] for row in rows] == [19, 20]
    assert all(row["provenance"] == demand.PROVENANCE_SELF_DERIVED == "self-derived" for row in rows), rows


# --- 2. the charter tag ------------------------------------------------------------

def _charter(state_dir: Path, selfevo_repo: Path | None = None) -> dict:
    view = demand.build_derived_view(state_dir, selfevo_repo)
    assert GOAL_TEXT_CANARY not in json.dumps(view["charter"]), "charter text came from goal_text.json"
    assert view["charter"]["merged"] is False
    return view["charter"]


def test_a_release_goals_md_read_is_the_only_release_goals_md_tag(tmp_path, monkeypatch) -> None:
    state_dir = _state(tmp_path)
    _goal_text(state_dir)  # present, and still never the charter
    _release_root(tmp_path, monkeypatch, RELEASE_CHARTER)

    charter = _charter(state_dir)

    assert charter == {"source": "release_goals_md", "merged": False, "text": RELEASE_CHARTER.strip()}


def test_b_goals_md_absent_with_goal_text_present_is_never_tagged(tmp_path, monkeypatch) -> None:
    state_dir = _state(tmp_path)
    _goal_text(state_dir)
    _release_root(tmp_path, monkeypatch, None)  # the release tree has no goals.md

    charter = _charter(state_dir)

    assert charter["source"] != "release_goals_md"
    assert charter == {"source": "none", "merged": False, "text": ""}


def test_c_merged_derived_inputs_without_release_goals_md_are_never_tagged(tmp_path, monkeypatch) -> None:
    """goal_text + derived priorities + an instance-repo goals.md copy, but
    no release goals.md: nothing may be merged or relabelled into the charter."""
    state_dir = _state(tmp_path)
    _goal_text(state_dir)
    _derived(state_dir, _ENTRIES)
    selfevo = tmp_path / "instance-repo"
    selfevo.mkdir()
    (selfevo / "goals.md").write_text(INSTANCE_COPY, encoding="utf-8")
    _release_root(tmp_path, monkeypatch, None)

    charter = _charter(state_dir, selfevo)

    assert charter == {"source": "none", "merged": False, "text": ""}
    assert "INSTANCE-REPO" not in json.dumps(charter)
