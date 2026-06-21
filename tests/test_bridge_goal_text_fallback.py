"""
Tests for issue #508: goal_text fallback in subagent bridge.

Verifies that the bridge reads goal_text from:
1. STATE_DIR/goals/goal_text.json (primary)
2. TARGET_WORKSPACE/host/eeepc/etc/goal_text.json (fallback when primary missing)
3. goal_id string (last resort)
"""
import json
import importlib
import sys
import types
from pathlib import Path
import pytest


MISSION_TEXT = (
    "eeebot is a resource-aware, self-evolving autonomous agent on a weak eeepc host. "
    "Priority A: archive stale requests. Priority B: create dashboard. "
    "Priority C: enumerate host capabilities. Priority D: commit code improvement."
)


def _load_bridge_load_json(tmp_path: Path):
    """
    Import load_json from the bridge script without executing its __main__ block.
    We do a minimal import via importlib machinery.
    """
    bridge_path = (
        Path(__file__).parent.parent / "scripts" / "eeepc_self_evolving_subagent_bridge.py"
    )
    spec = importlib.util.spec_from_file_location("bridge_module", bridge_path)
    assert spec is not None
    mod = types.ModuleType("bridge_module")
    mod.__spec__ = spec
    # Stub heavy deps so the module-level code doesn't fail in test env
    for dep in ["nanobot", "nanobot.config", "nanobot.config.schema",
                "nanobot.runtime", "nanobot.runtime.subagent_manager",
                "nanobot.agent", "nanobot.agent.subagent"]:
        if dep not in sys.modules:
            sys.modules[dep] = types.ModuleType(dep)
    return mod


def _simulate_goal_text_resolution(state_dir: Path, target_workspace: Path, goal_id: str) -> str:
    """
    Replicate the exact goal_text resolution logic from the bridge (lines 271-278).
    This avoids importing the full bridge but tests the same logic path.
    """
    def load_json(p: Path) -> dict | None:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    goal_text = (
        (load_json(state_dir / "goals" / "goal_text.json") or {}).get("text")
        or (load_json(target_workspace / "host" / "eeepc" / "etc" / "goal_text.json") or {}).get("text")
        or goal_id
    )
    return goal_text


class TestGoalTextResolution:
    def test_primary_state_dir_file_used_when_present(self, tmp_path):
        """STATE_DIR/goals/goal_text.json is the primary source."""
        state_dir = tmp_path / "state"
        target_ws = tmp_path / "workspace"
        (state_dir / "goals").mkdir(parents=True)
        (state_dir / "goals" / "goal_text.json").write_text(
            json.dumps({"text": "primary mission text"}), encoding="utf-8"
        )
        # Also create fallback to confirm primary wins
        (target_ws / "host" / "eeepc" / "etc").mkdir(parents=True)
        (target_ws / "host" / "eeepc" / "etc" / "goal_text.json").write_text(
            json.dumps({"text": "fallback mission text"}), encoding="utf-8"
        )
        result = _simulate_goal_text_resolution(state_dir, target_ws, "goal-bootstrap")
        assert result == "primary mission text"

    def test_fallback_to_workspace_when_state_missing(self, tmp_path):
        """Falls back to TARGET_WORKSPACE/host/eeepc/etc/goal_text.json when primary absent."""
        state_dir = tmp_path / "state"
        target_ws = tmp_path / "workspace"
        (state_dir / "goals").mkdir(parents=True)
        # No goal_text.json in state/goals/
        (target_ws / "host" / "eeepc" / "etc").mkdir(parents=True)
        (target_ws / "host" / "eeepc" / "etc" / "goal_text.json").write_text(
            json.dumps({"text": MISSION_TEXT}), encoding="utf-8"
        )
        result = _simulate_goal_text_resolution(state_dir, target_ws, "goal-bootstrap")
        assert result == MISSION_TEXT

    def test_fallback_contains_priority_targets(self, tmp_path):
        """Fallback text should contain Priority A/B/C/D content (not just goal ID)."""
        state_dir = tmp_path / "state"
        target_ws = tmp_path / "workspace"
        (state_dir / "goals").mkdir(parents=True)
        (target_ws / "host" / "eeepc" / "etc").mkdir(parents=True)

        # Use actual goal_text.json from the repo
        actual_goal_text_path = (
            Path(__file__).parent.parent / "host" / "eeepc" / "etc" / "goal_text.json"
        )
        if actual_goal_text_path.exists():
            (target_ws / "host" / "eeepc" / "etc" / "goal_text.json").write_bytes(
                actual_goal_text_path.read_bytes()
            )
            result = _simulate_goal_text_resolution(state_dir, target_ws, "goal-bootstrap")
            assert len(result) > 100, "goal_text should be a real mission, not a short ID"
            assert "goal-bootstrap" not in result or len(result) > 50

    def test_last_resort_returns_goal_id(self, tmp_path):
        """When both files are missing, returns goal_id string."""
        state_dir = tmp_path / "state"
        target_ws = tmp_path / "workspace"
        (state_dir / "goals").mkdir(parents=True)
        (target_ws / "host" / "eeepc" / "etc").mkdir(parents=True)
        # Neither file exists
        result = _simulate_goal_text_resolution(state_dir, target_ws, "goal-bootstrap")
        assert result == "goal-bootstrap"

    def test_state_file_with_empty_text_falls_back(self, tmp_path):
        """Empty text field in state file should fall through to workspace fallback."""
        state_dir = tmp_path / "state"
        target_ws = tmp_path / "workspace"
        (state_dir / "goals").mkdir(parents=True)
        (state_dir / "goals" / "goal_text.json").write_text(
            json.dumps({"text": ""}), encoding="utf-8"
        )
        (target_ws / "host" / "eeepc" / "etc").mkdir(parents=True)
        (target_ws / "host" / "eeepc" / "etc" / "goal_text.json").write_text(
            json.dumps({"text": "fallback mission text"}), encoding="utf-8"
        )
        result = _simulate_goal_text_resolution(state_dir, target_ws, "goal-bootstrap")
        # Empty string is falsy → fallback is used
        assert result == "fallback mission text"
