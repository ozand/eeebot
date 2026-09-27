"""#1764 -- a priority naming a file is not retired by a cycle that never touched it.

Audited all 39 operator priorities in ``state/demand/completed.json`` against
the cycle that retired each and that cycle's real ``files_changed``::

    FINISHED                                  24
    BOOKKEEPING                               15
    titles naming a path                      34
    retiring commits that never touched it    14

Every one of the 14 changed only a memory file, claiming completion rather
than performing it. ``_is_completed`` then suppresses the id permanently, and
the only unfolding code in this module is a spent one-off migration -- so the
operator's request is gone for good.

Replayed before building, per #1328: the rule blocks 14 of 39 and zero of the
24 genuine deliveries. Narrowed to confidently-extracted paths it keeps 12 of
the 14, still with zero false positives.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import demand
from nanobot.runtime.demand import paths_named_in_summary, title_path_touched


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def test_extracts_a_directory_prefixed_path():
    assert paths_named_in_summary(
        "Priority 4 - Implement render_cycle_strip in scripts/eeebot_dashboard.py"
    ) == ["scripts/eeebot_dashboard.py"]


def test_extracts_a_bare_filename():
    assert paths_named_in_summary("Priority 9 - rewrite MEMORY.md newest-first") == ["MEMORY.md"]


def test_extracts_both_paths_when_a_title_names_two():
    """One real title does this; the rule must see both, and `any` decides."""
    assert paths_named_in_summary(
        "Priority 10 - HISTORY.md newest-first: update cycle_logger.py to prepend"
    ) == ["HISTORY.md", "cycle_logger.py"]


def test_extracts_nothing_from_a_title_naming_a_symbol_not_a_file():
    """5 of the 39 name a function with no file. The rule must not apply."""
    assert paths_named_in_summary("Priority 20 - Consolidate the skills catalogue") == []
    assert paths_named_in_summary("Add a smoke check for the loop breaker") == []


def test_prose_is_not_mistaken_for_a_path():
    """The pattern anchors on a known extension. A looser one reads ordinary
    sentences as filenames -- measured wrong on 17 of the 39 real titles."""
    assert paths_named_in_summary("Tighten the e.g. wording and the i.e. case") == []
    assert paths_named_in_summary("Version 1.2 of the report") == []


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

def test_directory_prefixed_path_matches_exactly_and_as_a_suffix():
    assert title_path_touched(["scripts/foo.py"], ["scripts/foo.py"])
    assert title_path_touched(["scripts/foo.py"], ["repo/scripts/foo.py"])
    assert not title_path_touched(["scripts/foo.py"], ["scripts/other.py"])


def test_bare_filename_matches_on_basename():
    """Looser on purpose: the rule only ever WITHHOLDS, so a loose match can
    under-block but can never strand a priority that was really delivered."""
    assert title_path_touched(["MEMORY.md"], ["memory/MEMORY.md"])
    assert not title_path_touched(["MEMORY.md"], ["docs/plan.md"])


def test_any_named_path_suffices_not_all():
    """Requiring all would block a legitimate two-step delivery."""
    assert title_path_touched(["HISTORY.md", "cycle_logger.py"], ["scripts/cycle_logger.py"])


def test_windows_separators_and_leading_slashes_normalise():
    assert title_path_touched(["scripts/foo.py"], ["\\scripts\\foo.py"])
    assert title_path_touched(["scripts/foo.py"], ["/scripts/foo.py"])


def test_no_changed_files_is_not_a_touch():
    assert not title_path_touched(["scripts/foo.py"], [])
    assert not title_path_touched(["scripts/foo.py"], None)


# ---------------------------------------------------------------------------
# the fold
# ---------------------------------------------------------------------------

def _rows(demand_id: str, files: list[str], cycle: str = "cycle-1") -> list[dict]:
    return [
        {"phase": "proposed", "cycle_id": cycle, "demand_id": demand_id},
        {"phase": "outcome", "cycle_id": cycle, "outcome": "success",
         "ts": "2026-09-19T00:00:00Z", "files_changed": files},
    ]


def test_bookkeeping_close_is_withheld(tmp_path: Path):
    """The measured failure: the title asks for a dashboard function and the
    retiring commit changed only MEMORY.md."""
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("priority-abc", ["memory/facts/ledger-completion-proof.md"]),
        summaries_by_id={
            "priority-abc": "Priority 4 - Implement render_cycle_strip in scripts/eeebot_dashboard.py",
        },
    )
    assert "priority-abc" not in completed


def test_a_real_delivery_still_folds(tmp_path: Path):
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("priority-abc", ["scripts/eeebot_dashboard.py", "tests/test_x.py"]),
        summaries_by_id={
            "priority-abc": "Priority 4 - Implement render_cycle_strip in scripts/eeebot_dashboard.py",
        },
    )
    assert "priority-abc" in completed


def test_a_title_naming_no_path_folds_as_before(tmp_path: Path):
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("priority-xyz", ["memory/facts/ledger-completion-proof.md"]),
        summaries_by_id={"priority-xyz": "Priority 20 - Consolidate the skills catalogue"},
    )
    assert "priority-xyz" in completed


def test_an_unknown_id_folds_as_before(tmp_path: Path):
    """No summary available at fold time (a kind the caller does not pass
    into ``summaries_by_id`` at all, or one this pass could not
    regenerate) -- no rule. #1801 widens the caller's kind set to include
    defect and skill-candidate (see
    ``test_fold_withholds_untouched_defect_and_skill_candidate.py``); a
    reflection id, used here, stays outside it."""
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("reflection-123", ["memory/facts/ledger-completion-proof.md"]),
        summaries_by_id={},
    )
    assert "reflection-123" in completed


def test_creation_counts_as_touching(tmp_path: Path):
    """11 of the audited cases ask for a file that does not exist yet. A new
    file appears in files_changed exactly like a modified one."""
    completed = demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("priority-new", ["scripts/generate_system_map.py"]),
        summaries_by_id={"priority-new": "Priority 7 - Implement scripts/generate_system_map.py"},
    )
    assert "priority-new" in completed


def test_withholding_is_recorded_with_its_evidence(tmp_path: Path):
    demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("priority-abc", ["memory/facts/ledger-completion-proof.md"]),
        summaries_by_id={"priority-abc": "Priority 4 - render_cycle_strip in scripts/eeebot_dashboard.py"},
    )
    data = json.loads((tmp_path / "demand" / "fold_withheld.json").read_text(encoding="utf-8"))
    assert data["schema"] == demand.FOLD_WITHHELD_SCHEMA
    assert data["last_pass_ts"]
    entry = data["withheld"][0]
    assert entry["demand_id"] == "priority-abc"
    assert entry["named_paths"] == ["scripts/eeebot_dashboard.py"]
    assert entry["files_changed"] == ["memory/facts/ledger-completion-proof.md"]


def test_the_record_is_written_even_when_nothing_was_withheld(tmp_path: Path):
    """A guard that leaves no trace when it does nothing reads exactly like
    one that never ran -- last_pass_ts is what separates the two."""
    demand._fold_completed(
        tmp_path,
        ledger_rows=_rows("priority-abc", ["scripts/eeebot_dashboard.py"]),
        summaries_by_id={"priority-abc": "Priority 4 - scripts/eeebot_dashboard.py"},
    )
    data = json.loads((tmp_path / "demand" / "fold_withheld.json").read_text(encoding="utf-8"))
    assert data["withheld"] == []
    assert data["last_pass_ts"]


def test_a_withheld_priority_folds_later_when_a_cycle_does_the_work(tmp_path: Path):
    """Withheld is not retired: the priority stays offerable, and a later
    cycle that touches the named path retires it normally. Without this the
    rule would strand exactly what it means to protect."""
    summaries = {"priority-abc": "Priority 4 - Implement scripts/eeebot_dashboard.py"}
    demand._fold_completed(
        tmp_path, ledger_rows=_rows("priority-abc", ["memory/facts/ledger-completion-proof.md"], "cycle-1"),
        summaries_by_id=summaries,
    )
    completed = demand._fold_completed(
        tmp_path, ledger_rows=_rows("priority-abc", ["scripts/eeebot_dashboard.py"], "cycle-2"),
        summaries_by_id=summaries,
    )
    assert "priority-abc" in completed
