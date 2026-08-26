from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import demand
from nanobot.runtime.skill_candidate_mining import mine


def _write_rows(state: Path, rows: list[dict]) -> None:
    path = state / "action_index" / "2026-08-01.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _rows(count: int = 10) -> list[dict]:
    return [
        {
            "cycle_id": f"cycle-{i}",
            "ts": f"2026-08-{i + 1:02d}T12:00:00Z",
            "actions": ["read:scripts/*.py", "exec:pytest", "edit:scripts/*.py"],
        }
        for i in range(1, count + 1)
    ]


def test_longest_recurrent_ngram_collapses_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    _write_rows(tmp_path, _rows())
    candidates = mine(tmp_path, None)
    assert len(candidates) == 1
    assert candidates[0]["sequence"] == ["read:scripts/*.py", "exec:pytest", "edit:scripts/*.py"]
    assert candidates[0]["cycles"] == 10
    assert candidates[0]["days"] == 10
    assert candidates[0]["samples"] == ["cycle-1", "cycle-2", "cycle-3"]


def test_trivial_and_below_threshold_patterns_are_suppressed(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    rows = _rows(7)
    rows.extend({**row, "cycle_id": "trivial-" + row["cycle_id"], "actions": ["exec:pytest", "exec:git-commit"]} for row in _rows(10))
    _write_rows(tmp_path, rows)
    candidates = mine(tmp_path, None)
    assert candidates == []


def test_candidate_enters_demand_and_completed_candidate_is_suppressed(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    _write_rows(tmp_path, _rows())
    items = demand._skill_candidate_items(tmp_path, None)
    assert len(items) == 1
    assert items[0]["kind"] == "skill-candidate"
    assert "recurs in 10 distinct cycles" in items[0]["evidence"]
    completed = tmp_path / "demand" / "completed.json"
    completed.parent.mkdir(parents=True, exist_ok=True)
    completed.write_text(json.dumps({"entries": {items[0]["id"]: {"ts": "2026-08-10T00:00:00Z"}}}), encoding="utf-8")
    collected = demand.collect_demand(tmp_path, None)
    assert not any(item["id"] == items[0]["id"] for item in collected)


def test_candidate_kind_is_ordered_before_hypothesis(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    _write_rows(tmp_path, _rows())
    monkeypatch.setattr(demand, "_priority_items", lambda *_: [])
    monkeypatch.setattr(demand, "_ledger_defects", lambda *_: [])
    monkeypatch.setattr(demand, "_result_file_defects", lambda *_: [])
    monkeypatch.setattr(demand, "_compile_defects", lambda *_: [])
    monkeypatch.setattr(demand, "_heldout_defect_items", lambda *_: [])
    monkeypatch.setattr(demand, "_validator_defect_items", lambda *_: [])
    monkeypatch.setattr(demand, "_tamper_defect_items", lambda *_: [])
    monkeypatch.setattr(demand, "_repair_unused_items", lambda *_: [])
    monkeypatch.setattr(demand, "_goal_gap_items", lambda *_: [{"kind": "goal-gap", "id": "gap", "summary": "gap", "evidence": "", "affected_path": "", "vector": "V1", "direction": ""}])
    monkeypatch.setattr(demand, "_hypothesis_items", lambda *_: [{"kind": "hypothesis", "id": "hyp", "summary": "hyp", "evidence": "", "affected_path": "", "vector": "", "direction": ""}])
    monkeypatch.setattr(demand, "_decay_items", lambda *_: [])
    monkeypatch.setattr(demand, "_reflection_items", lambda *_: [])
    kinds = [item["kind"] for item in demand.collect_demand(tmp_path, None)]
    assert kinds == ["goal-gap", "skill-candidate", "hypothesis"]


def test_existing_skill_suppresses_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    _write_rows(tmp_path, _rows())
    skill = tmp_path / "skills" / "repeat-review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Repeat review\n\nread:scripts/*.py exec:pytest edit:scripts/*.py\n", encoding="utf-8")
    assert mine(tmp_path, tmp_path) == []


def test_no_llm_dependency_and_window_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_WINDOW_DAYS", "30")
    source = Path("nanobot/runtime/skill_candidate_mining.py").read_text(encoding="utf-8")
    assert not any(token in source for token in ("openai", "litellm", "LLMProvider"))
    _write_rows(tmp_path, [{**_rows(1)[0], "ts": "2020-01-01T00:00:00Z"}])
    assert mine(tmp_path, None) == []
