"""#1171: the reflector mint earns a card on recurrence, folds on agreement,
and walks the journal with a cursor instead of a 50-row window.

Live store 2026-08-27..09-02: 738 rows, 502 promotable items, all
``approach_hint``; 448 of the 502 were lexical singletons at Jaccard 0.35 and
the ~21 clusters that recurred across cycles were the general lessons. The
pre-#1171 mint staged the first two items of the newest 50 rows once a day,
folded on ``summary`` (the cycle narrative — 179 same-row siblings folded into
each other and lost their solution) and wrote that narrative as the card's
``problem``.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
import yaml

from nanobot.runtime import knowledge_curator as kc
from nanobot.runtime.knowledge_curator import (
    _LESSONS_PAYLOAD_SLUG,
    _STAGED_DIR,
    LESSONS_REL,
    apply_staged_lesson_cards,
    load_reflector_pool,
    promote_reflector_recommendations_to_v2,
)

DETAIL = "Configure a fallback model group for the local model in LiteLLM so server crashes fail over automatically."
DETAIL_PARAPHRASE = "Define fallback routes for the local model group in the LiteLLM configuration so crashes fail over."
OTHER = "Register newly added AGENTS.md sections in tests/test_agents_structure.py so standing instructions stay covered."


def _row(cycle: str, ts: str, *recs: tuple[str, str], summary: str = "", kind: str = "approach_hint") -> dict:
    return {
        "cycle_id": cycle, "timestamp": ts,
        "summary": summary or f"The agent did a number of things in {cycle}",
        "recommendations": [{"kind": kind, "detail": detail, "evidence": evidence} for detail, evidence in recs],
    }


def _write_live(state: Path, rows: list[dict]) -> Path:
    path = state / "reflector" / "reflections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _write_archive(state: Path, day: str, rows: list[dict]) -> Path:
    path = state / "reflector" / "archive" / f"reflections-{day}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    return path


def _workspace(tmp_path: Path, cards: list[dict] | None = None) -> Path:
    workspace = tmp_path / "workspace"
    target = workspace / LESSONS_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    if cards is not None:
        target.write_text(yaml.safe_dump({"lessons": cards}, sort_keys=False), encoding="utf-8")
    return workspace


def _staged_cards(state: Path) -> list[dict]:
    payload = state / "curator" / _STAGED_DIR / _LESSONS_PAYLOAD_SLUG
    if not payload.exists():
        return []
    return json.loads(payload.read_text(encoding="utf-8"))["cards"]


def _apply(workspace: Path, state: Path) -> list[str]:
    payload = state / "curator" / _STAGED_DIR / _LESSONS_PAYLOAD_SLUG
    return apply_staged_lesson_cards(workspace, json.loads(payload.read_text(encoding="utf-8")))


def _lessons(workspace: Path) -> list[dict]:
    return yaml.safe_load((workspace / LESSONS_REL).read_text(encoding="utf-8"))["lessons"]


def _card(card_id: str, solution: str, problem: str, *, seen: int = 1, evidence: list[str] | None = None) -> dict:
    return {
        "schema_version": 2, "id": card_id, "title": solution[:200], "problem": problem, "solution": solution,
        "tags": ["reflector"], "severity": "medium", "seen_count": seen,
        "first_seen": "2026-08-29", "last_seen": "2026-08-29", "evidence": evidence or [],
    }


# ─── the rule ────────────────────────────────────────────────────────────────


def test_first_sighting_waits_in_the_pool_and_is_not_minted(tmp_path: Path) -> None:
    """Pre-#1171 this staged one card from one sighting (and would have staged
    ~70 a day at the live rate)."""
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    _write_live(state, [_row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z", (DETAIL, "LiteLLM returned 502 for three calls"))])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 0

    assert _staged_cards(state) == []
    pool = load_reflector_pool(state)
    assert pool["last_run"]["items"] == 1 and pool["last_run"]["pooled_new"] == 1 and pool["last_run"]["staged"] == 0
    assert [c["cycles"] for c in pool["clusters"]] == [["cycle-aaaaaaaaaaa1"]]


def test_two_cycles_on_two_days_graduate_one_card_with_the_observation_as_problem(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    _write_live(state, [
        _row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z", (DETAIL, "LiteLLM returned 502 for three calls")),
        _row("cycle-bbbbbbbbbbb2", "2026-09-02T11:00:00Z", (DETAIL_PARAPHRASE, "The model server dropped the connection twice")),
    ])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 1

    [card] = _staged_cards(state)
    assert card["id"].startswith("LESS-REF-aaaaaaaaaaa1-")
    assert card["solution"] == DETAIL
    assert card["problem"] == "LiteLLM returned 502 for three calls", "problem is the observation, not the cycle narrative"
    assert card["seen_count"] == 2
    assert card["evidence"] == ["cycle-aaaaaaaaaaa1", "cycle-bbbbbbbbbbb2"]
    assert card["distinct_days"] == 2
    assert card["first_seen"] == "2026-09-01" and card["last_seen"] == "2026-09-02"
    pool = load_reflector_pool(state)
    assert pool["clusters"] == [] and pool["last_run"]["graduated"] == 1
    assert pool["last_staged_at"]


def test_same_day_recurrence_is_an_incident_echo_and_waits(tmp_path: Path) -> None:
    """Seven cycles failing the same way in one afternoon is one event. It is
    counted, visible, and not minted until a second day confirms it."""
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    _write_live(state, [
        _row(f"cycle-{i:012d}", f"2026-09-01T1{i}:00:00Z", (DETAIL, f"502 number {i}")) for i in range(3)
    ])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 0
    pool = load_reflector_pool(state)
    assert pool["last_run"]["same_day_only_waiting"] == 1
    [cluster] = pool["clusters"]
    assert len(cluster["cycles"]) == 3 and cluster["days"] == ["2026-09-01"]

    _write_live(state, [
        _row(f"cycle-{i:012d}", f"2026-09-01T1{i}:00:00Z", (DETAIL, f"502 number {i}")) for i in range(3)
    ] + [_row("cycle-nextday00000", "2026-09-02T09:00:00Z", (DETAIL_PARAPHRASE, "502 again the next morning"))])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 1
    [card] = _staged_cards(state)
    assert card["seen_count"] == 4 and card["distinct_days"] == 2


def test_same_row_siblings_are_separate_lessons_not_folded_into_each_other(tmp_path: Path) -> None:
    """The pre-#1171 fold key was ``summary``: two recommendations from one
    cycle shared it, so the second folded into the first and its solution was
    discarded (179 of 502 live items)."""
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    _write_live(state, [
        _row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z",
             (DETAIL, "502s during the run"), (OTHER, "AGENTS.md gained a section without a test"),
             summary="One narrative shared by both recommendations"),
        _row("cycle-bbbbbbbbbbb2", "2026-09-02T10:00:00Z",
             (DETAIL, "502s again"), (OTHER, "another AGENTS.md section without a test"),
             summary="One narrative shared by both recommendations"),
    ])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 2
    assert sorted(c["solution"] for c in _staged_cards(state)) == sorted([DETAIL, OTHER])


def test_recurrence_that_agrees_with_an_existing_card_folds_and_is_not_capped(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = _workspace(tmp_path, [_card("LESS-REF-000000000000-0000", DETAIL, "502s seen before", evidence=["cycle-000000000000"])])
    _write_live(state, [_row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z", (DETAIL_PARAPHRASE, "502s seen again"))])

    # max_items=0: no NEW card may be minted; the fold still goes through.
    assert promote_reflector_recommendations_to_v2(workspace, state, max_items=0) == 1
    pool = load_reflector_pool(state)
    assert pool["last_run"]["folded"] == 1 and pool["last_run"]["graduated"] == 0 and pool["clusters"] == []

    assert _apply(workspace, state) == [_staged_cards(state)[0]["id"]]
    [card] = _lessons(workspace)
    assert card["id"] == "LESS-REF-000000000000-0000", "folded, not inserted"
    assert card["seen_count"] == 2
    assert card["evidence"] == ["cycle-000000000000", "cycle-aaaaaaaaaaa1"]
    assert card["last_seen"] == "2026-09-01"


def test_narrative_problem_is_repaired_from_the_cards_own_origin_item(tmp_path: Path) -> None:
    """Two cards on origin/main (2026-09-02) carry the cycle narrative as
    ``problem``. Their origin items are still in the store; re-reading them
    repairs the problem in place and does not re-count the sighting."""
    narrative = "Added a doctest suite to tests/test_jsonl_stream_filter.py validating docstring examples"
    state = tmp_path / "state"
    workspace = _workspace(tmp_path, [_card("LESS-REF-4f03664182da-0d35", DETAIL, narrative, evidence=["cycle-4f03664182da"])])
    _write_live(state, [_row("cycle-4f03664182da", "2026-09-02T03:00:00Z", (DETAIL, "LiteLLM returned 502 for three calls"), summary=narrative)])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 1
    assert load_reflector_pool(state)["last_run"]["repaired"] == 1
    _apply(workspace, state)
    [card] = _lessons(workspace)
    assert card["problem"] == "LiteLLM returned 502 for three calls"
    assert card["seen_count"] == 1, "the origin item is not a second sighting"
    assert card["evidence"] == ["cycle-4f03664182da"]


def test_2026_08_29_filler_card_is_reached_through_its_problem_and_repaired(tmp_path: Path) -> None:
    """The four surviving 08-29 cards store the recommendation as ``problem``
    with the filler ``solution`` — no recommendation could match them on
    solution text, so #1106's upgrade path was unreachable for them. The
    recurrence of that recommendation upgrades the solution and puts the
    observation where the problem belongs."""
    state = tmp_path / "state"
    workspace = _workspace(tmp_path, [_card("LESS-REF-5b349fbfddd0", "Apply the reflected approach hint.", OTHER,
                                            evidence=["cycle-5b349fbfddd0"])])
    _write_live(state, [
        _row("cycle-5b349fbfddd0", "2026-08-29T10:00:00Z", (OTHER, "AGENTS.md gained a section with no structural test")),
        _row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z", (OTHER, "another AGENTS.md section landed untested")),
    ])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 2
    assert load_reflector_pool(state)["last_run"]["folded"] == 2
    _apply(workspace, state)
    [card] = _lessons(workspace)
    assert card["id"] == "LESS-REF-5b349fbfddd0"
    assert card["solution"] == OTHER
    assert card["problem"] == "AGENTS.md gained a section with no structural test"
    assert card["seen_count"] == 2 and card["evidence"] == ["cycle-5b349fbfddd0", "cycle-aaaaaaaaaaa1"]


def test_filler_solution_on_an_old_card_is_upgraded_by_a_matching_problem(tmp_path: Path) -> None:
    """#1106's upgrade path, now reached through the shared fold rule."""
    state = tmp_path / "state"
    workspace = _workspace(tmp_path, [{
        "id": "LESS-REF-c871bf9abe41", "problem": "Node missing from the inventory file",
        "solution": "Apply the reflected approach hint.", "seen_count": 1, "last_seen": "2026-08-29",
    }])
    _write_live(state, [_row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z",
                             ("Run apt-get update before installing so the inventory is current.", "Node missing from the inventory file"))])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 1
    _apply(workspace, state)
    [card] = _lessons(workspace)
    assert card["solution"] == "Run apt-get update before installing so the inventory is current."
    assert card["seen_count"] == 2


