"""Tests for #1346: lifecycle rows absent from the current inputs are marked, not silent.

``hypotheses/lifecycle.json`` is a sidecar keyed by ``hypothesis_id`` (else a
title slug) that outlives the inputs it was minted from (``backlog.json`` is
regenerated every cycle, ``durable.json`` by the strategist). A row whose key
is in no current input is never evaluated again and used to read exactly like
an active row. ``reconcile`` now marks such rows ``orphaned`` with the last
pass that did evaluate them, never deletes them, clears the mark when the key
reappears, and records pass metadata; ``lifecycle_counts`` exposes the
totals. The live files of 2026-09-05 (115 rows, 20 durable, 0 backlog) are
the fixture.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import hypothesis_backlog as hb

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "hypotheses_live_2026-09-05"
NOW = datetime(2026, 9, 6, 0, 0, tzinfo=timezone.utc)


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "hypotheses").mkdir(parents=True)
    return state


def _durable(state: Path, entries: list[dict]) -> None:
    (state / "hypotheses" / "durable.json").write_text(
        json.dumps({"schema": "hypothesis-durable-v1", "entries": entries}), encoding="utf-8"
    )


def _backlog(state: Path, entries: list[dict]) -> None:
    (state / "hypotheses" / "backlog.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")


def _lifecycle(state: Path) -> dict:
    return json.loads((state / "hypotheses" / "lifecycle.json").read_text(encoding="utf-8"))


def _entry(hid: str, title: str) -> dict:
    return {"hypothesis_id": hid, "title": title, "task_title": title}


# ─── orphan marking ──────────────────────────────────────────────────────────


def test_row_absent_from_inputs_is_marked_orphaned_with_last_evaluated(tmp_path):
    """Pass 1: key present. Pass 2: key gone. The row stays, marked, with the pass-1 timestamp."""
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0001", "Keep"), _entry("hyp-0002", "Vanishes")])
    _backlog(state, [])
    t1 = NOW
    hb.reconcile(state, now=t1)
    rows = _lifecycle(state)["entries"]
    assert rows["hyp-0002"]["last_evaluated"] == "2026-09-06T00:00:00Z"
    assert "orphaned" not in rows["hyp-0002"]

    _durable(state, [_entry("hyp-0001", "Keep")])
    t2 = NOW + timedelta(hours=1)
    hb.reconcile(state, now=t2)
    data = _lifecycle(state)
    rows = data["entries"]
    assert "hyp-0002" in rows, "orphaned rows are never deleted"
    assert rows["hyp-0002"]["orphaned"] is True
    assert rows["hyp-0002"]["orphaned_at"] == "2026-09-06T01:00:00Z"
    assert rows["hyp-0002"]["last_evaluated"] == "2026-09-06T00:00:00Z"  # the last pass that saw it
    assert rows["hyp-0001"]["last_evaluated"] == "2026-09-06T01:00:00Z"
    assert "orphaned" not in rows["hyp-0001"]
    assert data["last_pass"] == {
        "at": "2026-09-06T01:00:00Z",
        "inputs": {"durable.json": "ok", "backlog.json": "ok"},
        "inputs_ok": True,
        "evaluated": 1,
        "orphaned_now": 1,
        "slug_keyed_candidates": 0,
        "input_id_collisions": 0,
    }
    assert data["updated_at"] == "2026-09-06T01:00:00Z"


def test_orphaned_row_is_never_deleted_across_many_passes(tmp_path):
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0002", "Vanishes")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    _durable(state, [])
    for i in range(1, 6):
        hb.reconcile(state, now=NOW + timedelta(hours=i))
    rows = _lifecycle(state)["entries"]
    assert rows["hyp-0002"]["orphaned"] is True
    assert rows["hyp-0002"]["orphaned_at"] == "2026-09-06T01:00:00Z"  # first marking, not re-stamped
    assert rows["hyp-0002"]["status"] in ("active", "stale")  # status untouched by the mark


def test_reappearing_key_clears_the_mark_and_resumes(tmp_path):
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0002", "Comes back")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    _durable(state, [])
    hb.reconcile(state, now=NOW + timedelta(hours=1))
    assert _lifecycle(state)["entries"]["hyp-0002"]["orphaned"] is True
    _durable(state, [_entry("hyp-0002", "Comes back")])
    hb.reconcile(state, now=NOW + timedelta(hours=2))
    row = _lifecycle(state)["entries"]["hyp-0002"]
    assert "orphaned" not in row and "orphaned_at" not in row
    assert row["reappeared_at"] == "2026-09-06T02:00:00Z"
    assert row["last_evaluated"] == "2026-09-06T02:00:00Z"
    assert row["status"] == "active"
    assert hb.lifecycle_counts(state)["orphaned"] == 0


def test_legacy_row_gets_best_known_last_evaluated_when_orphaned(tmp_path):
    """Rows written before #1346 have no last_evaluated; the mark carries the best prior timestamp."""
    state = _state(tmp_path)
    (state / "hypotheses" / "lifecycle.json").write_text(json.dumps({
        "schema_version": "hypothesis-lifecycle-v1",
        "entries": {
            "hypothesis-old-touched": {"status": "active", "first_seen": "2026-07-14T00:00:00Z", "last_touched": "2026-07-15T01:24:28Z"},
            "hypothesis-old-stale": {"status": "stale", "first_seen": "2026-07-14T00:00:00Z", "stale_at": "2026-07-30T00:00:00Z"},
            "hypothesis-old-bare": {"status": "active", "first_seen": "2026-07-14T00:00:00Z"},
        },
    }), encoding="utf-8")
    _durable(state, [])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    rows = _lifecycle(state)["entries"]
    assert rows["hypothesis-old-touched"]["last_evaluated"] == "2026-07-15T01:24:28Z"
    assert rows["hypothesis-old-stale"]["last_evaluated"] == "2026-07-30T00:00:00Z"
    assert rows["hypothesis-old-bare"]["last_evaluated"] == "2026-07-14T00:00:00Z"
    assert all(r["orphaned"] is True for r in rows.values())


