"""ADR-034 (docs/adr/ADR-034-two-operator-documents-one-root-each.md) rules 2
and 3: one resolver per operator document, and the operator priority list's
four rule-3 states. Issue #1937 (A1) landed the resolver half of the
Test Contract; issue #1938 (A2) migrates the #1699-census readers onto
these resolvers, deletes their fallback paths, and closes the reader half
of the contract — covered by the tests below.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import demand, goal_review, llm_proposer, strategist_inputs
from nanobot.runtime.operator_documents import (
    DOCUMENT_SIZE_CAP_BYTES,
    PRIORITY_ALL_COMPLETED,
    PRIORITY_EMPTY,
    PRIORITY_PRESENT,
    PRIORITY_UNAVAILABLE,
    SOURCE_DERIVED,
    SOURCE_OPERATOR,
    STATE_ABSENT,
    STATE_TEXT,
    STATE_UNREADABLE,
    operator_priorities_status,
    resolve_charter,
    resolve_derived_priorities,
    resolve_operator_priorities,
)
from nanobot.runtime.state import load_runtime_state_from_root


def _goal_text_json(state_dir: Path, raw_text: str) -> None:
    goals_dir = state_dir / "goals"
    goals_dir.mkdir(parents=True, exist_ok=True)
    (goals_dir / "goal_text.json").write_text(
        json.dumps({"schema_version": "goal-text-v1", "goal_id": "g", "text": raw_text}),
        encoding="utf-8",
    )


def _mark_completed(state_dir: Path, num: str, title: str, instructions: str) -> None:
    """Write a synthetic completed-demand sidecar (#773) marking one priority
    entry done, the same id ``filter_completed_priorities_from_goal_text``
    derives from it — no git repo needed."""
    item = demand._make_item("priority", f"Priority {num} — {title}", instructions)
    demand_dir = state_dir / "demand"
    demand_dir.mkdir(parents=True, exist_ok=True)
    (demand_dir / "completed.json").write_text(
        json.dumps({"schema_version": "demand-completed-v1", "entries": {item["id"]: {"kind": "priority"}}}),
        encoding="utf-8",
    )


def test_each_document_resolves_from_its_one_root(tmp_path: Path, monkeypatch):
    """ADR-034 rule 2: resolver half (#1937/A1) — each resolver reads only
    its one root; reader half (#1938/A2) — every migrated reader obtains
    each document only through its resolver, ignoring decoys planted at
    every other (legacy/instance/state) path the ADR condemns."""
    # Charter: <release root>/goals.md only.
    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "goals.md").write_text("the charter text", encoding="utf-8")

    state_dir = tmp_path / "state"
    (state_dir / "goals").mkdir(parents=True)
    (state_dir / "goals.md").write_text("decoy legacy charter, must never be read", encoding="utf-8")

    charter = resolve_charter(release_root)
    assert charter.state == STATE_TEXT
    assert charter.text == "the charter text"

    # A release root with no goals.md never falls back to the state dir's decoy.
    empty_release_root = tmp_path / "release-empty"
    empty_release_root.mkdir()
    assert resolve_charter(empty_release_root).state == STATE_ABSENT

    # Derived priorities: state/goals/derived_priorities.json only.
    (state_dir / "goals" / "derived_priorities.json").write_text(
        json.dumps({"schema_version": "derived-v1", "priorities": [{"label": "D", "body": "do it", "number": 9, "vector": "V1"}]}),
        encoding="utf-8",
    )
    (state_dir / "derived_priorities.json").write_text("decoy at the wrong root", encoding="utf-8")
    derived = resolve_derived_priorities(state_dir)
    assert derived.state == STATE_TEXT
    assert len(derived.entries) == 1
    assert derived.entries[0].source == SOURCE_DERIVED
    assert derived.entries[0].number == 9

    # Operator priorities: state/goals/goal_text.json only, ignoring the
    # legacy state_dir/goals.md decoy planted above.
    _goal_text_json(state_dir, "Current priority targets:\n(A) Priority 1 — Do a thing: write scripts/x.py.")
    priorities = resolve_operator_priorities(state_dir)
    assert priorities.state == PRIORITY_PRESENT
    assert len(priorities.open_entries) == 1
    entry = priorities.open_entries[0]
    assert entry.source == SOURCE_OPERATOR
    assert entry.number == 1
    assert entry.title == "Do a thing"
    assert entry.instructions == "write scripts/x.py."

    # ─── reader half (#1938/A2) ──────────────────────────────────────────
    monkeypatch.setenv("RELEASE_ROOT", str(release_root))
    instance_repo = tmp_path / "instance"
    instance_repo.mkdir()
    (instance_repo / "goals.md").write_text("decoy instance charter", encoding="utf-8")

    # goal_review.read_charter_text: release root only.
    assert goal_review.read_charter_text(release_root) == "the charter text"

    # demand._charter_as_loop_sees_it: release root only (env), ignoring
    # both decoys even though selfevo_repo=instance_repo is passed.
    text, source, _path = demand._charter_as_loop_sees_it(state_dir, instance_repo)
    assert text == "the charter text"
    assert source == "release_goals_md"

    # state.py's dashboard surface: active_goal from state/goals/goal_text.json only.
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"goal_id": "goal-from-state-root", "text": "just operator priorities, never a charter"}),
        encoding="utf-8",
    )
    runtime = load_runtime_state_from_root(state_dir)
    assert runtime["active_goal"] == "goal-from-state-root"


def test_four_priority_states_are_distinct(tmp_path: Path):
    state_present = tmp_path / "present"
    _goal_text_json(
        state_present,
        "Current priority targets:\n(A) Priority 1 — Do a thing: write scripts/x.py.",
    )
    result_present = resolve_operator_priorities(state_present)
    assert result_present.state == PRIORITY_PRESENT
    assert result_present.open_count == 1
    assert result_present.completed_count == 0

    state_done = tmp_path / "done"
    instructions = "write scripts/x.py and commit."
    _goal_text_json(
        state_done,
        f"Current priority targets:\n(A) Priority 1 — Do a thing: {instructions}",
    )
    _mark_completed(state_done, "1", "Do a thing", instructions)
    result_done = resolve_operator_priorities(state_done)
    assert result_done.state == PRIORITY_ALL_COMPLETED
    assert result_done.completed_count == 1
    assert result_done.open_count == 0
    assert result_done.completed_entries[0].title == "Do a thing"

    state_empty = tmp_path / "empty"
    _goal_text_json(state_empty, "just a description, no priorities listed.")
    result_empty = resolve_operator_priorities(state_empty)
    assert result_empty.state == PRIORITY_EMPTY

    state_missing = tmp_path / "missing"
    result_missing = resolve_operator_priorities(state_missing)
    assert result_missing.state == PRIORITY_UNAVAILABLE

    # All four are pairwise distinct — none collapses into another.
    assert {
        result_present.state,
        result_done.state,
        result_empty.state,
        result_missing.state,
    } == {PRIORITY_PRESENT, PRIORITY_ALL_COMPLETED, PRIORITY_EMPTY, PRIORITY_UNAVAILABLE}


def test_whitespace_only_goal_text_json_is_unavailable_not_empty(tmp_path: Path):
    """A goal_text.json that is only whitespace is a stray/blank file, not a
    document validly listing no priorities: it resolves ``absent`` at the
    document level and therefore ``unavailable`` for the priority list — not
    ``empty`` (ADR-034 rule 3: ``empty`` means the document is valid and
    lists none)."""
    state_dir = tmp_path / "state"
    goals_dir = state_dir / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "goal_text.json").write_text("   \n\t  \n", encoding="utf-8")

    result = resolve_operator_priorities(state_dir)

    assert result.state == PRIORITY_UNAVAILABLE
    assert result.reason == "empty_file"


def test_boundary_documents_leak_no_text(tmp_path: Path):
    sensitive = "the operator secretly wants a mars colony by friday"

    # --- operator priorities: empty (real content, no priority section) ---
    state_empty = tmp_path / "empty"
    _goal_text_json(state_empty, sensitive)
    result_empty = resolve_operator_priorities(state_empty)
    assert result_empty.state == PRIORITY_EMPTY
    assert sensitive not in result_empty.reason
    assert sensitive not in repr(result_empty)
    assert sensitive not in str(result_empty)

    # --- corrupt: malformed JSON carrying the sensitive text ---
    state_corrupt = tmp_path / "corrupt"
    goals_dir = state_corrupt / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "goal_text.json").write_text(
        '{"text": "' + sensitive + '" not valid json }}}', encoding="utf-8"
    )
    result_corrupt = resolve_operator_priorities(state_corrupt)
    assert result_corrupt.state == PRIORITY_UNAVAILABLE
    assert result_corrupt.reason == "malformed_json"
    assert sensitive not in result_corrupt.reason
    assert sensitive not in repr(result_corrupt)

    # --- oversize: valid JSON, but over the cap — must not even be read ---
    state_oversize = tmp_path / "oversize"
    goals_dir = state_oversize / "goals"
    goals_dir.mkdir(parents=True)
    oversized_text = sensitive + ("x" * (DOCUMENT_SIZE_CAP_BYTES + 1))
    (goals_dir / "goal_text.json").write_text(
        json.dumps({"text": oversized_text}), encoding="utf-8"
    )
    result_oversize = resolve_operator_priorities(state_oversize)
    assert result_oversize.state == PRIORITY_UNAVAILABLE
    assert result_oversize.reason == "oversize"
    assert sensitive not in result_oversize.reason
    assert sensitive not in repr(result_oversize)

    # --- present, with real content: repr/str must still not leak it ---
    state_present = tmp_path / "present-sensitive"
    _goal_text_json(
        state_present,
        f"Current priority targets:\n(A) Priority 1 — Title: {sensitive}",
    )
    result_present = resolve_operator_priorities(state_present)
    assert result_present.state == PRIORITY_PRESENT
    assert sensitive not in repr(result_present)
    assert sensitive not in str(result_present)
    for entry in result_present.open_entries:
        assert sensitive not in repr(entry)
        assert sensitive not in str(entry)

    # --- charter: present and oversize ---
    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "goals.md").write_text(sensitive, encoding="utf-8")
    charter_present = resolve_charter(release_root)
    assert charter_present.state == STATE_TEXT
    assert sensitive not in repr(charter_present)
    assert sensitive not in str(charter_present)

    oversize_release_root = tmp_path / "release-oversize"
    oversize_release_root.mkdir()
    (oversize_release_root / "goals.md").write_text(
        sensitive + ("x" * (DOCUMENT_SIZE_CAP_BYTES + 1)), encoding="utf-8"
    )
    charter_oversize = resolve_charter(oversize_release_root)
    assert charter_oversize.state == STATE_UNREADABLE
    assert charter_oversize.reason == "oversize"
    assert sensitive not in repr(charter_oversize)

    # --- derived priorities: present entries and corrupt JSON ---
    state_derived_present = tmp_path / "derived-present"
    goals_dir = state_derived_present / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "derived_priorities.json").write_text(
        json.dumps({"priorities": [{"label": "Title", "body": sensitive, "number": 1, "vector": "V1"}]}),
        encoding="utf-8",
    )
    derived_present = resolve_derived_priorities(state_derived_present)
    assert derived_present.state == STATE_TEXT
    assert len(derived_present.entries) == 1
    assert sensitive not in repr(derived_present)
    assert sensitive not in str(derived_present)
    for entry in derived_present.entries:
        assert sensitive not in repr(entry)

    state_derived_corrupt = tmp_path / "derived-corrupt"
    goals_dir = state_derived_corrupt / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "derived_priorities.json").write_text(
        '{"priorities": [{"label": "T", "body": "' + sensitive + '"  broken', encoding="utf-8"
    )
    derived_corrupt = resolve_derived_priorities(state_derived_corrupt)
    assert derived_corrupt.state == STATE_UNREADABLE
    assert derived_corrupt.reason == "malformed_json"
    assert sensitive not in repr(derived_corrupt)

    # No boundary priority resolution ever exposes a raw-text field.
    for result in (result_empty, result_corrupt, result_oversize):
        assert not hasattr(result, "text")


def _goal_review_rows(state_dir: Path) -> list[dict]:
    path = state_dir / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("phase") == "goal_review"]


def test_instance_goals_md_is_ignored_by_every_reader(tmp_path: Path, monkeypatch):
    """ADR-034 rule 2: an instance-repo ``goals.md`` is never consulted by
    any A2-migrated reader — the #944-era "charter-first from the instance
    repo" step (always dead in production: an instance ``goals.md`` never
    existed) is deleted, not kept."""
    monkeypatch.delenv("RELEASE_ROOT", raising=False)
    state_dir = tmp_path / "state"
    instance_repo = tmp_path / "instance"
    instance_repo.mkdir()
    (instance_repo / "goals.md").write_text(
        "Current priority targets:\n(A) Priority 1 — Instance decoy: never read.\n",
        encoding="utf-8",
    )

    assert demand._priority_items(state_dir, instance_repo) == []
    assert demand._artifact_gap_items(state_dir, instance_repo) == []

    text, _source, path = demand._charter_as_loop_sees_it(state_dir, instance_repo)
    assert "Instance decoy" not in text
    assert path is None or path.parent != instance_repo

    assert goal_review._load_goal_data(state_dir, None) is None

    from nanobot.runtime.llm_proposer import _load_goal_text

    assert _load_goal_text(state_dir) == ""


def test_absent_and_unreadable_follow_the_reader_table(tmp_path: Path, monkeypatch):
    """ADR-034 rule 3: a missing/unreadable charter stops the reader (with a
    reason recorded); a missing/unreadable operator-priority document never
    stops the reader and is reported "unavailable" — never read as "no
    priorities" (that is ``empty``, a different state)."""
    monkeypatch.setenv(goal_review.ENABLED_ENV, "1")

    # --- charter absent: goal-review stops, but the reason is recorded ---
    state_dir = tmp_path / "state"
    empty_release_root = tmp_path / "empty-release"
    empty_release_root.mkdir()

    titles = goal_review.maybe_goal_review(state_dir, None, release_root=empty_release_root)

    assert titles == []
    rows = _goal_review_rows(state_dir)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "no_goal_text"

    # --- operator priorities absent: the reader still runs; the priority
    # queue reports nothing and "unavailable", never "no priorities" ---
    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "goals.md").write_text("a real charter", encoding="utf-8")
    state_dir_no_priorities = tmp_path / "state-no-priorities"

    items = demand._priority_items(state_dir_no_priorities, None)
    assert items == []  # "priority-*" emits nothing
    status = operator_priorities_status(state_dir_no_priorities)
    assert status.state == PRIORITY_UNAVAILABLE
    assert status.state != PRIORITY_EMPTY  # never mistaken for "no priorities"

    # The charter section is unaffected by the operator priorities being
    # absent — absence of one never silences the other.
    view = demand.build_derived_view(state_dir_no_priorities, None)
    assert view["priority_items"] == []

    # --- charter UNREADABLE (oversize) follows the same path as absent —
    # goal review stops here too, not just on a genuinely missing file ---
    oversize_release_root = tmp_path / "oversize-release"
    oversize_release_root.mkdir()
    (oversize_release_root / "goals.md").write_text(
        "x" * (DOCUMENT_SIZE_CAP_BYTES + 1), encoding="utf-8"
    )
    state_dir_unreadable = tmp_path / "state-unreadable-charter"
    titles_unreadable = goal_review.maybe_goal_review(
        state_dir_unreadable, None, release_root=oversize_release_root
    )
    assert titles_unreadable == []
    rows_unreadable = _goal_review_rows(state_dir_unreadable)
    assert len(rows_unreadable) == 1
    assert rows_unreadable[0]["outcome"] == "no_goal_text"

    good_release_root = tmp_path / "good-release"
    good_release_root.mkdir()
    (good_release_root / "goals.md").write_text("a real charter", encoding="utf-8")

    # --- proposer: charter absent/unreadable stops should_propose ---
    monkeypatch.setenv(llm_proposer.ENABLED_ENV, "1")
    monkeypatch.setenv("SELFEVO_DEMAND_DRIVEN_ENABLED", "0")
    for bad_release_root in (empty_release_root, oversize_release_root):
        proposer_state = tmp_path / f"proposer-{bad_release_root.name}"
        (proposer_state / "goals").mkdir(parents=True)
        monkeypatch.setenv("RELEASE_ROOT", str(bad_release_root))
        assert llm_proposer.should_propose(proposer_state, None) is False
    proposer_state_ok = tmp_path / "proposer-ok"
    (proposer_state_ok / "goals").mkdir(parents=True)
    monkeypatch.setenv("RELEASE_ROOT", str(good_release_root))
    assert llm_proposer.should_propose(proposer_state_ok, None) is True

    # --- strategist: charter absent/unreadable refuses, regardless of the
    # other four inputs being "complete" ---
    for charter_status in ("empty", "unavailable"):
        status = {
            "goals": {"status": charter_status},
            "scorecard": {"status": "complete"},
            "funnel": {"status": "complete"},
            "insights": {"status": "complete"},
            "evolution_tree": {"status": "complete"},
        }
        assert strategist_inputs.should_refuse(status) is True

    # --- artifact-gap: reader status "unavailable" when charter
    # absent/unreadable, never conflated with "ok, just no surface named" ---
    for bad_release_root in (empty_release_root, oversize_release_root):
        assert demand.artifact_gap_status(bad_release_root) == demand.ARTIFACT_GAP_STATUS_UNAVAILABLE
    assert demand.artifact_gap_status(good_release_root) == demand.ARTIFACT_GAP_STATUS_OK


def test_charter_view_is_the_charter(tmp_path: Path, monkeypatch):
    """ADR-034 rule 2: ``demand._charter_as_loop_sees_it`` never returns the
    operator's private priority text relabelled as the charter — the
    #944-era bug that leaked it into the public ``derived_view.json`` under
    ``source="goal_text_json"``."""
    monkeypatch.delenv("RELEASE_ROOT", raising=False)
    state_dir = tmp_path / "state"
    _goal_text_json(state_dir, "OPERATOR PRIVATE PRIORITY TEXT")

    text, source, path = demand._charter_as_loop_sees_it(state_dir, None)

    assert text == ""
    assert source == "none"
    assert path is None
    assert "OPERATOR PRIVATE PRIORITY TEXT" not in text

    # And the published view carries the same guarantee end to end.
    view = demand.build_derived_view(state_dir, None)
    assert view["charter"] == {"source": "none", "merged": False, "text": ""}


def test_missing_charter_keeps_diagnostics_running(tmp_path: Path, monkeypatch):
    """ADR-034 rule 3: a missing charter stops choosing/proposing/executing,
    but never diagnostics or reason-recording — goal-review's ledger row is
    still written (the "no_goal_text" reason), and unrelated readers that
    do not need the charter keep working."""
    monkeypatch.setenv(goal_review.ENABLED_ENV, "1")
    state_dir = tmp_path / "state"
    empty_release_root = tmp_path / "empty-release"
    empty_release_root.mkdir()

    result = goal_review.maybe_goal_review(state_dir, None, release_root=empty_release_root)

    assert result == []  # not None: the kill switch didn't fire, the review ran and no-opped
    rows = _goal_review_rows(state_dir)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "no_goal_text"

    # A reader that never needed the charter (derived-priorities queue
    # depth, used by health) is unaffected by the charter's absence.
    from nanobot.runtime.health import read_derived_priorities_queue

    queue = read_derived_priorities_queue(state_dir)
    assert queue == {"depth": 0, "limit": goal_review._DERIVED_PRIORITIES_MAX}

    # The publisher (demand.build_derived_view/publish_derived_view) keeps
    # running too: it still produces a full, well-formed view — showing
    # the charter as unavailable, never raising or going silent.
    monkeypatch.setenv("RELEASE_ROOT", str(empty_release_root))
    view = demand.build_derived_view(state_dir, None)
    assert view["charter"] == {"source": "none", "merged": False, "text": ""}
    assert view["schema_version"] == demand.DERIVED_VIEW_SCHEMA
    result_dict = demand.publish_derived_view(state_dir, None)
    assert result_dict["ok"] is True


def test_status_surface_never_renders_priority_text(tmp_path: Path, monkeypatch):
    """ADR-034 rule 3: the dashboard status surface
    (``state.load_runtime_state_from_root``) shows availability for
    ``goal_text.json``, never its text — even when the document holds
    real, sensitive content."""
    monkeypatch.delenv("RELEASE_ROOT", raising=False)
    sensitive = "the operator secretly wants a mars colony by friday"
    state_dir = tmp_path / "state"
    (state_dir / "goals").mkdir(parents=True)
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"goal_id": "goal-1", "text": sensitive}), encoding="utf-8"
    )

    runtime = load_runtime_state_from_root(state_dir)

    assert runtime["active_goal"] == "goal-1"
    assert runtime["goal_text"] in (PRIORITY_PRESENT, PRIORITY_ALL_COMPLETED, PRIORITY_EMPTY, PRIORITY_UNAVAILABLE)
    assert sensitive not in json.dumps(runtime)
