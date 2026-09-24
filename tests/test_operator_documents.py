"""ADR-034 (docs/adr/ADR-034-two-operator-documents-one-root-each.md) rules 2,
3 and 4: one resolver per operator document, the operator priority list's
four rule-3 states, and provenance surviving every hop. Issue #1937 (A1)
landed the resolver half of the Test Contract; issue #1938 (A2) migrates the
#1699-census readers onto these resolvers, deletes their fallback paths, and
closes the reader half of the contract. Issue #1939 (A3) replaces
goal_review.merged_goal_text's single numbered list with operator and
derived priorities resolved and completed-filtered independently, each
keeping source through demand, ranking and the prompt — covered by the two
rule-4 Test Contract tests at the end of this file.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from nanobot.runtime import demand, goal_review, llm_proposer, strategist_inputs
from nanobot.runtime.operator_documents import (
    DOCUMENT_SIZE_CAP_BYTES,
    PRIORITIES_BLOCK_CAP,
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
    render_priorities_block,
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

    # ─── prompt half (ADR-034 rule 5, #1940/A4) ────────────────────────────
    # The four states stay distinguishable once rendered into the prompt
    # block every A4 reader shows — `unavailable` in particular is worded so
    # it is never mistaken for "the operator has no priorities" (rule 3).
    block_present = render_priorities_block(result_present, ())
    block_done = render_priorities_block(result_done, ())
    block_empty = render_priorities_block(result_empty, ())
    block_missing = render_priorities_block(result_missing, ())

    assert "Do a thing" in block_present
    assert "Open:" in block_present

    assert "completed" in block_done.lower()
    assert "write scripts/x.py and commit." not in block_done  # instructions never shown, headers only

    assert "no priorities" in block_empty.lower()

    assert "unavailable" in block_missing.lower()
    # Worded as a negation ("NOT the same as ... no priorities"), never as
    # a bare claim that none exist.
    assert "not the same as" in block_missing.lower()

    # Pairwise distinct as rendered text too.
    assert len({block_present, block_done, block_empty, block_missing}) == 4


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


# ─── ADR-034 rule 4 (issue #1939, A3): provenance survives every hop ───────


def test_priority_provenance_survives_end_to_end(tmp_path: Path, monkeypatch):
    """Test Contract: operator and derived priorities are never merged into
    one numbered list; each item's source survives demand collection,
    ranking, and the proposer's own prompt. Completed filtering runs on
    each list separately and keeps the surviving item's label."""
    state_dir = tmp_path / "state"
    op_open_body = "do the open operator thing."
    op_done_body = "do the done operator thing."
    _goal_text_json(
        state_dir,
        "Current priority targets:\n"
        f"(A) Priority 1 — Operator open (V2): {op_open_body}\n"
        f"(B) Priority 2 — Operator done: {op_done_body}",
    )
    derived_open_body = "do the open derived thing."
    derived_done_body = "do the done derived thing."
    (state_dir / "goals" / "derived_priorities.json").write_text(
        json.dumps({
            "schema_version": "derived-v1",
            "priorities": [
                {"label": "Derived open", "body": derived_open_body, "number": 9, "vector": "V1"},
                {"label": "Derived done", "body": derived_done_body, "number": 10, "vector": "V1"},
            ],
        }),
        encoding="utf-8",
    )
    # _mark_completed writes the whole sidecar file, so both entries are
    # marked in one call — a second call would overwrite the first.
    op_done_item = demand._make_item("priority", "Priority 2 — Operator done", op_done_body)
    derived_done_item = demand._make_item("priority", "Priority 10 — Derived done", derived_done_body)
    demand_dir = state_dir / "demand"
    demand_dir.mkdir(parents=True, exist_ok=True)
    (demand_dir / "completed.json").write_text(
        json.dumps({
            "schema_version": "demand-completed-v1",
            "entries": {
                op_done_item["id"]: {"kind": "priority"},
                derived_done_item["id"]: {"kind": "priority"},
            },
        }),
        encoding="utf-8",
    )

    # Through demand: each list's completed entry is filtered out on its
    # OWN text — the other list's open entry is unaffected either way —
    # and every surviving item carries its real source, never a guess.
    items = demand._priority_items(state_dir, None)
    summaries = {i["summary"]: i for i in items}
    assert set(summaries) == {"Priority 1 — Operator open (V2)", "Priority 9 — Derived open"}
    assert summaries["Priority 1 — Operator open (V2)"]["provenance"] == demand.PROVENANCE_OPERATOR
    assert summaries["Priority 9 — Derived open"]["provenance"] == demand.PROVENANCE_SELF_DERIVED

    # Through ranking: operator outranks self-derived regardless of vector
    # (both here are effectively V2-vs-V1, and operator still sorts first).
    assert [i["provenance"] for i in items] == [
        demand.PROVENANCE_OPERATOR, demand.PROVENANCE_SELF_DERIVED,
    ]

    # Through the prompt: the proposer's own context carries the derived
    # list under its OWN, source-labeled heading — never folded into the
    # charter's own text (goal_review.merged_goal_text, #1665, superseded).
    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "goals.md").write_text("the charter", encoding="utf-8")
    monkeypatch.setenv("RELEASE_ROOT", str(release_root))
    context = llm_proposer.build_context(state_dir, None)
    assert "## Derived priorities (source: derived" in context
    assert "Derived open" in context
    assert "Derived done" not in context  # completed, filtered independently here too
    # ADR-034 rule 5 (A4): the operator's OWN list now reaches this prompt
    # too, under its own heading, distinct from the charter and from the
    # derived section above.
    assert "## Operator priorities" in context
    assert "Operator open" in context
    # Completed is headers only (architect decision): the title survives as
    # a do-not-repeat marker, its instructions body does not.
    assert "- 2. Operator done" in context
    assert op_done_body not in context

    # Through demand-driven mode's own "## Demand" section: when the
    # collected items themselves are what the model sees (demand_items),
    # each item's real source is labeled inline — never a merged,
    # unlabeled list either.
    demand_context = llm_proposer.build_context(state_dir, None, demand_items=items)
    assert "## Demand" in demand_context
    assert "Priority 1 — Operator open (V2) [source: operator]" in demand_context
    assert "Priority 9 — Derived open [source: self-derived]" in demand_context


