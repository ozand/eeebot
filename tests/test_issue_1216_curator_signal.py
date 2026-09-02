from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from nanobot.runtime import knowledge_curator as curator


def _write_reflections(state: Path, recommendations: list[dict]) -> None:
    path = state / "reflector" / "reflections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "cycle_id": "cycle-1216",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "summary": "reflected parser issue",
            "recommendations": recommendations,
        }) + "\n",
        encoding="utf-8",
    )


def test_reflector_store_states_are_persisted(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"

    absent = curator.promote_reflector_recommendations_to_v2(tmp_path, state)
    assert absent == 0
    assert curator.load_reflector_pool(state)["last_run"]["store"] == "absent"

    _write_reflections(state, [])
    present = curator.promote_reflector_recommendations_to_v2(tmp_path, state)
    assert present == 0
    assert curator.load_reflector_pool(state)["last_run"]["store"] == "present"

    future = datetime.now(timezone.utc).timestamp() + 91 * 86400
    monkeypatch.setattr(curator.time, "time", lambda: future)
    stale = curator.promote_reflector_recommendations_to_v2(tmp_path, state)
    assert stale == 0
    assert curator.load_reflector_pool(state)["last_run"]["store"] == "stale"


def test_unreadable_reflector_store_is_distinct(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    _write_reflections(state, [])
    original_open = Path.open

    def fail_reflector_open(path, *args, **kwargs):
        if path.name == "reflections.jsonl":
            raise OSError("simulated read failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_reflector_open)
    assert curator.promote_reflector_recommendations_to_v2(tmp_path, state) == 0
    assert curator.load_reflector_pool(state)["last_run"]["store"] == "unreadable"


def test_run_curation_persists_fact_curation_stage_outcomes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    result = curator.run_curation(tmp_path, state, llm=lambda *_args: "[]")

    assert result["ok"] is True
    assert result["stages"]["curation"] == {
        "status": "empty", "processed": 0, "writes": 0, "staged": [],
    }
    status = json.loads((state / "curator" / "status.json").read_text(encoding="utf-8"))
    assert status["curation"] == result["stages"]["curation"]
    assert status["reflector_mint"]["store"] == "absent"


def test_run_curation_reports_fact_stage_error(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (tmp_path / "lessons").mkdir()
    (tmp_path / "lessons" / "lessons.yaml").write_text(
        "- id: L1\n  title: insight\n  approach: use it\n", encoding="utf-8",
    )

    result = curator.run_curation(tmp_path, state, llm=lambda *_args: "not json")

    assert result["ok"] is False
    assert result["stages"]["curation"] == {
        "status": "error", "processed": 0, "writes": 0, "staged": [],
    }
