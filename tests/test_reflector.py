from __future__ import annotations

import gzip
import json
from pathlib import Path

from nanobot.runtime import demand, model_registry, reflector


def _write(path: Path, rows: list[dict] | list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in rows) + "\n", encoding="utf-8")


def _seed(state: Path, cycle: str = "c1") -> None:
    _write(state / "ledger/cycles.jsonl", [
        {"phase": "proposed", "cycle_id": cycle, "task_title": "Do work", "target_path": "scripts/a.py"},
        {"phase": "gate", "cycle_id": cycle, "allowed": True},
        {"phase": "outcome", "cycle_id": cycle, "outcome": "success", "ts": "2026-08-26T00:00:00Z"},
    ])
    _write(state / "llm_calls/prompts/2026-08-26.jsonl", [
        {"cycle_id": cycle, "seq": 1, "messages": [{"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "scripts/a.py"}}}]}]},
    ])


def _answer(cycle: str = "c1") -> str:
    return json.dumps({"cycle_id": cycle, "summary": "good", "findings": [{"kind": "good_practice", "detail": "bounded"}], "recommendations": [{"kind": "approach_hint", "detail": "reuse helper", "evidence": cycle}], "followed_previous": []})


def test_reflector_journal_watermark_and_prior_tail(tmp_path: Path):
    _seed(tmp_path)
    seen = []
    def llm(messages, model):
        seen.append(messages[1]["content"])
        return _answer()
    assert reflector.run_reflector(tmp_path, llm=llm)["processed"] == 1
    assert "PRIOR REFLECTIONS" in seen[0]
    assert reflector.run_reflector(tmp_path, llm=llm)["processed"] == 0
    assert (tmp_path / "reflector/reflections.jsonl").exists()


def test_malformed_output_watermark_unmoved(tmp_path: Path):
    _seed(tmp_path)
    assert reflector.run_reflector(tmp_path, llm=lambda *_: "bad")["errors"] == 1
    assert not (tmp_path / "reflector/watermark.json").exists()
    row = json.loads((tmp_path / "reflector/reflections.jsonl").read_text().splitlines()[0])
    assert row["status"] == "error"
    assert "not_json" in row["error"]
    assert row["response_head"] == "bad"


def test_parse_output_reports_distinct_validation_reasons():
    assert reflector._parse_output("bad", "c1") == (None, "not_json")
    fenced = "```json\n" + _answer("c1") + "\n```"
    parsed, reason = reflector._parse_output(fenced, "c1")
    assert parsed is not None and reason == "fenced_json"
    parsed, reason = reflector._parse_output({"cycle_id": "wrong", "summary": "x", "findings": [], "recommendations": []}, "c1")
    assert parsed is None and reason == "cycle_id_mismatch"
    parsed, reason = reflector._parse_output({"cycle_id": "c1", "findings": [], "recommendations": []}, "c1")
    assert parsed is None and reason == "missing_or_invalid:summary"
    parsed, reason = reflector._parse_output({"cycle_id": "c1", "summary": "x", "findings": [{"kind": "bad", "detail": "x"}], "recommendations": []}, "c1")
    assert parsed is None and reason.startswith("invalid_finding:")
    parsed, reason = reflector._parse_output({"cycle_id": "c1", "summary": "x", "findings": [], "recommendations": [{"kind": "bad", "detail": "x"}]}, "c1")
    assert parsed is None and reason.startswith("invalid_recommendation:")
    parsed, reason = reflector._parse_output([], "c1")
    assert parsed is None and reason == "not_object"


def test_parse_output_reports_fenced_invalid_json():
    parsed, reason = reflector._parse_output("```json\nnot-json\n```", "c1")
    assert parsed is None and reason == "fenced_not_json"


def test_fenced_json_is_repaired_and_reason_is_journaled(tmp_path: Path):
    _seed(tmp_path)
    fenced = "```json\n" + _answer("c1") + "\n```"
    assert reflector.run_reflector(tmp_path, llm=lambda *_: fenced)["processed"] == 1
    row = json.loads((tmp_path / "reflector/reflections.jsonl").read_text().splitlines()[0])
    assert row["parse_reason"] == "fenced_json"


def test_pruned_transcript_is_journaled_and_watermarked(tmp_path: Path):
    _write(tmp_path / "ledger/cycles.jsonl", [{"phase": "outcome", "cycle_id": "gone", "outcome": "failed"}])
    result = reflector.run_reflector(tmp_path, llm=lambda *_: "bad")
    assert result["skipped_pruned"] == 1
    assert json.loads((tmp_path / "reflector/watermark.json").read_text())["last_processed"] == "gone"


def test_recommendations_are_demand_items(tmp_path: Path):
    _seed(tmp_path)
    reflector.run_reflector(tmp_path, llm=lambda *_: _answer())
    items = demand._reflection_items(tmp_path)
    assert items and items[0]["kind"] == "reflection" and "cycle c1" in items[0]["evidence"]


def test_reflector_unit_allows_state_telemetry_without_widening_hardening():
    unit = (Path(__file__).parents[1] / "host/eeepc/systemd/eeebot-reflector.service").read_text(encoding="utf-8")
    assert "ReadWritePaths=/var/lib/eeepc-agent/self-evolving-agent/state\n" in unit
    assert "ReadWritePaths=/var/lib/eeepc-agent/self-evolving-agent/state/reflector" not in unit
    assert "ProtectSystem=strict" in unit
    assert "NoNewPrivileges=true" in unit
    assert "PrivateTmp=true" in unit


def test_reflector_model_override(monkeypatch):
    monkeypatch.setenv("SELFEVO_REFLECTOR_MODEL", "an/reflection-model")
    assert model_registry.resolve_model("reflector") == "an/reflection-model"


def test_prompt_lookup_opens_only_candidate_date_files(tmp_path: Path, monkeypatch):
    _seed(tmp_path)
    prompt_dir = tmp_path / "llm_calls" / "prompts"
    _write(prompt_dir / "2026-08-20.jsonl", [{"cycle_id": "other", "seq": 1, "messages": []}])
    opened = []
    original = reflector._iter_jsonl
    def tracking(path):
        opened.append(path.name)
        yield from original(path)
    monkeypatch.setattr(reflector, "_iter_jsonl", tracking)
    opened.clear()
    candidates = reflector._completed_cycles(reflector._ledger_rows(tmp_path), "")
    reflector._prompt_records(tmp_path, candidates)
    assert opened[-1:] == ["2026-08-26.jsonl"]
    assert "2026-08-20.jsonl" not in opened


def test_gz_only_transcript_is_processed_not_skipped(tmp_path: Path):
    _seed(tmp_path)
    prompt_dir = tmp_path / "llm_calls" / "prompts"
    plain = prompt_dir / "2026-08-26.jsonl"
    gz = prompt_dir / "2026-08-25.jsonl.gz"
    plain.unlink()
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"cycle_id": "c1", "seq": 1, "messages": [{"role": "assistant", "content": "work"}]}) + "\n")
    answer = {"cycle_id": "c1", "summary": "reviewed", "findings": [{"kind": "good_practice", "detail": "clear sequence"}], "recommendations": [], "followed_previous": []}
    result = reflector.run_reflector(tmp_path, llm=lambda *_: json.dumps(answer))
    assert result["processed"] == 1
    assert result["skipped_pruned"] == 0
    row = json.loads((tmp_path / "reflector/reflections.jsonl").read_text().splitlines()[0])
    assert row["findings"]