def test_merged_text_consumers_migrated_with_source(tmp_path: Path, monkeypatch):
    """Test Contract: every consumer of the FORMER merged numbered text —
    goal_review's dedup-label baseline and its next-priority-number
    assignment — reads operator and derived priorities on their own texts
    and still correctly dedupes/numbers across BOTH lists, never merging
    them into one blob first (goal_review.merged_goal_text, #860/#1665,
    superseded by this migration)."""
    from tests.test_goal_review import (
        GOAL_TEXT,
        NOW,
        VALID_PRIORITY,
        _goal_review_rows,
        _seed_valid_evidence,
        _write_goal_text,
        _write_snapshot,
    )

    monkeypatch.setenv(goal_review.ENABLED_ENV, "1")
    state_dir = tmp_path / "state"
    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "goals.md").write_text("a real charter", encoding="utf-8")

    _write_goal_text(state_dir, GOAL_TEXT)  # operator: Priority 11, 16 open
    _write_snapshot(state_dir, [{
        "metric": "repeat_failure_rate", "vector": "V1", "current": 0.5, "target": 0.3,
        "evidence": (
            "repeat_failure_rate=0.5 is above max target 0.3 over the last "
            "7d window (goal vector V1)"
        ),
    }])
    _seed_valid_evidence(state_dir)
    goal_review._write_derived_priorities(state_dir, [{
        "label": "Trim proposer retry burn", "vector": "V1",
        "body": "Already derived yesterday.", "number": 17, "added_utc": "2026-08-01T00:00:00Z",
    }])

    # A candidate duplicating the DERIVED list's label — never the
    # operator's own text — is still rejected: the dedup baseline unions
    # labels from both lists, each scanned on its own text.
    captured_ctx: dict[str, str] = {}

    def _capture_and_reply(ctx: str) -> dict:
        captured_ctx["ctx"] = ctx
        return {"priorities": [VALID_PRIORITY]}

    monkeypatch.setattr(goal_review, "_call_llm", _capture_and_reply)
    titles = goal_review.maybe_goal_review(state_dir, None, now=NOW, release_root=release_root)
    assert titles == []
    row = _goal_review_rows(state_dir)[-1]
    assert row["outcome"] == "no_valid_priorities"
    assert row["rejected"] == [{"label": "Trim proposer retry burn", "reason": "duplicate"}]

    # The key structural check: goal_review's OWN LLM prompt keeps the
    # derived list under its own heading, separate from the operator's
    # "Current priority targets:" section — never folded into the SAME
    # numbered list the way merged_goal_text used to (pre-A3, the derived
    # entry's rendered "Priority 17 — Trim proposer retry burn (V1): ..."
    # line landed INSIDE goal_text's own "Current priority targets:"
    # section, indistinguishable from an operator-authored entry).
    ctx = captured_ctx["ctx"]
    goal_section = ctx.split("## Derived priorities", 1)[0]
    assert "Trim proposer retry burn" not in goal_section
    assert "## Derived priorities (source: derived" in ctx
    derived_section = ctx.split("## Derived priorities (source: derived", 1)[1]
    assert "Trim proposer retry burn" in derived_section.split("##", 1)[0]

    # A genuinely new candidate is still numbered past the highest
    # "Priority N" in EITHER list — the operator's 16 and the derived
    # list's 17 — giving 18: the union each list's own numbers is
    # preserved even though they are never scanned as one blob.
    new_priority = {**VALID_PRIORITY, "label": "A brand new priority"}
    monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: {"priorities": [new_priority]})
    titles2 = goal_review.maybe_goal_review(
        state_dir, None, now=NOW + timedelta(days=2), release_root=release_root,
    )
    assert titles2 == ["Priority 18 — A brand new priority"]
    derived = goal_review.read_derived_priorities(state_dir)
    assert [d["number"] for d in derived] == [17, 18]


