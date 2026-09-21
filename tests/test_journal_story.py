from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from scripts.journal_story import (
    LEDGER_SOURCE,
    StoryValidationError,
    assemble_story_artifact,
    build_story_prompt,
    load_day_journal,
    run_narrator_job,
    select_beats,
    validate_narration,
)


def _ledger_row(ts: str, cycle: str, outcome: str = "success") -> str:
    return json.dumps({"phase": "outcome", "cycle_id": cycle, "outcome": outcome, "task_title": cycle, "ts": ts})


def _write_ledger(state_dir, archive_lines: list[str], live_lines: list[str], *, archive_day: str = "2026-09-15"):
    ledger = state_dir / "ledger"
    ledger.mkdir(parents=True)
    with gzip.open(ledger / f"cycles-{archive_day}.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write("\n".join(archive_lines) + "\n")
    (ledger / "cycles.jsonl").write_text("\n".join(live_lines) + "\n", encoding="utf-8")
    return ledger


def test_day_spanning_rotation_boundary_counts_the_day_not_the_live_file(tmp_path):
    _write_ledger(
        tmp_path,
        [_ledger_row("2026-09-15T08:00:00Z", "cycle-a"), _ledger_row("2026-09-15T20:00:00Z", "cycle-b", "failed")],
        [_ledger_row("2026-09-15T23:59:40Z", "cycle-c"), _ledger_row("2026-09-16T00:10:00Z", "cycle-d")],
    )
    journal = load_day_journal(tmp_path, "2026-09-15")
    assert journal["source"] == LEDGER_SOURCE
    assert journal["status"] == "complete"
    assert journal["files_read"] == ["cycles-2026-09-15.jsonl.gz", "cycles.jsonl"]
    beats = select_beats(journal["rows"], day="2026-09-15")
    assert [beat["source"]["cycle_id"] for beat in beats] == ["cycle-a", "cycle-b", "cycle-c"]
    assert beats[0]["source"] == {"file": "cycles-2026-09-15.jsonl.gz", "line": 1, "cycle_id": "cycle-a", "phase": "outcome"}
    assert beats[2]["source"]["file"] == "cycles.jsonl"
    # The live file alone holds one row of this day; the day has three.
    assert sum(1 for row in journal["rows"] if row["_source_file"] == "cycles.jsonl") == 1


def test_partly_unreadable_day_is_marked_incomplete_not_thin(tmp_path):
    _write_ledger(
        tmp_path,
        [_ledger_row("2026-09-15T08:00:00Z", "cycle-a"), "{not json", _ledger_row("2026-09-15T09:00:00Z", "cycle-b")],
        [],
    )
    journal = load_day_journal(tmp_path, "2026-09-15")
    assert journal["status"] == "incomplete"
    assert journal["unreadable"] == [{"file": "cycles-2026-09-15.jsonl.gz", "line": 2, "reason": "malformed_json"}]
    assert len(select_beats(journal["rows"])) == 2
    thin = load_day_journal(tmp_path, "2026-09-13")
    assert thin["status"] == "complete"
    assert select_beats(thin["rows"]) == []


def test_corrupt_archive_is_reported_not_skipped_silently(tmp_path):
    ledger = _write_ledger(tmp_path, [_ledger_row("2026-09-15T08:00:00Z", "cycle-a")], [])
    (ledger / "cycles-2026-09-14.jsonl.gz").write_bytes(b"\x1f\x8b\x08\x00garbage")
    journal = load_day_journal(tmp_path, "2026-09-15")
    assert journal["status"] == "incomplete"
    assert journal["notes"] == ["unreadable_file:cycles-2026-09-14.jsonl.gz"]
    assert len(journal["rows"]) == 1
    assert load_day_journal(tmp_path / "nowhere", "2026-09-15")["notes"] == ["ledger_dir_missing"]


def test_a_cross_midnight_cycles_title_reaches_its_outcome_beat_1841(tmp_path):
    """Reproduces the real 2026-09-20 production case (cycle-e953d1bce4fc,
    fetched from state/ledger on eeepc): its `proposed` row lands
    2026-09-19T23:10:20Z, its `outcome` row (no task_title of its own)
    lands 2026-09-20T00:05:07Z. Before #1841, load_day_journan's own day
    filter dropped the `proposed` row before select_beats ever saw it, so
    the recorded state/story/2026-09-20.json artifact's beat-001 fell back
    to the bare "outcome event" title.
    """
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    proposed = json.dumps({
        "phase": "proposed", "cycle_id": "cycle-e953d1bce4fc",
        "task_title": "Add collect_incident_errors to scripts/search_subagent_archive.py "
                      "to gather error-status result records",
        "ts": "2026-09-19T23:10:20.391471Z",
    })
    with gzip.open(ledger / "cycles-2026-09-19.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(proposed + "\n")
    outcome = json.dumps({
        "phase": "outcome", "cycle_id": "cycle-e953d1bce4fc", "outcome": "success",
        "ts": "2026-09-20T00:05:07.210007Z",
    })
    with gzip.open(ledger / "cycles-2026-09-20.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(outcome + "\n")
    (ledger / "cycles.jsonl").write_text("", encoding="utf-8")

    journal = load_day_journal(tmp_path, "2026-09-20")
    beats = select_beats(journal["rows"], day="2026-09-20")
    assert len(beats) == 1
    assert beats[0]["event"] == (
        "Add collect_incident_errors to scripts/search_subagent_archive.py "
        "to gather error-status result records"
    )
    assert beats[0]["event"] != "outcome event"  # the pre-fix fallback


def test_a_neighbouring_days_own_event_still_does_not_enter_the_beats_1841(tmp_path):
    """The day filter for BEATS (events) must stay strict -- only the
    `proposed`-row title exemption changed. An outcome row genuinely
    stamped the day before must not surface as one of today's beats, even
    though the file window reads yesterday's archive too."""
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    yesterday_outcome = json.dumps({
        "phase": "outcome", "cycle_id": "cycle-yesterday", "outcome": "success",
        "task_title": "Yesterday's own change", "ts": "2026-09-19T23:50:00Z",
    })
    with gzip.open(ledger / "cycles-2026-09-19.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(yesterday_outcome + "\n")
    (ledger / "cycles.jsonl").write_text("", encoding="utf-8")

    journal = load_day_journal(tmp_path, "2026-09-20")
    assert not any(row.get("cycle_id") == "cycle-yesterday" for row in journal["rows"])
    beats = select_beats(journal["rows"], day="2026-09-20")
    assert beats == []


def _row(line: int, *, cycle: str = "cycle-a", outcome: str = "success", title: str = "Ship change"):
    return {
        "phase": "outcome", "cycle_id": cycle, "outcome": outcome,
        "task_title": title, "ts": "2026-09-15T12:00:00Z",
        "_source_file": "cycles.jsonl", "_source_line": line,
    }


def test_selection_is_deterministic_and_carries_sources_and_sign():
    rows = [_row(2, cycle="cycle-b", outcome="failed"), _row(1, cycle="cycle-a")]
    beats = select_beats(rows)
    assert [beat["beat_id"] for beat in beats] == ["beat-001", "beat-002"]
    assert [beat["sign"] for beat in beats] == ["worked", "failed"]
    assert beats[0]["source"] == {"file": "cycles.jsonl", "line": 1, "cycle_id": "cycle-a", "phase": "outcome"}


def test_retried_cycle_takes_sign_from_its_last_outcome_row_and_title_from_proposed():
    rows = [
        {"phase": "proposed", "cycle_id": "cycle-a", "task_title": "Add palette lookup", "ts": "2026-09-15T11:00:00Z",
         "_source_file": "cycles.jsonl", "_source_line": 1},
        {**_row(2, outcome="success", title=""), "ts": "2026-09-15T12:00:00Z"},
        {**_row(3, outcome="failed", title=""), "ts": "2026-09-15T12:30:00Z"},
    ]
    beats = select_beats(rows)
    assert len(beats) == 1
    assert beats[0]["sign"] == "failed"
    assert beats[0]["source"]["line"] == 3
    assert beats[0]["event"] == "Add palette lookup"


def test_one_event_and_quiet_day_do_not_get_padded():
    assert len(select_beats([_row(1)])) == 1
    assert select_beats([]) == []


def test_prompt_contains_only_selected_beats_and_fixed_sign_instruction():
    beats = select_beats([_row(1)])
    prompt = build_story_prompt(beats, {"ledger": "A journal record of what happened."})
    payload = json.loads(prompt)
    assert payload["beats"] == beats
    assert "preserve it exactly" in payload["sign_rule"]


def test_wrong_sign_is_rejected_even_when_prose_is_confident():
    beats = select_beats([_row(1, outcome="failed")])
    with pytest.raises(StoryValidationError, match="sign drift"):
        validate_narration([{"beat_id": "beat-001", "text": "I finally fixed it.", "sign": "worked", "terms": []}], beats)


def test_failed_beat_rejects_positive_prose_and_unknown_is_explicit():
    failed = select_beats([_row(1, outcome="failed")])
    with pytest.raises(StoryValidationError, match="positive wording"):
        validate_narration([{"beat_id": "beat-001", "text": "It succeeded.", "sign": "failed", "terms": []}], failed)
    unknown = select_beats([_row(1, outcome="partial")])
    with pytest.raises(StoryValidationError, match="unknown"):
        validate_narration([{"beat_id": "beat-001", "text": "I finally fixed it.", "sign": "unknown", "terms": []}], unknown)


def test_beats_own_vocabulary_is_echoed_not_invented():
    """#1840: a word the beat's own event text already carries is the
    SUBJECT of the change, not an invented tone. A faithful narration that
    reuses that word must pass; a narration that invents a DIFFERENT
    forbidden word absent from the beat must still be rejected -- proven
    for all three word lists, both directions."""
    # _NEGATIVE_WORDS on a "worked" beat: the real 2026-09-20 beat-007
    # shape (see test_narrator_job_2026_09_20_replay_reaches_ok below for
    # the full-day replay against the recorded artifact).
    worked = select_beats([_row(1, outcome="success", title="Add failure search guidance in AGENTS.md")])
    validate_narration(
        [{"beat_id": "beat-001", "text": "Updated AGENTS.md to point failure searches toward the subagent archive.", "sign": "worked", "terms": []}],
        worked,
    )
    with pytest.raises(StoryValidationError, match="negative wording"):
        validate_narration(
            [{"beat_id": "beat-001", "text": "The AGENTS.md update was broken on arrival.", "sign": "worked", "terms": []}],
            worked,
        )

    # _POSITIVE_WORDS on a "failed" beat: "success" is the subject of the
    # investigated metric, not a claim that the investigation succeeded.
    failed = select_beats([_row(1, outcome="failed", title="Investigate the success metric regression")])
    validate_narration(
        [{"beat_id": "beat-001", "text": "Investigated the success metric regression; the fix did not land.", "sign": "failed", "terms": []}],
        failed,
    )
    with pytest.raises(StoryValidationError, match="positive wording"):
        validate_narration(
            [{"beat_id": "beat-001", "text": "The regression investigation improved matters.", "sign": "failed", "terms": []}],
            failed,
        )

    # _CONFIDENT_UNKNOWN_WORDS on an "unknown" beat: "fixed" names the
    # threshold under investigation, not a confident verdict on it.
    unknown = select_beats([_row(1, outcome="partial", title="Investigate the fixed threshold behaviour")])
    validate_narration(
        [{"beat_id": "beat-001", "text": "Investigated the fixed threshold behaviour; the outcome is unclear.", "sign": "unknown", "terms": []}],
        unknown,
    )
    with pytest.raises(StoryValidationError, match="confident wording"):
        validate_narration(
            [{"beat_id": "beat-001", "text": "The threshold behaviour is definitely settled now.", "sign": "unknown", "terms": []}],
            unknown,
        )


def test_invented_beat_and_undefined_term_are_rejected():
    beats = select_beats([_row(1)])
    with pytest.raises(StoryValidationError, match="no selected beat"):
        validate_narration([{"beat_id": "beat-999", "text": "New event.", "sign": "worked", "terms": []}], beats)
    with pytest.raises(StoryValidationError, match="undefined glossary"):
        validate_narration([{"beat_id": "beat-001", "text": "It worked.", "sign": "worked", "terms": ["unknown"]}], beats, glossary={"ledger": "..."})


def test_artifact_carries_prompt_model_output_and_citation_set():
    beats = select_beats([_row(3)])
    artifact = assemble_story_artifact(
        beats,
        [{"beat_id": "beat-001", "text": "The change worked.", "sign": "worked", "terms": []}],
        prompt="prompt", model_output={"raw": "output"},
    )
    assert artifact["schema_version"] == "journal-story-v1"
    assert artifact["citation_set"]["beat-001"]["line"] == 3


# ─── increment 2: run_narrator_job ──────────────────────────────────────────


def _compliant_llm(messages, model):
    prompt = json.loads(messages[1]["content"])
    out = [
        {"beat_id": b["beat_id"], "text": f"Something happened: {b['event']}.", "sign": b["sign"], "terms": []}
        for b in prompt["beats"]
    ]
    return json.dumps(out)


def _sign_flipping_llm(messages, model):
    prompt = json.loads(messages[1]["content"])
    out = [{"beat_id": b["beat_id"], "text": "it worked great", "sign": "worked", "terms": []} for b in prompt["beats"]]
    return json.dumps(out)


def _raising_llm(messages, model):
    raise RuntimeError("gateway unreachable")


def test_prompt_contains_only_the_selected_days_beats(tmp_path):
    _write_ledger(
        tmp_path,
        [_ledger_row("2026-09-15T08:00:00Z", "cycle-a", "success")],
        [_ledger_row("2026-09-16T09:00:00Z", "cycle-b", "failed")],
    )
    captured: dict = {}

    def _capturing_llm(messages, model):
        captured["prompt"] = json.loads(messages[1]["content"])
        return json.dumps([
            {"beat_id": b["beat_id"], "text": "x", "sign": b["sign"], "terms": []}
            for b in captured["prompt"]["beats"]
        ])

    result = run_narrator_job(tmp_path, "2026-09-15", llm=_capturing_llm)
    assert result["status"] == "ok"
    event_names = {b["event"] for b in captured["prompt"]["beats"]}
    assert event_names == {"cycle-a"}  # never the 09-16 row


def test_sign_flipping_model_is_rejected_with_the_violation_named(tmp_path):
    _write_ledger(tmp_path, [_ledger_row("2026-09-15T08:00:00Z", "cycle-a", "failed")], [])
    result = run_narrator_job(tmp_path, "2026-09-15", llm=_sign_flipping_llm)
    assert result["status"] == "rejected"
    assert result["violations"] and "sign drift" in result["violations"][0]
    on_disk = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
    assert on_disk["status"] == "rejected"
    assert on_disk["violations"] == result["violations"]


def test_compliant_model_is_ok_with_a_citation_map_resolving_real_rows(tmp_path):
    _write_ledger(
        tmp_path,
        [_ledger_row("2026-09-15T08:00:00Z", "cycle-a", "success"),
         _ledger_row("2026-09-15T09:00:00Z", "cycle-b", "failed")],
        [],
    )
    result = run_narrator_job(tmp_path, "2026-09-15", llm=_compliant_llm)
    assert result["status"] == "ok"
    assert len(result["narration"]) == 2
    for beat_id, source in result["citation_set"].items():
        assert source["file"] == "cycles-2026-09-15.jsonl.gz"
        assert isinstance(source["line"], int)


def test_writer_never_raises_on_a_gateway_failure(tmp_path):
    """#1842: the job could not run at all, so this is ``error`` -- not
    ``rejected``, which means the content gate fired on a completed run."""
    _write_ledger(tmp_path, [_ledger_row("2026-09-15T08:00:00Z", "cycle-a", "success")], [])
    result = run_narrator_job(tmp_path, "2026-09-15", llm=_raising_llm)  # must not raise
    assert result["status"] == "error"
    assert "RuntimeError" in result["violations"][0]
    assert Path(result["artifact_path"]).is_file()


def test_writer_never_raises_on_malformed_model_output(tmp_path):
    _write_ledger(tmp_path, [_ledger_row("2026-09-15T08:00:00Z", "cycle-a", "success")], [])
    result = run_narrator_job(tmp_path, "2026-09-15", llm=lambda messages, model: "not json at all")
    assert result["status"] == "rejected"
    assert Path(result["artifact_path"]).is_file()


# The real recorded day (2026-09-20) that first exposed the #1840 defect --
# beats and model_output copied verbatim from state/story/2026-09-20.json
# on the eeepc host (artifact generated_at 2026-09-21T00:30:01.202762Z,
# status: rejected, violations: ["negative wording contradicts worked
# beat beat-007"]). A replay of a recorded day, not a manufactured event
# -- no second model call, per #1840's own acceptance criteria.
_RECORDED_2026_09_20_BEATS = [
    {"beat_id": "beat-001", "event": "outcome event", "sign": "worked", "cost": None,
     "source": {"file": "cycles-2026-09-20.jsonl.gz", "line": 3, "cycle_id": "cycle-e953d1bce4fc", "phase": "outcome"}},
    {"beat_id": "beat-002", "event": "Add select_test_runner to scripts/verify_and_proof.py to choose runner based on test target", "sign": "worked", "cost": None,
     "source": {"file": "cycles-2026-09-20.jsonl.gz", "line": 24, "cycle_id": "cycle-802327dfcda5", "phase": "outcome"}},
    {"beat_id": "beat-003", "event": "Add prune_zero_consumer_keys to scripts/format_existence_index_exclusions.py to filter zero-consumer keys", "sign": "worked", "cost": None,
     "source": {"file": "cycles-2026-09-20.jsonl.gz", "line": 39, "cycle_id": "cycle-5b82bdfb77d7", "phase": "outcome"}},
    {"beat_id": "beat-004", "event": "Add count_consecutive_matching_errors to scripts/check_repeat_failures.py to tally trailing matching error messages", "sign": "worked", "cost": None,
     "source": {"file": "cycles-2026-09-20.jsonl.gz", "line": 54, "cycle_id": "cycle-13dee0060d95", "phase": "outcome"}},
    {"beat_id": "beat-005", "event": "Add extract_incident_id to scripts/search_subagent_archive.py to parse incident IDs from defect strings", "sign": "unknown", "cost": None,
     "source": {"file": "cycles-2026-09-20.jsonl.gz", "line": 67, "cycle_id": "cycle-c9aff50b1457", "phase": "outcome"}},
    {"beat_id": "beat-006", "event": "Exclude index.md from lesson schema assertions in test_lessons_integrity.py", "sign": "worked", "cost": None,
     "source": {"file": "cycles-2026-09-20.jsonl.gz", "line": 95, "cycle_id": "cycle-067815bdbb03", "phase": "outcome"}},
    {"beat_id": "beat-007", "event": "Add failure search guidance in AGENTS.md to direct queries to subagent archive instead of ledger", "sign": "worked", "cost": None,
     "source": {"file": "cycles-2026-09-20.jsonl.gz", "line": 111, "cycle_id": "cycle-37717160a42e", "phase": "outcome"}},
    {"beat_id": "beat-008", "event": "Add steering rule in AGENTS.md to emit strictly raw JSON without conversational preambles in final turns", "sign": "unknown", "cost": None,
     "source": {"file": "cycles-2026-09-20.jsonl.gz", "line": 131, "cycle_id": "cycle-b500a5fab8fe", "phase": "outcome"}},
]

_RECORDED_2026_09_20_MODEL_OUTPUT = json.dumps([
    {"beat_id": "beat-001", "text": "A routine outcome check completed cleanly.", "sign": "worked", "terms": []},
    {"beat_id": "beat-002", "text": "Added select_test_runner to verify_and_proof so it can pick the right runner for each test target.", "sign": "worked", "terms": []},
    {"beat_id": "beat-003", "text": "Added prune_zero_consumer_keys to filter out keys with no consumers from exclusion lists.", "sign": "worked", "terms": []},
    {"beat_id": "beat-004", "text": "Added count_consecutive_matching_errors to check_repeat_failures to tally repeated trailing errors.", "sign": "worked", "terms": []},
    {"beat_id": "beat-005", "text": "Worked on extract_incident_id to pull IDs from defect strings, but the final result is unknown.", "sign": "unknown", "terms": []},
    {"beat_id": "beat-006", "text": "Excluded index.md from schema integrity checks across lesson files.", "sign": "worked", "terms": []},
    {"beat_id": "beat-007", "text": "Updated AGENTS.md to point failure searches toward the subagent archive rather than the ledger.", "sign": "worked", "terms": []},
    {"beat_id": "beat-008", "text": "Added steering rules in AGENTS.md to keep final turns strictly raw JSON, though the outcome is unknown.", "sign": "unknown", "terms": []},
])


def test_recorded_2026_09_20_day_now_validates_ok_no_second_model_call():
    """#1840 AC: re-running the narrator for 2026-09-20 reaches an ok
    result. This is a replay of the recorded day -- the beats and the
    model's own output are copied verbatim from state/story/2026-09-20.json
    on the eeepc host, fetched via SSH from the live artifact; no model is
    called here. Before #1840, this raised StoryValidationError on
    beat-007 ("negative wording contradicts worked beat beat-007") --
    beat-007's own event text names "failure" as the subject of the
    change, and the model's faithful retelling echoed that exact word."""
    validated = validate_narration(
        json.loads(_RECORDED_2026_09_20_MODEL_OUTPUT), _RECORDED_2026_09_20_BEATS,
    )
    assert len(validated) == 8
    beat_007 = next(item for item in validated if item["beat_id"] == "beat-007")
    assert beat_007["sign"] == "worked"
    assert "failure" in beat_007["text"].lower()


def test_no_beats_day_writes_ok_artifact_with_no_model_call(tmp_path):
    calls: list = []

    def _should_not_be_called(messages, model):
        calls.append(1)
        return "[]"

    result = run_narrator_job(tmp_path, "2026-09-15", llm=_should_not_be_called)
    assert result["status"] == "ok"
    assert result["beats"] == []
    assert result["narration"] == []
    assert not calls  # no model call for a day with nothing to narrate
    assert Path(result["artifact_path"]).is_file()


def test_artifact_path_is_state_story_day_json(tmp_path):
    _write_ledger(tmp_path, [_ledger_row("2026-09-15T08:00:00Z", "cycle-a", "success")], [])
    result = run_narrator_job(tmp_path, "2026-09-15", llm=_compliant_llm)
    assert result["artifact_path"] == str(tmp_path / "story" / "2026-09-15.json")