# ─── unavailable inputs are not evidence of absence ──────────────────────────


def test_unreadable_input_never_orphans_anything(tmp_path):
    """A corrupt durable.json (or a missing backlog.json) means absence cannot be asserted."""
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0001", "A"), _entry("hyp-0002", "B")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    (state / "hypotheses" / "durable.json").write_text("{not json", encoding="utf-8")
    hb.reconcile(state, now=NOW + timedelta(hours=1))
    data = _lifecycle(state)
    assert all("orphaned" not in row for row in data["entries"].values())
    assert data["last_pass"]["inputs"] == {"durable.json": "unavailable", "backlog.json": "ok"}
    assert data["last_pass"]["inputs_ok"] is False and data["last_pass"]["orphaned_now"] == 0
    assert hb.lifecycle_counts(state)["inputs_unavailable"] == 1
    # backlog missing entirely: same rule
    _durable(state, [_entry("hyp-0001", "A")])
    (state / "hypotheses" / "backlog.json").unlink()
    hb.reconcile(state, now=NOW + timedelta(hours=2))
    data = _lifecycle(state)
    assert "orphaned" not in data["entries"]["hyp-0002"]
    assert data["last_pass"]["inputs"]["backlog.json"] == "unavailable"


def test_valid_empty_inputs_orphan_every_row(tmp_path):
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0001", "A")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    _durable(state, [])
    hb.reconcile(state, now=NOW + timedelta(hours=1))
    counts = hb.lifecycle_counts(state)
    assert counts["total"] == 1 and counts["orphaned"] == 1 and counts["evaluated_last_pass"] == 0


def test_nothing_readable_at_all_is_todays_behaviour(tmp_path):
    state = _state(tmp_path)
    (state / "hypotheses" / "lifecycle.json").write_text(json.dumps({
        "schema_version": "hypothesis-lifecycle-v1", "entries": {"hyp-0001": {"status": "active"}},
    }), encoding="utf-8")
    hb.reconcile(state, now=NOW)  # no durable.json, no backlog.json
    data = _lifecycle(state)
    assert data["entries"] == {"hyp-0001": {"status": "active"}}
    assert "last_pass" not in data


def test_corrupt_lifecycle_sidecar_is_never_overwritten(tmp_path):
    """A sidecar we could not read might hold the 100 rows we promise never to delete."""
    state = _state(tmp_path)
    path = state / "hypotheses" / "lifecycle.json"
    path.write_text("{corrupt", encoding="utf-8")
    _durable(state, [_entry("hyp-0001", "A")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    assert path.read_text(encoding="utf-8") == "{corrupt"  # byte-identical
    assert hb.lifecycle_counts(state) == {}  # no data is not zero of everything
    # the read side still serves candidates fail-open
    assert hb.top_candidates(state) == [{"key": "hyp-0001", "title": "A", "source": "durable", "claim": ""}]


def test_missing_sidecar_is_created_fresh(tmp_path):
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0001", "A")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    assert set(_lifecycle(state)["entries"]) == {"hyp-0001"}
    assert hb.lifecycle_counts(state)["total"] == 1


def test_present_but_untitled_input_row_is_not_orphaned(tmp_path):
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0001", "Titled"), _entry("hyp-0002", "Loses its title")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    _durable(state, [_entry("hyp-0001", "Titled"), {"hypothesis_id": "hyp-0002", "title": ""}])
    hb.reconcile(state, now=NOW + timedelta(hours=1))
    row = _lifecycle(state)["entries"]["hyp-0002"]
    assert "orphaned" not in row  # in the input, just not offered as a candidate
    assert row["last_evaluated"] == "2026-09-06T00:00:00Z"  # not evaluated as a candidate this pass


def test_wrongly_shaped_input_is_unavailable(tmp_path):
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0001", "A")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    (state / "hypotheses" / "durable.json").write_text(json.dumps({"entries": "x"}), encoding="utf-8")
    hb.reconcile(state, now=NOW + timedelta(hours=1))
    data = _lifecycle(state)
    assert data["last_pass"]["inputs"]["durable.json"] == "unavailable"
    assert "orphaned" not in data["entries"]["hyp-0001"]


def test_exception_mid_pass_leaves_the_sidecar_byte_identical(tmp_path, monkeypatch):
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0001", "A")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    path = state / "hypotheses" / "lifecycle.json"
    before = path.read_bytes()

    def _boom(*a, **k):
        raise RuntimeError("ledger read failed")

    monkeypatch.setattr(hb, "_load_ledger_rows", _boom)
    hb.reconcile(state, now=NOW + timedelta(hours=1))  # must not raise
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".lifecycle.json.*.tmp"))


def test_same_id_in_both_sources_is_not_a_collision(tmp_path):
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0001", "A")])
    _backlog(state, [_entry("hyp-0001", "A (queued copy)")])
    hb.reconcile(state, now=NOW)
    assert _lifecycle(state)["last_pass"]["input_id_collisions"] == 0


def test_never_reconciled_sidecar_is_distinguishable_from_zero_orphans(tmp_path):
    state = _state(tmp_path)
    (state / "hypotheses" / "lifecycle.json").write_text(json.dumps({
        "schema_version": "hypothesis-lifecycle-v1", "entries": {"hyp-0001": {"status": "active"}},
    }), encoding="utf-8")
    counts = hb.lifecycle_counts(state)
    assert counts["last_pass_recorded"] == 0 and counts["orphaned"] == 0
    _durable(state, [_entry("hyp-0001", "A")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    assert hb.lifecycle_counts(state)["last_pass_recorded"] == 1


def test_supported_hypotheses_skip_orphaned_rows(tmp_path):
    state = _state(tmp_path)
    (state / "hypotheses" / "lifecycle.json").write_text(json.dumps({
        "schema_version": "hypothesis-lifecycle-v1",
        "entries": {
            "hyp-live": {"status": "answered", "verdict": "supported", "verdict_at": "2026-09-01T00:00:00Z", "title": "live"},
            "hyp-fossil": {"status": "answered", "verdict": "supported", "verdict_at": "2026-07-01T00:00:00Z", "title": "fossil", "orphaned": True},
        },
    }), encoding="utf-8")
    assert [h["title"] for h in hb.supported_hypotheses(state)] == ["live"]


# ─── keys: id preferred, slug counted, claim recorded ────────────────────────


def test_hypothesis_id_preferred_and_slug_fallback_counted(tmp_path):
    state = _state(tmp_path)
    _durable(state, [
        {"hypothesis_id": "hyp-0001", "task_title": "Has an id"},
        {"task_title": "No id, only a title"},
        {"title": "Another title-only row"},
    ])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    data = _lifecycle(state)
    assert set(data["entries"]) == {"hyp-0001", "slug-no-id-only-a-title", "slug-another-title-only-row"}
    assert data["last_pass"]["slug_keyed_candidates"] == 2
    counts = hb.lifecycle_counts(state)
    assert counts["id_keyed"] == 1 and counts["slug_keyed"] == 2


def test_claim_key_is_recorded_not_used_as_key(tmp_path):
    """Two restatements of one claim keep their own rows; the shared claim is counted as a collision."""
    state = _state(tmp_path)
    shared = {
        "hypothesis": "Stale host metrics feed causes the scorecard gap to stay open",
        "action": "Add an in-process mtime refresh of host_metrics inside the validator harness",
    }
    _durable(state, [
        {"hypothesis_id": "hyp-0008", "task_title": "Validator heartbeat touch", **shared},
        {"hypothesis_id": "hyp-0031", "task_title": "Refresh host metrics from the harness", **shared},
        {"hypothesis_id": "hyp-0040", "task_title": "Unrelated", "hypothesis": "x", "action": "y"},
    ])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    rows = _lifecycle(state)["entries"]
    assert set(rows) == {"hyp-0008", "hyp-0031", "hyp-0040"}
    claim_a, claim_b = rows["hyp-0008"].get("claim_key"), rows["hyp-0031"].get("claim_key")
    counts = hb.lifecycle_counts(state)
    if claim_a:  # the #1345 taxonomy resolved this pair
        assert claim_a.startswith("claim-") and claim_a == claim_b
        assert counts["claim_keyed"] >= 2 and counts["claim_collisions"] >= 1
    else:  # taxonomy did not resolve: nothing recorded, nothing counted
        assert counts["claim_keyed"] == 0 and counts["claim_collisions"] == 0
    assert "claim_key" not in rows["hyp-0040"] or rows["hyp-0040"]["claim_key"].startswith("claim-")


# ─── the live files as fixture ───────────────────────────────────────────────


def test_live_fixture_marks_the_fossils_and_exposes_the_counts(tmp_path):
    """2026-09-05 21:34 MSK: lifecycle 115 rows, durable 20, backlog 0 -> 100 orphans."""
    state = _state(tmp_path)
    for name in ("backlog.json", "durable.json", "lifecycle.json"):
        shutil.copy(FIXTURE_DIR / name, state / "hypotheses" / name)
    before = hb.lifecycle_counts(state)
    assert before["total"] == 115 and before["orphaned"] == 0 and before["evaluated_last_pass"] == 0
    assert before["last_pass_recorded"] == 0  # 0 orphans here means "not yet measured"

    hb.reconcile(state, now=NOW)

    after = hb.lifecycle_counts(state)
    durable_entries = json.loads((FIXTURE_DIR / "durable.json").read_text(encoding="utf-8"))["entries"]
    durable_ids = {e["hypothesis_id"] for e in durable_entries}
    # 20 entries but 15 distinct ids: the strategist reused hyp-0021 (x4) and
    # hyp-0022 (x3), so 5 entries hide behind another entry's lifecycle row
    assert len(durable_entries) == 20 and len(durable_ids) == 15
    assert after["input_id_collisions"] == 5
    assert after["total"] == 115
    assert after["evaluated_last_pass"] == 15
    assert after["orphaned"] == 115 - 15 == 100
    assert after["slug_keyed"] == 2 and after["id_keyed"] == 113
    assert after["inputs_unavailable"] == 0
    # 91 of 115 rows are keyed by ids of the planner retired in #923
    assert (after["prefix_hypothesis"], after["prefix_hyp"], after["prefix_slug"], after["prefix_other"]) == (91, 22, 2, 0)
    rows = _lifecycle(state)["entries"]
    assert all(rows[k]["orphaned"] is True and rows[k]["last_evaluated"] for k in rows if k not in durable_ids)
    assert all("orphaned" not in rows[k] and rows[k]["last_evaluated"] == "2026-09-06T00:00:00Z" for k in durable_ids)
    # the fossils are still there, with their old status
    assert rows["hypothesis-refresh-approval-gate"]["status"] == "answered"
    assert rows["hypothesis-refresh-approval-gate"]["last_evaluated"] == "2026-07-15T01:24:28.087907Z"


def test_lifecycle_counts_keys_are_additive_to_the_878_set(tmp_path):
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0001", "A")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    counts = hb.lifecycle_counts(state)
    for key in ("active", "answered", "supported", "refuted", "inconclusive"):
        assert key in counts
    for key in ("total", "stale", "orphaned", "evaluated_last_pass", "id_keyed", "slug_keyed",
                "claim_keyed", "claim_collisions", "inputs_unavailable", "input_id_collisions",
                "prefix_hypothesis", "prefix_hyp", "prefix_slug", "prefix_other", "last_pass_recorded"):
        assert key in counts


def test_reused_hypothesis_ids_are_counted_as_input_collisions(tmp_path):
    state = _state(tmp_path)
    _durable(state, [_entry("hyp-0021", "First idea"), _entry("hyp-0021", "Second idea, same id"), _entry("hyp-0022", "Third")])
    _backlog(state, [])
    hb.reconcile(state, now=NOW)
    assert set(_lifecycle(state)["entries"]) == {"hyp-0021", "hyp-0022"}  # one row stands for two entries
    assert _lifecycle(state)["last_pass"]["input_id_collisions"] == 1
    assert hb.lifecycle_counts(state)["input_id_collisions"] == 1
