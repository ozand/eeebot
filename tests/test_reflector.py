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
    assert json.loads((tmp_path / "reflector/reflections.jsonl").read_text().splitlines()[0])["status"] == "error"


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
    candidates = reflector._completed_cycles(reflector._ledger_rows(tmp_path), "")
    opened.clear()
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
    candidates = reflector._completed_cycles(reflector._ledger_rows(tmp_path), "")
    opened_iter.clear()
    reflector._prompt_records(tmp_path, candidates)
    assert "2026-08-25.jsonl" not in opened_iter


def test_reflector_caps_consecutive_errors(tmp_path: Path):
    _write(tmp_path / "ledger/cycles.jsonl", [
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
    result = reflector.run_reflector(tmp_path, llm=lambda *_: "bad", max_consecutive_errors=2)
    assert result["errors"] == 2
    assert result["consecutive_errors"] == 2
    assert not (tmp_path / "reflector/watermark.json").exists()


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
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(reflector.time, "monotonic", lambda: next(ticks))
    result = reflector.run_reflector(tmp_path, llm=lambda *_: _answer("c1"), max_runtime_seconds=1)
    assert result["processed"] == 1
