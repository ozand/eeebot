"""#1403: a curator `duplicate` decision is verified against the cited body.

Before this change a duplicate decision could not reach an artifact body by
construction: the first pass is called with an empty facts argument, the
duplicate schema had no path field, and ``_touched_facts`` only loads what a
create/update names. Every duplicate was decided against ``memory/index.md`` —
one line per artifact — and ``target_file`` was empty on all 189 recorded rows.

Fixtures under ``tests/fixtures/curator_1403/`` are the real rows recovered for
the #1344 audit: two candidates from the host's ``lessons/errors.yaml``, one
reflector recommendation, and the three artifacts they were judged against read
from ``ozand/eeebot-self-evolving`` at ``origin/main`` (64bce672). The two facts
are 88 and 89 bytes, byte-for-byte as deployed. Where a shape is needed that the
corpus does not contain (an oversized artifact, an undecodable one, a citation
that resolves nowhere) it is constructed in the test and named as such.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.runtime.knowledge_curator import (
    DUPLICATE_SUPPORT_THRESHOLD,
    _cited_bodies,
    duplicate_source_path,
    duplicate_support,
    read_cited_body,
    run_curation,
    verify_duplicate_claim,
)

FIXTURES = Path(__file__).parent / "fixtures" / "curator_1403"
RELEASE_FACT = "memory/facts/release-promotion-metadata.md"
PERMISSIONS_FACT = "memory/facts/git-database-permissions.md"
AGENTS_FACT = "memory/facts/agents-instruction-assertions.md"


def _candidates() -> dict[str, dict]:
    return json.loads((FIXTURES / "candidates.json").read_text(encoding="utf-8"))


def _workspace(tmp_path: Path, facts: list[str]) -> Path:
    """A workspace carrying the named fixture artifacts at their real paths."""
    for rel in facts:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((FIXTURES / rel).read_bytes())
    return tmp_path


def _claim(path: str, reason: str = "already documented") -> dict:
    return {"action": "duplicate", "lesson_id": "L1", "duplicate_path": path, "reason": reason}


# ─── the two facts really are one sentence ───────────────────────────────────


def test_fixture_facts_are_the_deployed_bytes():
    """The calibration rests on these being tiny; pin the sizes."""
    assert (FIXTURES / RELEASE_FACT).stat().st_size == 88
    assert (FIXTURES / PERMISSIONS_FACT).stat().st_size == 89
    assert (FIXTURES / AGENTS_FACT).stat().st_size == 321


# ─── AC2: the two ERR cards are not duplicates of the one-sentence facts ─────


@pytest.mark.parametrize(
    "lesson_id,fact,reason",
    [
        ("ERR-2026-06-14-001", RELEASE_FACT,
         "Already documented in memory/facts/release-promotion-metadata.md."),
        ("ERR-2026-06-15-004", PERMISSIONS_FACT,
         "Workspace permission pattern is addressed in the git database permissions fact."),
    ],
)
def test_one_sentence_fact_does_not_cover_the_incident_card(tmp_path, lesson_id, fact, reason):
    """#1344 audit rows: same topic, none of the operational content.

    The release card carries the failure signature (blocked_not_ready), the
    recovery procedure and a prevention rule; the fact states the requirement
    and stops. Recorded as unimportant so the discard is visible, never as a
    verified duplicate.
    """
    workspace = _workspace(tmp_path, [fact])
    entry = _candidates()[lesson_id]

    decision, recorded_reason, target = verify_duplicate_claim(
        workspace, _claim(fact, reason), entry
    )

    assert decision == "unimportant"
    assert target == ""
    assert reason in recorded_reason, "the model's own reason must survive"
    assert "unsupported duplicate claim" in recorded_reason


def test_reverse_containment_would_have_confirmed_the_wrong_answer(tmp_path):
    """Direction is the whole defect: the 88-byte fact is almost entirely
    contained in the card it discarded. Measured the other way the claim looks
    overwhelming; measured as coverage of the candidate it collapses."""
    from nanobot.runtime.lesson_v2 import keyword_set

    workspace = _workspace(tmp_path, [RELEASE_FACT])
    entry = _candidates()["ERR-2026-06-14-001"]
    body = read_cited_body(workspace, Path(RELEASE_FACT))
    assert body is not None

    candidate = keyword_set(" ".join(str(v) for v in entry.values() if isinstance(v, str)))
    artifact = keyword_set(body)
    shared = len(candidate & artifact)

    assert shared / len(artifact) > 0.8, "artifact is nearly contained in the candidate"
    assert duplicate_support(entry, body) < DUPLICATE_SUPPORT_THRESHOLD


# ─── AC3: a genuine duplicate still stands, with the path recorded ───────────


def test_genuine_duplicate_stands_and_records_the_cited_path(tmp_path):
    """REFL-5b349fbfddd0-0 vs agents-instruction-assertions.md, which states the
    candidate's rule ('register the new heading in REQUIRED_SECTIONS in
    tests/test_agents_structure.py') almost verbatim."""
    workspace = _workspace(tmp_path, [AGENTS_FACT])
    entry = _candidates()["REFL-5b349fbfddd0-0"]

    decision, reason, target = verify_duplicate_claim(
        workspace,
        _claim(AGENTS_FACT, "Registering AGENTS.md sections in REQUIRED_SECTIONS is covered."),
        entry,
    )

    assert decision == "duplicate"
    assert target == AGENTS_FACT
    assert reason == "Registering AGENTS.md sections in REQUIRED_SECTIONS is covered."
    assert duplicate_support(entry, (FIXTURES / AGENTS_FACT).read_text(encoding="utf-8")) >= \
        DUPLICATE_SUPPORT_THRESHOLD


