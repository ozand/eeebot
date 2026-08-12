"""
Tests for issue #510: subagent-verify-materialized-improvement must use
bounded_execution profile and standard budget (not research_only / micro).

research_only prevents the LLM from using write/exec tools, causing subagents
to exit with 0 meaningful tool calls. bounded_execution grants full tool access.
micro budget (~2 tool calls) is insufficient for: git pull + implement + test +
git commit + MEMORY.md update.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.runtime.coordinator import (
    _derive_generated_candidates,
    _write_materialized_improvement_artifact,
)

MATERIALIZE_SYNTHESIZED_ID = "materialize-synthesized-improvement"
VERIFY_TASK_ID = "subagent-verify-materialized-improvement"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_goals_dir(tmp_path: Path) -> Path:
    """Create a minimal goals directory with empty history."""
    goals_dir = tmp_path / "goals"
    (goals_dir / "history").mkdir(parents=True)
    return goals_dir


def _get_verify_candidates(goals_dir: Path, parent_task_id: str) -> list[dict]:
    candidates = _derive_generated_candidates(
        goals_dir=goals_dir,
        result_status="PASS",
        current_task_id=parent_task_id,
    )
    return [c for c in candidates if c.get("task_id") == VERIFY_TASK_ID]


# ---------------------------------------------------------------------------
# Tests — _derive_generated_candidates
# ---------------------------------------------------------------------------

class TestSubagentVerifyProfile:

    def test_verify_candidate_profile_is_bounded_execution_from_materialize_pass(self, tmp_path):
        """materialize-pass-streak-improvement → verify candidate uses bounded_execution."""
        goals_dir = _make_goals_dir(tmp_path)
        candidates = _get_verify_candidates(goals_dir, "materialize-pass-streak-improvement")
        assert candidates, f"No {VERIFY_TASK_ID} candidate generated"
        for c in candidates:
            assert c.get("subagent_profile") == "bounded_execution", (
                f"Got {c.get('subagent_profile')!r}, expected bounded_execution"
            )

    def test_verify_candidate_budget_is_standard_from_materialize_pass(self, tmp_path):
        """materialize-pass-streak-improvement → verify candidate uses standard budget."""
        goals_dir = _make_goals_dir(tmp_path)
        candidates = _get_verify_candidates(goals_dir, "materialize-pass-streak-improvement")
        assert candidates
        for c in candidates:
            assert c.get("subagent_budget") == "standard", (
                f"Got {c.get('subagent_budget')!r}, expected standard"
            )

    def test_verify_candidate_profile_from_synthesized_materialization(self, tmp_path):
        """materialize-synthesized-improvement → verify candidate uses bounded_execution."""
        goals_dir = _make_goals_dir(tmp_path)
        candidates = _get_verify_candidates(goals_dir, MATERIALIZE_SYNTHESIZED_ID)
        assert candidates, f"No {VERIFY_TASK_ID} from {MATERIALIZE_SYNTHESIZED_ID}"
        for c in candidates:
            assert c.get("subagent_profile") == "bounded_execution"
            assert c.get("subagent_budget") == "standard"

    def test_no_research_only_in_any_verify_candidate(self, tmp_path):
        """Confirm research_only is absent from all verify candidates."""
        goals_dir = _make_goals_dir(tmp_path)
        for parent in ["materialize-pass-streak-improvement", MATERIALIZE_SYNTHESIZED_ID]:
            candidates = _get_verify_candidates(goals_dir, parent)
            for c in candidates:
                assert c.get("subagent_profile") != "research_only", (
                    f"research_only found in candidate from {parent} — blocks tool use"
                )

    def test_no_micro_budget_in_any_verify_candidate(self, tmp_path):
        """Confirm micro budget is absent from all verify candidates."""
        goals_dir = _make_goals_dir(tmp_path)
        for parent in ["materialize-pass-streak-improvement", MATERIALIZE_SYNTHESIZED_ID]:
            candidates = _get_verify_candidates(goals_dir, parent)
            for c in candidates:
                assert c.get("subagent_budget") != "micro", (
                    f"micro budget found in candidate from {parent} — insufficient for commit flow"
                )
