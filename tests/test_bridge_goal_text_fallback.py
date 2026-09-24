"""ADR-034 rules 2/3: the executor mission's base goal text.

Was #508's goal_text fallback chain (STATE_DIR primary ->
TARGET_WORKSPACE/host/eeepc/etc fallback -> goal_id last resort) --
ADR-034 A2 deleted that chain (the workspace fallback path exists on no
host, #1699/#1938 census) and moved the resolution onto
``operator_documents.resolve_charter``/``resolve_operator_priorities_text``,
the same functions ``bridge.py``'s mission-assembly block calls. This file
now tests that resolver-level contract directly: charter present -> used
alone; charter absent/unreadable -> the operator's own document stands
alone; both absent -> the caller falls back to the bare goal id (bridge.py's
own ``_base_goal_text = _priorities_text or goal_id``).
"""
import json
from pathlib import Path

from nanobot.runtime.operator_documents import (
    DOCUMENT_SIZE_CAP_BYTES,
    STATE_TEXT,
    resolve_charter,
    resolve_operator_priorities_text,
)

MISSION_TEXT = (
    "eeebot is a resource-aware, self-evolving autonomous agent on a weak eeepc host. "
    "Priority A: archive stale requests. Priority B: create dashboard. "
    "Priority C: enumerate host capabilities. Priority D: commit code improvement."
)


def _base_goal_text(release_root: Path, state_dir: Path, goal_id: str) -> str:
    """The same precedence bridge.py's mission-assembly block uses."""
    charter_res = resolve_charter(release_root)
    if charter_res.state == STATE_TEXT:
        return charter_res.text
    priorities_res = resolve_operator_priorities_text(state_dir)
    priorities_text = priorities_res.text if priorities_res.state == STATE_TEXT else ""
    return priorities_text or goal_id


def _goal_text_json(state_dir: Path, text: str) -> None:
    (state_dir / "goals").mkdir(parents=True, exist_ok=True)
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"text": text}), encoding="utf-8"
    )


class TestGoalTextResolution:
    def test_charter_present_wins_over_operator_document(self, tmp_path):
        """ADR-034 rule 2: when a real release charter exists, it is the
        mission text alone -- never merged with or replaced by the
        operator's own priority document."""
        state_dir = tmp_path / "state"
        release_root = tmp_path / "release"
        release_root.mkdir()
        (release_root / "goals.md").write_text("the release charter", encoding="utf-8")
        _goal_text_json(state_dir, MISSION_TEXT)

        result = _base_goal_text(release_root, state_dir, "goal-bootstrap")

        assert result == "the release charter"

    def test_charter_absent_falls_back_to_operator_document(self, tmp_path):
        """ADR-034 rule 3: charter absent -- the operator's own document
        (state/goals/goal_text.json) stands alone."""
        state_dir = tmp_path / "state"
        release_root = tmp_path / "release"
        release_root.mkdir()  # no goals.md
        _goal_text_json(state_dir, MISSION_TEXT)

        result = _base_goal_text(release_root, state_dir, "goal-bootstrap")

        assert result == MISSION_TEXT

    def test_charter_unreadable_follows_the_same_path_as_absent(self, tmp_path):
        """ADR-034 rule 3: an unreadable charter (here, oversize) is
        indistinguishable from an absent one to this caller -- both fall
        back to the operator's document, never truncated content."""
        state_dir = tmp_path / "state"
        release_root = tmp_path / "release"
        release_root.mkdir()
        (release_root / "goals.md").write_text(
            "x" * (DOCUMENT_SIZE_CAP_BYTES + 1), encoding="utf-8"
        )
        _goal_text_json(state_dir, MISSION_TEXT)

        result = _base_goal_text(release_root, state_dir, "goal-bootstrap")

        assert result == MISSION_TEXT

    def test_last_resort_returns_goal_id(self, tmp_path):
        """Charter and operator document both absent -- the bare goal id."""
        state_dir = tmp_path / "state"
        release_root = tmp_path / "release"
        release_root.mkdir()
        (state_dir / "goals").mkdir(parents=True)

        result = _base_goal_text(release_root, state_dir, "goal-bootstrap")

        assert result == "goal-bootstrap"

    def test_operator_document_with_empty_text_falls_back_to_goal_id(self, tmp_path):
        """An empty "text" field is a valid, priority-free document at the
        resolver level, not a reason to substitute anything else -- but
        this caller (bridge.py) still has nothing usable to show, so it
        falls back to the goal id, same as if the file were absent."""
        state_dir = tmp_path / "state"
        release_root = tmp_path / "release"
        release_root.mkdir()
        _goal_text_json(state_dir, "")

        result = _base_goal_text(release_root, state_dir, "goal-bootstrap")

        assert result == "goal-bootstrap"