def test_calibration_band_holds_for_all_three_fixture_pairs(tmp_path):
    """The threshold sits in the empty band between the audited outcomes."""
    workspace = _workspace(tmp_path, [RELEASE_FACT, PERMISSIONS_FACT, AGENTS_FACT])
    cands = _candidates()
    support = {
        ("ERR-2026-06-14-001", RELEASE_FACT): None,
        ("ERR-2026-06-15-004", PERMISSIONS_FACT): None,
        ("REFL-5b349fbfddd0-0", AGENTS_FACT): None,
    }
    for (lesson_id, fact) in list(support):
        body = read_cited_body(workspace, Path(fact))
        assert body is not None
        support[(lesson_id, fact)] = duplicate_support(cands[lesson_id], body)

    false_claims = [v for k, v in support.items() if k[0].startswith("ERR-")]
    genuine = support[("REFL-5b349fbfddd0-0", AGENTS_FACT)]
    assert max(false_claims) < DUPLICATE_SUPPORT_THRESHOLD <= genuine


# ─── AC4: an unresolvable citation is recorded as unimportant ────────────────


def test_citation_that_resolves_nowhere_is_unimportant(tmp_path):
    """'Duplicate of lesson card integrity schema fact' — the #1344 audit row
    whose cited fact does not exist anywhere in the corpus. Constructed here as
    a path that resolves nowhere, since the corpus cannot contain it."""
    workspace = _workspace(tmp_path, [AGENTS_FACT])
    entry = _candidates()["REFL-5b349fbfddd0-0"]

    decision, reason, target = verify_duplicate_claim(
        workspace,
        _claim("memory/facts/lesson-card-integrity-schema.md", "Duplicate of lesson card schema fact."),
        entry,
    )

    assert decision == "unimportant"
    assert target == ""
    assert "unreadable" in reason and "Duplicate of lesson card schema fact." in reason


def test_duplicate_claim_with_no_path_at_all_is_unimportant(tmp_path):
    """The pre-#1403 shape: a duplicate decision carrying no citation."""
    workspace = _workspace(tmp_path, [AGENTS_FACT])
    entry = _candidates()["REFL-5b349fbfddd0-0"]

    decision, reason, target = verify_duplicate_claim(
        workspace, {"action": "duplicate", "lesson_id": "L1", "reason": "already covered"}, entry
    )

    assert decision == "unimportant"
    assert target == ""
    assert "no cited artifact" in reason


@pytest.mark.parametrize("path", [
    "../../../etc/passwd.md",
    "memory/../../escape.md",
    "/etc/hosts.md",
    "memory/facts/note.txt",
    "notes/facts/thing.md",
    "memory/a/b/c/deep.md",
    "",
])
def test_uncitable_paths_are_refused(path):
    assert duplicate_source_path(path) is None


def test_citable_roots_cover_what_the_indexes_list():
    """memory/index.md lists memory/*.md discipline documents beside
    memory/facts/*.md; both must be citable or genuine duplicates break."""
    assert duplicate_source_path("memory/facts/agents-instruction-assertions.md") is not None
    assert duplicate_source_path("memory/turn_budget_pacing.md") is not None
    assert duplicate_source_path("lessons/avoiding_repeat_failures.md") is not None
    assert duplicate_source_path("docs/facts/thing.md") is not None