# ─── cursor ──────────────────────────────────────────────────────────────────


def test_cursor_advances_and_rows_are_not_reprocessed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    rows = [_row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z", (DETAIL, "502s"))]
    _write_live(state, rows)
    promote_reflector_recommendations_to_v2(workspace, state)
    assert load_reflector_pool(state)["cursor"] == "2026-09-01T10:00:00Z"

    promote_reflector_recommendations_to_v2(workspace, state)
    pool = load_reflector_pool(state)
    assert pool["last_run"]["rows_read"] == 1 and pool["last_run"]["rows_processed"] == 0
    assert [c["cycles"] for c in pool["clusters"]] == [["cycle-aaaaaaaaaaa1"]]

    rows.append(_row("cycle-bbbbbbbbbbb2", "2026-09-02T10:00:00Z", (DETAIL, "502s again")))
    _write_live(state, rows)
    assert promote_reflector_recommendations_to_v2(workspace, state) == 1
    assert load_reflector_pool(state)["cursor"] == "2026-09-02T10:00:00Z"


def test_recurrence_across_an_archive_and_the_live_journal_is_seen(tmp_path: Path) -> None:
    """The 50-row window saw ~10 hours; an item outside it was never
    evaluated and after rotation never could be."""
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    _write_archive(state, "2026-09-01", [
        _row(f"cycle-{i:012d}", f"2026-08-31T{i % 24:02d}:00:00Z", (f"Unrelated one-off advice number {i} about a script.", f"evidence {i}"))
        for i in range(80)
    ] + [_row("cycle-aaaaaaaaaaa1", "2026-09-01T00:30:00Z", (DETAIL, "502s"))])
    _write_live(state, [_row("cycle-bbbbbbbbbbb2", "2026-09-02T10:00:00Z", (DETAIL_PARAPHRASE, "502s again"))])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 1
    [card] = _staged_cards(state)
    assert card["evidence"] == ["cycle-aaaaaaaaaaa1", "cycle-bbbbbbbbbbb2"]
    assert load_reflector_pool(state)["last_run"]["rows_read"] == 82


def test_rows_per_run_cap_carries_the_remainder_to_the_next_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kc, "_REFLECTOR_MAX_ROWS_PER_RUN", 1)
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    _write_live(state, [
        _row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z", (DETAIL, "502s")),
        _row("cycle-bbbbbbbbbbb2", "2026-09-02T10:00:00Z", (DETAIL, "502s again")),
    ])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 0
    pool = load_reflector_pool(state)
    assert pool["cursor"] == "2026-09-01T10:00:00Z"
    assert pool["last_run"]["rows_after_cursor"] == 2 and pool["last_run"]["rows_processed"] == 1

    assert promote_reflector_recommendations_to_v2(workspace, state) == 1
    assert load_reflector_pool(state)["cursor"] == "2026-09-02T10:00:00Z"