def test_prompt_records_substring_prefilter_skips_unmatched_files(tmp_path: Path, monkeypatch):
    _seed(tmp_path)
    prompt_dir = tmp_path / "llm_calls" / "prompts"
    _write(prompt_dir / "2026-08-25.jsonl", [{"cycle_id": "other", "seq": 1, "messages": []}])
    opened_iter = []
    original = reflector._iter_jsonl
    def tracking(path):
        opened_iter.append(path.name)
        yield from original(path)
    monkeypatch.setattr(reflector, "_iter_jsonl", tracking)
    opened_iter.clear()
    candidates = reflector._completed_cycles(reflector._ledger_rows(tmp_path), "")
    reflector._prompt_records(tmp_path, candidates)
    assert "2026-08-25.jsonl" not in opened_iter


def test_reflector_caps_consecutive_errors(tmp_path: Path):
    _write(tmp_path / "ledger/cycles.jsonl", [
        {"phase": "outcome", "cycle_id": "c0", "outcome": "failed", "ts": "2026-08-25T23:59:00Z"},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "failed", "ts": "2026-08-26T00:00:00Z"},
        {"phase": "outcome", "cycle_id": "c2", "outcome": "failed", "ts": "2026-08-26T00:01:00Z"},
        {"phase": "outcome", "cycle_id": "c3", "outcome": "failed", "ts": "2026-08-26T00:02:00Z"},
        {"phase": "outcome", "cycle_id": "c4", "outcome": "failed", "ts": "2026-08-26T00:03:00Z"},
    ])
    _write(tmp_path / "llm_calls/prompts/2026-08-26.jsonl", [
        {"cycle_id": "c1", "seq": 1, "messages": []},
        {"cycle_id": "c2", "seq": 1, "messages": []},
        {"cycle_id": "c3", "seq": 1, "messages": []},
        {"cycle_id": "c4", "seq": 1, "messages": []},
    ])
    result = reflector.run_reflector(tmp_path, llm=lambda *_: "bad", max_consecutive_errors=2, max_cycles=4)
    assert result["errors"] == 2
    assert result["consecutive_errors"] == 2
    assert json.loads((tmp_path / "reflector/watermark.json").read_text())["last_processed"] == "c0"