# ─── AC5: fail-open — a read that really fails degrades to not-a-duplicate ───


def test_missing_artifact_degrades_to_not_a_duplicate(tmp_path):
    entry = _candidates()["REFL-5b349fbfddd0-0"]
    decision, _reason, target = verify_duplicate_claim(tmp_path, _claim(AGENTS_FACT), entry)
    assert (decision, target) == ("unimportant", "")


def test_oversized_artifact_degrades_to_not_a_duplicate(tmp_path):
    """A real 129 KB file past the read cap, not a patched reader."""
    dest = tmp_path / AGENTS_FACT
    dest.parent.mkdir(parents=True)
    dest.write_text("register REQUIRED_SECTIONS tests\n" * 4200, encoding="utf-8")
    assert dest.stat().st_size > 128 * 1024

    entry = _candidates()["REFL-5b349fbfddd0-0"]
    assert read_cited_body(tmp_path, Path(AGENTS_FACT)) is None
    assert verify_duplicate_claim(tmp_path, _claim(AGENTS_FACT), entry)[0] == "unimportant"


def test_undecodable_artifact_degrades_to_not_a_duplicate(tmp_path):
    """Real invalid UTF-8 on disk: read_text raises UnicodeDecodeError."""
    dest = tmp_path / AGENTS_FACT
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"# fact\n\xff\xfe\x00 register REQUIRED_SECTIONS\n")
    with pytest.raises(UnicodeError):
        dest.read_text(encoding="utf-8")

    entry = _candidates()["REFL-5b349fbfddd0-0"]
    assert read_cited_body(tmp_path, Path(AGENTS_FACT)) is None
    assert verify_duplicate_claim(tmp_path, _claim(AGENTS_FACT), entry)[0] == "unimportant"


def test_directory_in_place_of_artifact_degrades_to_not_a_duplicate(tmp_path):
    """A real directory at the cited path — open() fails, and it must not raise."""
    (tmp_path / AGENTS_FACT).mkdir(parents=True)
    entry = _candidates()["REFL-5b349fbfddd0-0"]
    assert read_cited_body(tmp_path, Path(AGENTS_FACT)) is None
    assert verify_duplicate_claim(tmp_path, _claim(AGENTS_FACT), entry)[0] == "unimportant"


def test_fail_open_never_raises_and_never_rejects(tmp_path):
    """The direction that can hurt: a failed read must never produce a
    rejection, only a claim that does not stand."""
    entry = _candidates()["ERR-2026-06-14-001"]
    for path in (AGENTS_FACT, "memory/facts/gone.md", "memory/nope.md"):
        decision, _reason, target = verify_duplicate_claim(tmp_path, _claim(path), entry)
        assert decision == "unimportant"
        assert decision != "rejected"
        assert target == ""


def test_empty_candidate_does_not_divide_by_zero(tmp_path):
    workspace = _workspace(tmp_path, [AGENTS_FACT])
    body = read_cited_body(workspace, Path(AGENTS_FACT))
    assert duplicate_support({}, body or "") == 0.0
    assert duplicate_support(None, body or "") == 0.0
    assert duplicate_support(_candidates()["REFL-5b349fbfddd0-0"], "") == 0.0


# ─── the plumbing half: the cited body reaches the second pass ───────────────


def test_duplicate_citation_body_is_loaded_for_the_second_pass(tmp_path):
    """``_touched_facts`` alone returns nothing here — no create/update names a
    path — which is exactly why the body never reached the model."""
    from nanobot.runtime.knowledge_curator import _touched_facts

    workspace = _workspace(tmp_path, [RELEASE_FACT])
    decisions = [_claim(RELEASE_FACT)]

    assert _touched_facts(workspace, decisions) == ""
    bodies = _cited_bodies(workspace, decisions)
    assert "SOURCE_COMMIT metadata is required for release promotion." in bodies
    assert RELEASE_FACT in bodies


def test_cited_bodies_deduplicates_and_skips_unreadable(tmp_path):
    workspace = _workspace(tmp_path, [RELEASE_FACT])
    decisions = [_claim(RELEASE_FACT), _claim(RELEASE_FACT), _claim("memory/facts/gone.md")]
    bodies = _cited_bodies(workspace, decisions)
    assert bodies.count("SOURCE_COMMIT metadata") == 1
    assert "gone.md" not in bodies


