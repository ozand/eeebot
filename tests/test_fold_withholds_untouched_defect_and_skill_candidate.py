"""#1801 -- widen #1764's rule to defect and skill-candidate demand ids.

Found auditing #1785's demand_id chain (the self_dedup replacement): the
chain fires on 7 rejects over 7 days, and 6 of the 7 are NOT real
duplicates. The cause is not the chain -- it is that a defect/skill-
candidate demand_id names a CLASS of work with several distinct proposals
minted under it, so the first success retires all of them regardless of
what it actually touched. Exactly #1764's failure, in a kind #1764 did not
cover.

``demand._fold_completed`` itself was ALREADY kind-agnostic (its own
withholding check only looks at ``summaries_by_id.get(demand_id)``,
never at what kind of id it is) -- see
``tests/test_fold_withholds_untouched_priority.py`` for that mechanism's
own tests, all still valid unchanged. The actual gap was one call site:
``collect_demand`` only ever put "priority" kind summaries into
``summaries_by_id``, so a defect/skill-candidate id's summary was never
available at fold time and the rule silently never applied to those two
kinds. This file covers the widened call site.

Replay (2026-09-19, real host data -- ``state/demand/completed.json`` +
the full ledger history, ``state/ledger/cycles-*.jsonl.gz`` + the live
file):

    relevant (defect/skill-candidate) completed.json entries    193
    of those, retiring cycle's own title names no path             31
    of the 162 judgeable, would fold as before (path touched)     157
    WOULD NOW BE WITHHELD                                            5
    of those 5, a genuine delivery (false positive)                  0

(The 5 used the retiring cycle's own ``proposed.task_title`` as a proxy
for the original mint summary -- historical mint text is not durably
recorded for algorithmically-derived kinds the way a goal_text.json
priority's title is, so this is a conservative lower bound, not the exact
count the live rule would produce. All 5 changed only memory files or an
unrelated output, never the artifact their own title named -- the same
bookkeeping-claim pattern #1764 found for priorities.)

Directly confirmed against LIVE, currently-regenerable summaries (the two
of the seven chain-fired ids whose underlying source condition still
exists today) -- on the exact motivating example:

    defect-c2d7044a8332 (NOT the real duplicate -- see the correction
    below): live summary is "subagent result error: 5593622b" -- names no
    path, so the widened rule cannot judge it and it stays retired,
    unaffected either way.

    skill-candidate-3684831689ac (one of the six false matches -- the
    issue's own quoted example, "created skills/verify-lessons-integrity/
    SKILL.md -> different artifact entirely"): live summary is "package
    recurring procedure: write:* -> exec:python3 -m pytest
    tests/test_lessons_integrity.py", naming
    ``tests/test_lessons_integrity.py`` -- the retiring cycle's own
    ``files_changed`` (``skills/verify-lessons-integrity/SKILL.md``,
    ``tests/test_verify_lessons_integrity_skill.py``) never touches that
    exact file, so the widened rule WOULD have withheld it.

CORRECTION: an earlier revision of this file called
``defect-c2d7044a8332`` "the ONE real duplicate of the seven", inherited
from #1785's own mislabeling (that PR matched the same rejected proposal's
title against a DIFFERENT mechanism -- the retired text matcher's own git-
log hit -- and never checked it against ITS OWN chain match). Re-examined
against each id's ACTUAL retiring cycle: the real duplicate is
``defect-791431b59fa3`` (rejected "Add companion test suite for test
existence validator in tests/test_validate_test_existence.py"; retiring
cycle touched ``scripts/validate_test_existence.py`` AND
``tests/test_validate_test_existence.py`` -- the same file, a genuine
match). ``defect-c2d7044a8332`` is one of the six false ones, exactly like
the issue's own quoted pair. 1 real / 6 false stands; only which one is
real changes.

#1801 does not make the chain reject again for any of the 7 audited
historical fires -- see #1801's own PR (`chain-log-only`, PR #1807, merged
first) for why the chain now only OBSERVES a completed-chain match
(``reason: "self_dedup_observed"``) instead of terminating the proposal.
What THIS PR restores is narrower and additive: reject power for a
candidate_id whose completed-chain entry the fold ITSELF verified against
a named path (``entries[id]["path_verified"] is True`` --
:func:`demand.completed_demand_ids_path_verified`). Every one of the 7
audited ids folded before this field existed and (being append-only) never
gains it retroactively, so all 7 stay observe-only under the actual
mechanism today, matching the "6 of 7 -- those where the id is class-
scoped" expectation as the SAFE limit case (7 of 7, since none of the 7
are ever retroactively re-verified): no historical entry can ever
spuriously regain reject power; only a FUTURE completion the fold directly
checks against a named path can.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import cycle_ledger, demand


def _rows(demand_id: str, files: list[str], cycle: str = "cycle-1") -> list[dict]:
    return [
        {"phase": "proposed", "cycle_id": cycle, "demand_id": demand_id},
        {"phase": "outcome", "cycle_id": cycle, "outcome": "success",
         "ts": "2026-09-19T00:00:00Z", "files_changed": files},
    ]


# ---------------------------------------------------------------------------
# _fold_completed itself needs no code change -- these pin that it is
# already kind-agnostic, using "defect"/"skill-candidate" ids explicitly.
# ---------------------------------------------------------------------------

def test_a_defect_bookkeeping_close_is_withheld(tmp_path: Path):
    """The measured 2026-09-19 case, reconstructed: a defect naming a
    script and a retiring cycle that never touched it."""
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("defect-abc", ["memory/facts/ledger-completion-proof.md"]),
        summaries_by_id={
            "defect-abc": "repair: re-wire idle skill skills/foo/SKILL.md — extend or consume it",
        },
    )
    assert "defect-abc" not in completed


def test_a_skill_candidate_bookkeeping_close_is_withheld(tmp_path: Path):
    """The issue's own quoted example, reconstructed with the live mint
    summary: names tests/test_lessons_integrity.py; the retiring cycle
    touched a different test file entirely."""
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows(
            "skill-candidate-3684831689ac",
            ["skills/verify-lessons-integrity/SKILL.md", "tests/test_verify_lessons_integrity_skill.py"],
        ),
        summaries_by_id={
            "skill-candidate-3684831689ac": (
                "package recurring procedure: write:* -> "
                "exec:python3 -m pytest tests/test_lessons_integrity.py"
            ),
        },
    )
    assert "skill-candidate-3684831689ac" not in completed


def test_an_opaque_summary_naming_no_path_is_unaffected_either_way(tmp_path: Path):
    """defect-c2d7044a8332 -- one of the six FALSE chain matches (see the
    file docstring's correction; this is not the real duplicate). Its live
    summary ("subagent result error: 5593622b") names no path, so the rule
    cannot judge it -- it stays retired regardless, exactly as AC2 requires
    ("must not apply where it cannot judge"). This is the id the widened
    rule structurally CANNOT protect against: the false match survives
    #1801 too, which is exactly why #1801 does not, by itself, restore
    reject power for it (see path_verified below)."""
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("defect-c2d7044a8332", ["scripts/scan_archive.py", "tests/test_scan_archive.py"]),
        summaries_by_id={"defect-c2d7044a8332": "subagent result error: 5593622b"},
    )
    assert "defect-c2d7044a8332" in completed


def test_a_defect_genuine_delivery_still_folds(tmp_path: Path):
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("defect-real", ["scripts/validate_skill_name.py", "tests/test_x.py"]),
        summaries_by_id={"defect-real": "validator scripts/validate_skill_name.py fails when run"},
    )
    assert "defect-real" in completed


def test_a_defect_naming_no_path_folds_as_before(tmp_path: Path):
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("defect-xyz", ["memory/facts/ledger-completion-proof.md"]),
        summaries_by_id={"defect-xyz": "curator: 3 staged fact(s) have no keyword overlap"},
    )
    assert "defect-xyz" in completed


def test_goal_gap_is_not_widened_here(tmp_path: Path):
    """#1801 widens ONLY defect and skill-candidate -- goal-gap already has
    its own TTL-based re-offer path (#778) and was not part of this
    issue's audit. An id with no entry in summaries_by_id folds as
    before, same as any other unwidened kind."""
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("goal-gap-abc", ["memory/facts/ledger-completion-proof.md"]),
        summaries_by_id={},
    )
    assert "goal-gap-abc" in completed


# ---------------------------------------------------------------------------
# path_verified -- the additive fix on top of chain-log-only (#1807): reject
# power for the SUBSET of completed ids the fold itself checked against a
# named path, restored ADDITIVELY, never retroactively for a pre-existing
# entry (see the file docstring's correction for why that matters).
# ---------------------------------------------------------------------------

def test_a_genuine_delivery_is_marked_path_verified(tmp_path: Path):
    demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("defect-real", ["scripts/validate_skill_name.py", "tests/test_x.py"]),
        summaries_by_id={"defect-real": "validator scripts/validate_skill_name.py fails when run"},
    )
    assert "defect-real" in demand.completed_demand_ids_path_verified(tmp_path)


def test_a_summary_naming_no_path_is_not_path_verified(tmp_path: Path):
    """defect-c2d7044a8332's own case: folds (nothing to withhold — the
    rule cannot judge it) but must NOT be trusted as if it had been
    checked. This is what keeps a class-scoped, opaque-summary id from
    ever regaining reject power."""
    demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("defect-c2d7044a8332", ["scripts/scan_archive.py"]),
        summaries_by_id={"defect-c2d7044a8332": "subagent result error: 5593622b"},
    )
    assert "defect-c2d7044a8332" in demand.completed_demand_ids(tmp_path)
    assert "defect-c2d7044a8332" not in demand.completed_demand_ids_path_verified(tmp_path)


def test_an_unwidened_kind_is_not_path_verified(tmp_path: Path):
    """goal-gap (and every other kind #1801 does not widen) folds exactly
    as before AND is never path_verified, even when its own summary
    happens to name a path -- the caller never puts its summary into
    summaries_by_id at all, so the fold never gets a chance to check it."""
    demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("goal-gap-abc", ["scripts/foo.py"]),
        summaries_by_id={},
    )
    assert "goal-gap-abc" in demand.completed_demand_ids(tmp_path)
    assert "goal-gap-abc" not in demand.completed_demand_ids_path_verified(tmp_path)


def test_a_pre_existing_entry_never_becomes_path_verified_retroactively(tmp_path: Path):
    """Append-only: an id already in completed.json from BEFORE this field
    existed is left exactly as it was. A later fold pass that could now
    verify it does not rewrite the existing entry -- this is the guarantee
    that makes "all 7 historical chain-fires stay observe-only, forever"
    true by construction, not by a one-time migration that could drift."""
    completed_path = tmp_path / "demand" / "completed.json"
    completed_path.parent.mkdir(parents=True)
    completed_path.write_text(json.dumps({
        "schema_version": "demand-completed-v1",
        "entries": {
            "defect-legacy": {
                "cycle_id": "cycle-old", "ts": "2026-01-01T00:00:00Z",
                "files_changed": ["scripts/legacy.py"], "change_tier": "code-bearing", "serves": "",
            },
        },
    }), encoding="utf-8")

    demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("defect-legacy", ["scripts/legacy.py"], cycle="cycle-old"),
        summaries_by_id={"defect-legacy": "validator scripts/legacy.py fails when run"},
    )

    assert "defect-legacy" in demand.completed_demand_ids(tmp_path)
    assert "defect-legacy" not in demand.completed_demand_ids_path_verified(tmp_path)


# ---------------------------------------------------------------------------
# collect_demand's own summaries_by_id construction -- the actual #1801 fix
# ---------------------------------------------------------------------------

def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    (state_dir / "goals").mkdir(parents=True)
    return state_dir


def _write_goal_text(state_dir: Path, text: str) -> None:
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"text": text}), encoding="utf-8",
    )


def test_collect_demand_withholds_an_untouched_defect_end_to_end(tmp_path, monkeypatch):
    """Before #1801: collect_demand only ever built summaries_by_id from
    "priority" items, so this defect's demand_id folded as completed
    despite never touching the path its own summary named. After: it is
    withheld and the defect is offered again."""
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")

    demand_id = demand.item_id("defect", "repair: re-wire idle skill skills/foo/SKILL.md")
    fake_item = {"id": demand_id, "kind": "defect", "summary": "repair: re-wire idle skill skills/foo/SKILL.md"}
    monkeypatch.setattr(demand, "_ledger_defects", lambda *a, **kw: [fake_item])
    for other in (
        "_result_file_defects", "_compile_defects", "_heldout_defect_items",
        "_validator_defect_items", "_skill_eval_defect_items",
        "_knowledge_lift_defect_items", "_curator_unsupported_items",
        "_tamper_defect_items", "_skill_candidate_items",
    ):
        monkeypatch.setattr(demand, other, lambda *a, **kw: [])

    cycle_ledger.append_event(state_dir, {
        "phase": "proposed", "cycle_id": "cycle-1", "demand_id": demand_id,
        "task_title": "some unrelated refinement",
    })
    cycle_ledger.append_event(state_dir, {
        "phase": "outcome", "cycle_id": "cycle-1", "outcome": "success",
        "files_changed": ["memory/facts/ledger-completion-proof.md"],
    })

    demand.collect_demand(state_dir, None)

    assert demand_id not in demand.completed_demand_ids(state_dir)
    withheld = json.loads((state_dir / "demand" / "fold_withheld.json").read_text(encoding="utf-8"))
    assert any(w["demand_id"] == demand_id for w in withheld["withheld"])

    # It is still presented — the withholding did not strand it.
    remaining_ids = {i["id"] for i in demand.collect_demand(state_dir, None)}
    assert demand_id in remaining_ids


def test_collect_demand_withholds_an_untouched_skill_candidate_end_to_end(tmp_path, monkeypatch):
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")

    summary = "package recurring procedure: write:* -> exec:python3 -m pytest tests/test_lessons_integrity.py"
    demand_id = demand.item_id("skill-candidate", summary)
    fake_item = {"id": demand_id, "kind": "skill-candidate", "summary": summary}
    monkeypatch.setattr(demand, "_skill_candidate_items", lambda *a, **kw: [fake_item])
    for other in (
        "_ledger_defects", "_result_file_defects", "_compile_defects",
        "_heldout_defect_items", "_validator_defect_items",
        "_skill_eval_defect_items", "_knowledge_lift_defect_items",
        "_curator_unsupported_items", "_tamper_defect_items",
    ):
        monkeypatch.setattr(demand, other, lambda *a, **kw: [])

    cycle_ledger.append_event(state_dir, {
        "phase": "proposed", "cycle_id": "cycle-1", "demand_id": demand_id,
        "task_title": "Package recurring lesson write and integrity pytest verification into verify-lessons-integrity skill",
    })
    cycle_ledger.append_event(state_dir, {
        "phase": "outcome", "cycle_id": "cycle-1", "outcome": "success",
        "files_changed": ["skills/verify-lessons-integrity/SKILL.md", "tests/test_verify_lessons_integrity_skill.py"],
    })

    demand.collect_demand(state_dir, None)

    assert demand_id not in demand.completed_demand_ids(state_dir)


def test_collect_demand_still_folds_a_genuine_defect_delivery(tmp_path, monkeypatch):
    """The AC's other half: this must not become a permanent quarantine for
    every defect. A cycle that touches the named path retires normally."""
    state_dir = _state_dir(tmp_path)
    _write_goal_text(state_dir, "no priority section, so should_propose is True")

    demand_id = demand.item_id("defect", "validator scripts/validate_skill_name.py fails when run")
    fake_item = {"id": demand_id, "kind": "defect", "summary": "validator scripts/validate_skill_name.py fails when run"}
    monkeypatch.setattr(demand, "_validator_defect_items", lambda *a, **kw: [fake_item])
    for other in (
        "_ledger_defects", "_result_file_defects", "_compile_defects",
        "_heldout_defect_items", "_skill_eval_defect_items",
        "_knowledge_lift_defect_items", "_curator_unsupported_items",
        "_tamper_defect_items", "_skill_candidate_items",
    ):
        monkeypatch.setattr(demand, other, lambda *a, **kw: [])

    cycle_ledger.append_event(state_dir, {
        "phase": "proposed", "cycle_id": "cycle-1", "demand_id": demand_id,
        "task_title": "Fix scripts/validate_skill_name.py",
    })
    cycle_ledger.append_event(state_dir, {
        "phase": "outcome", "cycle_id": "cycle-1", "outcome": "success",
        "files_changed": ["scripts/validate_skill_name.py", "tests/test_validate_skill_name.py"],
    })

    demand.collect_demand(state_dir, None)

    assert demand_id in demand.completed_demand_ids(state_dir)