def test_reflector_wall_clock_guard_stops_before_next_cycle(tmp_path: Path, monkeypatch):
    _seed(tmp_path, "c1")
    _write(tmp_path / "ledger/cycles.jsonl", [
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-08-26T00:00:00Z"},
        {"phase": "outcome", "cycle_id": "c2", "outcome": "success", "ts": "2026-08-26T00:01:00Z"},
    ])
    _write(tmp_path / "llm_calls/prompts/2026-08-26.jsonl", [
        {"cycle_id": "c1", "seq": 1, "messages": []},
        {"cycle_id": "c2", "seq": 1, "messages": []},
    ])
    ticks = iter((0.0, 0.0, 0.0, 0.0, 2.0, 2.0))
    monkeypatch.setattr(reflector.time, "monotonic", lambda: next(ticks))
    result = reflector.run_reflector(tmp_path, llm=lambda *_: _answer("c1"), max_runtime_seconds=1)
    assert result["processed"] == 1


# Response-shape diagnostics must remain independently countable.

def test_classify_not_json_fenced_unclosed():
    """A fence that opens but never closes → fenced_unclosed."""
    raw = "```json\n{\"cycle_id\": \"c1\""
    _, reason = reflector._parse_output(raw, "c1")
    assert reason == "fenced_unclosed"


def test_classify_not_json_fenced_trailing_text():
    """Fence with closing marker but trailing prose → fenced_trailing_text."""
    raw = "```json\n" + _answer("c1") + "\n```\nHope this helps!"
    _, reason = reflector._parse_output(raw, "c1")
    assert reason == "fenced_trailing_text"


def test_classify_not_json_prose_then_fence():
    """Prose then a fence (chatty model) → prose_then_fence."""
    raw = "Sure, here is the reflection:\n```json\n" + _answer("c1") + "\n```"
    _, reason = reflector._parse_output(raw, "c1")
    assert reason == "prose_then_fence"


def test_classify_not_json_json_truncated():
    """Bare object or array that json.loads rejects (truncated) → json_truncated."""
    _, reason = reflector._parse_output('{"cycle_id": "c1", "summ', "c1")
    assert reason == "json_truncated"
    _, reason2 = reflector._parse_output('["partial', "c1")
    assert reason2 == "json_truncated"


def test_classify_not_json_plain_prose():
    """Plain prose (no JSON, no fence) → not_json."""
    _, reason = reflector._parse_output("The model returned prose.", "c1")
    assert reason == "not_json"


def test_cycle_id_missing_vs_mismatch():
    """cycle_id absent → cycle_id_missing; wrong value → cycle_id_mismatch."""
    _, reason = reflector._parse_output(
        {"summary": "x", "findings": [], "recommendations": []}, "c1"
    )
    assert reason == "cycle_id_missing"

    _, reason2 = reflector._parse_output(
        {"cycle_id": "wrong", "summary": "x", "findings": [], "recommendations": []}, "c1"
    )
    assert reason2 == "cycle_id_mismatch"


def test_invalid_finding_bad_kind_vs_empty_detail():
    """Valid kind + empty detail → empty_detail; invalid kind → bad_kind:<kind>."""
    base = {"cycle_id": "c1", "summary": "x", "recommendations": []}

    # invalid kind
    _, reason = reflector._parse_output(
        {**base, "findings": [{"kind": "unknown_kind", "detail": "something"}]}, "c1"
    )
    assert reason == "invalid_finding:bad_kind:unknown_kind"

    # valid kind, empty detail
    _, reason2 = reflector._parse_output(
        {**base, "findings": [{"kind": "error_pattern", "detail": ""}]}, "c1"
    )
    assert reason2 == "invalid_finding:empty_detail"

    # not a dict at all
    _, reason3 = reflector._parse_output(
        {**base, "findings": ["not_a_dict"]}, "c1"
    )
    assert reason3 == "invalid_finding:not_object"


def test_invalid_recommendation_bad_kind_vs_empty_detail():
    """Valid kind + empty detail → empty_detail; invalid kind → bad_kind:<kind>."""
    base = {"cycle_id": "c1", "summary": "x", "findings": []}

    # invalid kind
    _, reason = reflector._parse_output(
        {**base, "recommendations": [{"kind": "not_a_kind", "detail": "something"}]}, "c1"
    )
    assert reason == "invalid_recommendation:bad_kind:not_a_kind"

    # valid kind, empty detail
    _, reason2 = reflector._parse_output(
        {**base, "recommendations": [{"kind": "skill_candidate", "detail": ""}]}, "c1"
    )
    assert reason2 == "invalid_recommendation:empty_detail"

    # not a dict at all
    _, reason3 = reflector._parse_output(
        {**base, "recommendations": [42]}, "c1"
    )
    assert reason3 == "invalid_recommendation:not_object"