# ─── ADR-034 rule 5 (issue #1940, A4): executor, proposer and planner see
# the operator priorities ──────────────────────────────────────────────────


def test_proposer_sees_operator_priorities_end_to_end(tmp_path: Path, monkeypatch):
    """Test Contract: with operator priorities present and not completed,
    the proposer's own context carries the entry (number, title and
    instructions) under its own heading so a proposal can cite it; with
    every listed priority completed, the context shows the rule-3
    one-line notice instead — Completed headers only, never the
    instructions body (architect decision, 2026-09-24)."""
    monkeypatch.delenv("RELEASE_ROOT", raising=False)
    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "goals.md").write_text("the charter", encoding="utf-8")
    monkeypatch.setenv("RELEASE_ROOT", str(release_root))

    state_present = tmp_path / "present"
    _goal_text_json(
        state_present,
        "Current priority targets:\n(A) Priority 4 — Trim the retry loop: cut wasted calls.",
    )
    context_present = llm_proposer.build_context(state_present, None)
    assert "## Operator priorities" in context_present
    assert "4. Trim the retry loop: cut wasted calls." in context_present

    state_done = tmp_path / "done"
    instructions = "cut wasted calls."
    _goal_text_json(
        state_done,
        f"Current priority targets:\n(A) Priority 4 — Trim the retry loop: {instructions}",
    )
    _mark_completed(state_done, "4", "Trim the retry loop", instructions)
    context_done = llm_proposer.build_context(state_done, None)
    assert "## Operator priorities" in context_done
    assert "completed" in context_done.lower()
    assert "4. Trim the retry loop: cut wasted calls." not in context_done  # headers only