def test_new_card_cap_defers_graduation_and_the_next_run_finishes_it_without_new_rows(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    _write_live(state, [
        _row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z", (DETAIL, "502s"), (OTHER, "section without test")),
        _row("cycle-bbbbbbbbbbb2", "2026-09-02T10:00:00Z", (DETAIL, "502s again"), (OTHER, "another section")),
    ])

    assert promote_reflector_recommendations_to_v2(workspace, state, max_items=1) == 1
    pool = load_reflector_pool(state)
    assert pool["last_run"]["graduated"] == 1 and pool["last_run"]["deferred_by_cap"] == 1
    assert len(pool["clusters"]) == 1

    assert promote_reflector_recommendations_to_v2(workspace, state, max_items=1) == 1
    pool = load_reflector_pool(state)
    assert pool["last_run"]["rows_processed"] == 0 and pool["last_run"]["graduated"] == 1
    assert pool["clusters"] == []
    assert sorted(c["solution"] for c in _staged_cards(state)) == sorted([DETAIL, OTHER])


# ─── instrumentation ─────────────────────────────────────────────────────────


def test_near_misses_below_the_fold_threshold_are_counted(tmp_path: Path) -> None:
    """Jaccard 4/12 = 0.33 against the existing card: inside [0.25, 0.35)."""
    state = tmp_path / "state"
    existing = "alpha beta gamma delta epsilon zeta eta theta"
    workspace = _workspace(tmp_path, [_card("LESS-REF-000000000000-0000", existing, "an observation")])
    _write_live(state, [_row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z",
                             ("alpha beta gamma delta iota kappa lambda omicron", "another observation"))])

    assert promote_reflector_recommendations_to_v2(workspace, state) == 0
    pool = load_reflector_pool(state)
    assert pool["last_run"]["near_misses"] == 1 and pool["last_run"]["pooled_new"] == 1 and pool["last_run"]["folded"] == 0