def test_error_row_has_structured_fields(tmp_path: Path):
    """Error journal rows carry parse_reason, response_chars, and response_tail as top-level keys."""
    _seed(tmp_path)
    truncated = '{"cycle_id": "c1", "summ'  # json_truncated shape
    reflector.run_reflector(tmp_path, llm=lambda *_: truncated)
    row = json.loads((tmp_path / "reflector/reflections.jsonl").read_text().splitlines()[0])
    assert row["status"] == "error"
    assert row["parse_reason"] == "json_truncated"
    assert row["response_chars"] == len(truncated)
    assert isinstance(row["response_tail"], str)
    assert truncated[-10:] in row["response_tail"]  # tail contains the end of response
    assert isinstance(row["response_head"], str)


def test_error_row_fields_not_stale_across_consecutive_attempts(tmp_path: Path):
    """Consecutive errors each get their own response/reason; no cross-contamination."""
    _write(tmp_path / "ledger/cycles.jsonl", [
        {"phase": "outcome", "cycle_id": "c1", "outcome": "failed", "ts": "2026-08-26T00:00:00Z"},
        {"phase": "outcome", "cycle_id": "c2", "outcome": "failed", "ts": "2026-08-26T00:01:00Z"},
    ])
    _write(tmp_path / "llm_calls/prompts/2026-08-26.jsonl", [
        {"cycle_id": "c1", "seq": 1, "messages": []},
        {"cycle_id": "c2", "seq": 1, "messages": []},
    ])
    responses = iter(["prose only", '{"cycle_id": "c2"'])
    result = reflector.run_reflector(tmp_path, llm=lambda *_: next(responses), max_cycles=2, max_consecutive_errors=2)
    assert result["errors"] == 2
    rows = [json.loads(line) for line in (tmp_path / "reflector/reflections.jsonl").read_text().splitlines() if line.strip()]
    err_rows = [r for r in rows if r.get("status") == "error"]
    assert len(err_rows) == 2
    # Each row records its own cycle_id
    assert err_rows[0]["cycle_id"] == "c1"
    assert err_rows[1]["cycle_id"] == "c2"
    # Reasons are distinct and correspond to their response shape
    assert err_rows[0]["parse_reason"] == "not_json"
    assert err_rows[1]["parse_reason"] == "json_truncated"
    # response_head and response_chars are not stale (each cycle's own)
    assert err_rows[0]["response_head"] == "prose only"
    assert err_rows[1]["response_chars"] == len('{"cycle_id": "c2"')


def test_repaired_fence_count_is_visible_in_journal(tmp_path: Path):
    """fenced_json repairs are countable: each success row has parse_reason=fenced_json."""
    _write(tmp_path / "ledger/cycles.jsonl", [
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-08-26T00:00:00Z"},
        {"phase": "outcome", "cycle_id": "c2", "outcome": "success", "ts": "2026-08-26T00:01:00Z"},
    ])
    _write(tmp_path / "llm_calls/prompts/2026-08-26.jsonl", [
        {"cycle_id": "c1", "seq": 1, "messages": []},
        {"cycle_id": "c2", "seq": 1, "messages": []},
    ])

    def fenced_llm(messages, model):
        cid = messages[1]["content"].split("CYCLE_ID: ")[1].split("\n")[0].strip()
        return "```json\n" + json.dumps({"cycle_id": cid, "summary": "ok", "findings": [], "recommendations": [], "followed_previous": []}) + "\n```"

    result = reflector.run_reflector(tmp_path, llm=fenced_llm, max_cycles=2)
    assert result["processed"] == 2
    rows = [json.loads(line) for line in (tmp_path / "reflector/reflections.jsonl").read_text().splitlines() if line.strip()]
    fenced_count = sum(1 for r in rows if r.get("parse_reason") == "fenced_json")
    assert fenced_count == 2


def test_llm_exception_produces_error_row_with_empty_parse_reason(tmp_path: Path):
    """A transport failure explicitly records that parsing was not attempted."""
    _seed(tmp_path)

    def failing_llm(messages, model):
        raise RuntimeError("connection refused")

    reflector.run_reflector(tmp_path, llm=failing_llm)
    row = json.loads((tmp_path / "reflector/reflections.jsonl").read_text().splitlines()[0])
    assert row["status"] == "error"
    assert row["parse_reason"] == "not_attempted"
    assert row["response_chars"] == 0
    assert row["response_head"] == ""
    assert row["response_tail"] == ""