def test_prompt_blocks_present_and_bounded(tmp_path: Path, monkeypatch):
    """Test Contract: executor and planner prompts carry the operator-
    priorities block under its own heading, within its own fixed budget
    (:data:`PRIORITIES_BLOCK_CAP`), counted only in the caller's overall
    prompt cap -- never displacing the charter or the release ontology's
    OPERATING.md floor (ADR-034 rule 5, architect decision 2026-09-24).
    Over the block's own cap it renders whole as ``unavailable``/
    ``oversize``, never a partial, silently-truncated render."""
    from nanobot.agent.context import ContextBuilder

    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "goals.md").write_text("the charter", encoding="utf-8")
    (release_root / "IDENTITY.md").write_text("# IDENTITY\n\nWho I am.", encoding="utf-8")
    (release_root / "SOUL.md").write_text("# SOUL\n\nWhat I value.", encoding="utf-8")
    (release_root / "USER.md").write_text("# USER\n\nOperator profile.", encoding="utf-8")
    (release_root / "OPERATING.md").write_text("# OPERATING\n\n" + ("cycle rule. " * 50), encoding="utf-8")

    state_dir = tmp_path / "state"
    _goal_text_json(
        state_dir,
        "Current priority targets:\n(A) Priority 1 — Do a thing: write scripts/x.py.",
    )

    builder = ContextBuilder(tmp_path / "workspace", release_root=release_root, state_dir=state_dir)
    prompt = builder.build_system_prompt(loop_profile=True)

    assert len(prompt) <= ContextBuilder.MAX_SYSTEM_PROMPT_CHARS
    assert "## Operator priorities" in prompt
    assert "## goals.md" in prompt  # the charter, never displaced
    assert "# OPERATING" in prompt  # the release floor, never displaced
    operating_chars = builder.last_fit["sections"].get("operating", 0)
    assert operating_chars >= len("# OPERATING\n\n" + "cycle rule. " * 50) - 1

    # Over the block's own cap: unavailable/oversize whole, never a partial
    # render silently cut down to fit.
    huge_state = tmp_path / "state-huge"
    lines = "\n".join(
        f"(A) Priority {n} — Do thing {n}: " + ("word " * 80) for n in range(1, 20)
    )
    _goal_text_json(huge_state, f"Current priority targets:\n{lines}")
    huge_builder = ContextBuilder(tmp_path / "workspace2", release_root=release_root, state_dir=huge_state)
    huge_block = huge_builder._load_priorities_block()
    assert len(huge_block) <= PRIORITIES_BLOCK_CAP + len("## Operator priorities\n\n")
    assert "unavailable" in huge_block.lower()
    assert "reason: oversize" in huge_block.lower()
    assert "Do thing 1" not in huge_block  # no partial content, ever

    # Planner: same renderer, same shared budget, end to end through
    # bridge._run_planning_session's own task message.
    import asyncio

    from nanobot.runtime import bridge
    from tests.test_run_planning_session import _fake_config, _init_repo_with_origin, _make_fake_mgr_factory

    repo = _init_repo_with_origin(tmp_path)
    skill = release_root / "nanobot" / "skills" / "task-writing" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# task-writing contract\n", encoding="utf-8")
    manager_factory = _make_fake_mgr_factory(state_dir, {"insight": "x", "plan": "y", "iterations_planned": 10})
    monkeypatch.setattr(bridge, "SubagentManager", manager_factory)
    monkeypatch.setattr(bridge, "RELEASE_ROOT", release_root)

    outcome = asyncio.run(bridge._run_planning_session(
        provider=object(), bus=object(), config=_fake_config(), model="test-model",
        state_dir=state_dir, selfevo_repo=repo, denied_paths=set(), cycle_id="cycle-1",
    ))

    assert outcome["ran"] is True
    planner_task = manager_factory.last_task or ""
    assert "## Operator priorities" in planner_task
    assert "Do a thing" in planner_task  # the one open entry seeded above
    assert len(planner_task) <= 20_000  # generously bounded; not swamped by the priorities block


def test_prompt_never_picks_a_priority_for_the_executor(tmp_path: Path):
    """Test Contract: nothing in prompt assembly selects an item for the
    executor. The operator-priorities block states the order is intent,
    not instruction (ADR-034 rule 5); and the executor's own task message
    (``bridge.build_task``) still reads generically -- "priorities are
    handled by the proposer" -- when the request carries no
    ``curriculum_level``, unaffected by the new block's presence."""
    from nanobot.runtime.bridge import build_task
    from nanobot.runtime.operator_documents import PRIORITY_INTENT_LINE

    state_dir = tmp_path / "state"
    _goal_text_json(
        state_dir,
        "Current priority targets:\n"
        "(A) Priority 1 — First thing: do it.\n"
        "(B) Priority 2 — Second thing: do it too.",
    )
    res = resolve_operator_priorities(state_dir)
    block = render_priorities_block(res, ())
    assert PRIORITY_INTENT_LINE in block
    assert "you must work on priority 1" not in block.lower()
    assert "start with priority" not in block.lower()

    task = build_task({}, "goal text", "")
    assert "This task is not an operator priority; priorities are handled by the proposer." in task
