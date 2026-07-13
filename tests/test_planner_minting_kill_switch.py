"""Issue #739: deterministic-planner minting kill-switch.

Post-#707-GO the LLM proposer is the reliable request source (#707); the
deterministic planner (coordinator feedback-decision lanes) re-mints the
SAME duplicate ``subagent-verify-materialized-improvement`` request every
cycle it runs (~79 dup-skips/day against a fixed commit), pure ledger noise.
``SELFEVO_DETERMINISTIC_PLANNER_ENABLED`` (default "1" = today's behavior,
byte-identical) lets an operator flip the planner's two request-minting call
sites off ("0") without deleting any lane code or touching the bridge/
proposer — mirrors the ``SELFEVO_LLM_PROPOSER_ENABLED`` kill-switch style
from #707 (nanobot/runtime/llm_proposer.py).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.runtime.cycle_planning import (
    DETERMINISTIC_PLANNER_ENABLED_ENV,
    _deterministic_planner_enabled,
    _ensure_verify_request_for_fresh_materialization,
    _write_subagent_request_artifact,
)


def _write_materialized_artifact(improvements_dir: Path, *, name: str, title: str) -> Path:
    improvements_dir.mkdir(parents=True, exist_ok=True)
    path = improvements_dir / name
    path.write_text(json.dumps({
        "schema_version": "materialized-improvement-v1",
        "task_id": "materialize-synthesized-improvement",
        "next_bounded_candidate": {"title": title},
        "derived_candidate": {"title": title},
    }), encoding="utf-8")
    return path


def _bounded_execution_plan() -> dict:
    return {
        "current_task_id": "subagent-verify-materialized-improvement",
        "tasks": [
            {"task_id": "subagent-verify-materialized-improvement", "title": "verify"},
        ],
        "materialized_improvement_artifact_path": "/nonexistent/materialized-x.json",
    }


# ---------------------------------------------------------------------------
# Helper truthiness table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw_value,expected",
    [
        (None, True),        # unset -> default "1" -> enabled
        ("1", True),
        ("0", False),
        ("garbage", True),   # anything other than literal "0" preserves current behavior
        ("", True),
        ("  0  ", False),    # whitespace-tolerant
        ("00", True),        # only the exact literal "0" disables
    ],
)
def test_deterministic_planner_enabled_truthiness(
    monkeypatch: pytest.MonkeyPatch, raw_value: str | None, expected: bool
) -> None:
    if raw_value is None:
        monkeypatch.delenv(DETERMINISTIC_PLANNER_ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(DETERMINISTIC_PLANNER_ENABLED_ENV, raw_value)
    assert _deterministic_planner_enabled() is expected


# ---------------------------------------------------------------------------
# _write_subagent_request_artifact
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_value", [None, "1", "garbage"])
def test_write_subagent_request_artifact_unaffected_when_flag_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_value: str | None
) -> None:
    if raw_value is None:
        monkeypatch.delenv(DETERMINISTIC_PLANNER_ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(DETERMINISTIC_PLANNER_ENABLED_ENV, raw_value)

    state_root = tmp_path / "state"
    result_path = _write_subagent_request_artifact(
        state_root=state_root,
        cycle_id="cycle-1",
        goal_id="goal-1",
        current_plan=_bounded_execution_plan(),
    )

    assert result_path is not None
    assert Path(result_path).exists()


def test_write_subagent_request_artifact_returns_none_and_writes_nothing_when_flag_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DETERMINISTIC_PLANNER_ENABLED_ENV, "0")

    state_root = tmp_path / "state"
    result_path = _write_subagent_request_artifact(
        state_root=state_root,
        cycle_id="cycle-1",
        goal_id="goal-1",
        current_plan=_bounded_execution_plan(),
    )

    assert result_path is None
    requests_dir = state_root / "subagents" / "requests"
    assert not requests_dir.exists() or list(requests_dir.glob("*.json")) == []


# ---------------------------------------------------------------------------
# _ensure_verify_request_for_fresh_materialization
# ---------------------------------------------------------------------------

def test_ensure_verify_request_for_fresh_materialization_returns_none_when_flag_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DETERMINISTIC_PLANNER_ENABLED_ENV, "0")

    state_root = tmp_path / "state"
    improvements_dir = state_root / "improvements"
    _write_materialized_artifact(
        improvements_dir,
        name="materialized-cycle-739.json",
        title="Some fresh hypothesis title never seen in git log",
    )

    result_path = _ensure_verify_request_for_fresh_materialization(
        state_root=state_root,
        cycle_id="cycle-739",
        goal_id="goal-bootstrap",
    )

    assert result_path is None
    requests_dir = state_root / "subagents" / "requests"
    assert not requests_dir.exists() or list(requests_dir.glob("*.json")) == []


def test_ensure_verify_request_for_fresh_materialization_unaffected_when_flag_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DETERMINISTIC_PLANNER_ENABLED_ENV, raising=False)

    state_root = tmp_path / "state"
    improvements_dir = state_root / "improvements"
    fresh_artifact = _write_materialized_artifact(
        improvements_dir,
        name="materialized-cycle-739b.json",
        title="Another fresh hypothesis title never seen in git log",
    )

    result_path = _ensure_verify_request_for_fresh_materialization(
        state_root=state_root,
        cycle_id="cycle-739b",
        goal_id="goal-bootstrap",
    )

    assert result_path is not None
    written = json.loads(Path(result_path).read_text(encoding="utf-8"))
    assert written["source_artifact"] == str(fresh_artifact)