def test_schema_tells_the_model_to_name_the_artifact():
    from nanobot.runtime.knowledge_curator import _messages

    system = _messages([{"id": "L1"}], "index", "")[0]["content"]
    assert "duplicate_path" in system
    assert "never duplicate" in system


# ─── AC1 end to end: no duplicate row without a resolvable target_file ───────


def _decisions(state: Path) -> list[dict]:
    path = state / "curator" / "decisions.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _seed_journal(root: Path, entries: dict[str, dict]) -> None:
    import yaml

    (root / "lessons").mkdir(parents=True, exist_ok=True)
    (root / "lessons" / "lessons.yaml").write_text(
        yaml.safe_dump([dict(v, id=k) for k, v in entries.items()], sort_keys=False),
        encoding="utf-8",
    )


def test_no_duplicate_row_is_written_without_a_resolvable_target(tmp_path):
    """One run carrying all four claim shapes at once."""
    cands = _candidates()
    workspace = _workspace(tmp_path, [RELEASE_FACT, AGENTS_FACT])
    _seed_journal(workspace, cands)
    state = tmp_path / "state"

    def llm(messages, model):
        assert model
        return json.dumps([
            {"action": "duplicate", "lesson_id": "REFL-5b349fbfddd0-0",
             "duplicate_path": AGENTS_FACT, "reason": "covered"},
            {"action": "duplicate", "lesson_id": "ERR-2026-06-14-001",
             "duplicate_path": RELEASE_FACT, "reason": "already documented"},
            {"action": "duplicate", "lesson_id": "ERR-2026-06-15-004",
             "duplicate_path": "memory/facts/nowhere.md", "reason": "invented citation"},
        ])

    result = run_curation(workspace, state, llm=llm)
    assert result["ok"]

    rows = _decisions(state)
    duplicates = [r for r in rows if r["decision"] == "duplicate"]
    assert duplicates, "the genuine duplicate must still be recorded as one"
    for row in duplicates:
        assert row["target_file"], f"duplicate row without a citation: {row}"
        assert (workspace / row["target_file"]).is_file(), \
            f"duplicate row cites an artifact that does not resolve: {row}"

    by_id = {r["lesson_id"]: r for r in rows}
    assert by_id["REFL-5b349fbfddd0-0"]["decision"] == "duplicate"
    assert by_id["REFL-5b349fbfddd0-0"]["target_file"] == AGENTS_FACT
    assert by_id["ERR-2026-06-14-001"]["decision"] == "unimportant"
    assert by_id["ERR-2026-06-15-004"]["decision"] == "unimportant"


def test_existing_decision_rows_are_not_rewritten(tmp_path):
    """target_file is filled going forward only; the 189 recorded rows stay as
    they are. The log is append-only."""
    cands = _candidates()
    workspace = _workspace(tmp_path, [AGENTS_FACT])
    _seed_journal(workspace, cands)
    state = tmp_path / "state"
    decisions_path = state / "curator" / "decisions.jsonl"
    decisions_path.parent.mkdir(parents=True)
    historical = [
        {"timestamp": "2026-08-27T07:27:42.302125Z", "lesson_id": "ERR-2026-06-14-001",
         "decision": "duplicate", "reason": "Already documented in "
         "memory/facts/release-promotion-metadata.md.", "target_file": ""},
    ]
    decisions_path.write_text(json.dumps(historical[0]) + "\n", encoding="utf-8")

    def llm(messages, model):
        return json.dumps([
            {"action": "duplicate", "lesson_id": "REFL-5b349fbfddd0-0",
             "duplicate_path": AGENTS_FACT, "reason": "covered"},
        ])

    run_curation(workspace, state, llm=llm)

    rows = _decisions(state)
    assert rows[0] == historical[0], "historical rows must not be rewritten or backfilled"
    assert len(rows) > 1


def test_claim_for_a_candidate_absent_from_the_batch_is_unverified(tmp_path):
    """A citation whose lesson this batch does not carry cannot be checked
    against anything; that is unverifiable, not confirmed."""
    workspace = _workspace(tmp_path, [AGENTS_FACT])
    decision, reason, target = verify_duplicate_claim(workspace, _claim(AGENTS_FACT), None)
    assert (decision, target) == ("unimportant", "")
    assert "candidate not in batch" in reason
