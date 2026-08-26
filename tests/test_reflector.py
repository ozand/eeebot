from __future__ import annotations

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