def test_run_counts_distinguish_candidates_from_an_idle_run(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    _write_live(state, [_row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z", (DETAIL, "502s"))])
    promote_reflector_recommendations_to_v2(workspace, state)
    busy = load_reflector_pool(state)["last_run"]
    line = capsys.readouterr().out
    assert "curator-reflector: " in line and "items=1" in line and "staged=0" in line

    promote_reflector_recommendations_to_v2(workspace, state)
    idle = load_reflector_pool(state)["last_run"]
    assert (busy["items"], busy["staged"]) == (1, 0)
    assert (idle["items"], idle["staged"]) == (0, 0)


def test_pool_is_bounded_and_stale_clusters_are_evicted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kc, "_REFLECTOR_POOL_MAX", 2)
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    _write_live(state, [
        _row("cycle-000000000001", "2026-08-01T10:00:00Z", ("Archive the stale inventory snapshot before rotating the journal.", "e1")),
        _row("cycle-000000000002", "2026-09-01T10:00:00Z", ("Pin the pytest version so the collection phase stays deterministic.", "e2")),
        _row("cycle-000000000003", "2026-09-01T11:00:00Z", ("Validate YAML configuration keys against the schema at load time.", "e3")),
        _row("cycle-000000000004", "2026-09-01T12:00:00Z", ("Squash fixup commits locally before pushing the branch for review.", "e4")),
    ])

    promote_reflector_recommendations_to_v2(workspace, state)
    pool = load_reflector_pool(state)
    assert pool["last_run"]["pooled_new"] == 4
    assert pool["last_run"]["evicted"] == 2  # one past 14 days, one over the size bound
    assert [c["cycles"][0] for c in pool["clusters"]] == ["cycle-000000000004", "cycle-000000000003"]


def test_error_rows_unparseable_lines_and_other_kinds_are_skipped(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    path = _write_live(state, [
        {"cycle_id": "cycle-err", "timestamp": "2026-09-01T09:00:00Z", "status": "error", "recommendations": []},
        _row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z", (DETAIL, "502s")),
        _row("cycle-ccccccccccc3", "2026-09-01T10:30:00Z", ("Change the task instructions to name the target.", "x"), kind="instruction_change"),
        _row("cycle-bbbbbbbbbbb2", "2026-09-02T10:00:00Z", (DETAIL, "502s again")),
    ])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"cycle_id": "cycle-truncated", "recommendations": [\n')

    assert promote_reflector_recommendations_to_v2(workspace, state) == 1
    pool = load_reflector_pool(state)
    assert pool["last_run"]["unparseable"] == 1 and pool["last_run"]["items"] == 2


def test_corrupt_pool_sidecar_restarts_from_the_oldest_row(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = _workspace(tmp_path)
    (state / "curator").mkdir(parents=True)
    (state / "curator" / "reflector_pool.json").write_text("{not json", encoding="utf-8")
    _write_live(state, [
        _row("cycle-aaaaaaaaaaa1", "2026-09-01T10:00:00Z", (DETAIL, "502s")),
        _row("cycle-bbbbbbbbbbb2", "2026-09-02T10:00:00Z", (DETAIL, "502s again")),
    ])
    assert promote_reflector_recommendations_to_v2(workspace, state) == 1


# ─── pickup uses the same rule ───────────────────────────────────────────────


def test_pickup_folds_a_staged_card_into_a_card_that_arrived_meanwhile(tmp_path: Path) -> None:
    """The checkout may have gained a card since staging; the pickup applies
    the same fold rule as the curator instead of inserting a near-duplicate."""
    workspace = _workspace(tmp_path, [_card("LESS-REF-000000000000-0000", DETAIL, "502s before", evidence=["cycle-000000000000"])])
    staged = _card("LESS-REF-aaaaaaaaaaa1-1234", DETAIL_PARAPHRASE, "502s again", seen=2,
                   evidence=["cycle-aaaaaaaaaaa1", "cycle-bbbbbbbbbbb2"])
    staged["distinct_days"] = 2

    assert apply_staged_lesson_cards(workspace, {"cards": [staged]}) == ["LESS-REF-aaaaaaaaaaa1-1234"]
    [card] = _lessons(workspace)
    assert card["id"] == "LESS-REF-000000000000-0000"
    assert card["seen_count"] == 3
    assert card["evidence"] == ["cycle-000000000000", "cycle-aaaaaaaaaaa1", "cycle-bbbbbbbbbbb2"]
    assert card["distinct_days"] == 2


@pytest.mark.parametrize("threshold", [kc._REFLECTOR_FOLD_THRESHOLD])
def test_fold_threshold_is_a_named_constant_with_its_calibration_on_record(threshold: float) -> None:
    assert threshold == 0.35
    source = Path(kc.__file__).read_text(encoding="utf-8")
    assert "calibrated on ONE week of ONE loop" in source
