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


def test_mint_result_distinguishes_candidates_and_staged_counts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_reflections(state, [{"kind": "approach_hint", "detail": "Use bounded parser reads incrementally for large files", "evidence": "#1216"}])

    result = curator.promote_reflector_recommendations_to_v2(tmp_path, state)

    assert result["rows_read"] == 1
    assert result["candidates"] == 1
    assert result["rejected"] == 0
    assert result["folded"] == 0
    assert result["staged"] == 1
    assert result["mint_succeeded"] is True


def test_mint_result_distinguishes_absent_store_from_empty_store(tmp_path: Path) -> None:
    state = tmp_path / "state"

    absent = curator.promote_reflector_recommendations_to_v2(tmp_path, state)
    assert absent["store"] == "absent"
    assert absent["rows_read"] == 0
    assert absent["mint_succeeded"] is False

    _write_reflections(state, [])
    empty = curator.promote_reflector_recommendations_to_v2(tmp_path, state)
    assert empty["store"] == "present"
    assert empty["rows_read"] == 1
    assert empty["candidates"] == 0
    assert empty["staged"] == 0
    assert empty["mint_succeeded"] is True


def test_run_curation_updates_last_successful_mint_only_when_staged(tmp_path: Path) -> None:
    state = tmp_path / "state"
    status_path = state / "curator" / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps({"last_successful_mint_ts": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    _write_reflections(state, [])

    quiet = curator.run_curation(tmp_path, state, llm=lambda *_args: "[]")
    quiet_status = json.loads(status_path.read_text(encoding="utf-8"))
    assert quiet["stages"]["reflector_mint"]["staged"] == 0
    assert quiet_status["last_successful_mint_ts"] == "2026-01-01T00:00:00+00:00"

    _write_reflections(state, [{"kind": "approach_hint", "detail": "Use bounded parser reads incrementally for large files", "evidence": "#1216"}])
    minted = curator.run_curation(tmp_path, state, llm=lambda *_args: "[]")
    minted_status = json.loads(status_path.read_text(encoding="utf-8"))
    assert minted["stages"]["reflector_mint"]["staged"] == 1
    assert minted_status["last_successful_mint_ts"] != "2026-01-01T00:00:00+00:00"


def test_run_curation_persists_last_success_and_stage_outcomes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_reflections(state, [])
    result = curator.run_curation(tmp_path, state, llm=lambda *_args: "[]")

    assert result["ok"] is True
    assert "reflector_mint" in result["stages"]
    status = json.loads((state / "curator" / "status.json").read_text(encoding="utf-8"))
    assert status["last_success_ts"]
    assert status["reflector_mint"] == result["stages"]["reflector_mint"]
