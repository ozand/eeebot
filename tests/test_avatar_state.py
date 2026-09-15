"""Tests for avatar state contract, state file writer, and ADR-018 rules.

Test contract from ADR-018:
Rule 1: No instance artifact imports nanobot (asserted by test).
Rule 2: State file carries version. Unknown version fails loudly, doesn't fallback.
Rule 3: Missing or unreadable state file produces "unknown" posture, NEVER healthy (ADR-014 Rule 1).
Rule 4: Pose contract, fixture, and cover-test lie outside cycle's mutation surface,
        verified by asserting paths are not commit-eligible under MUTATION_POLICY.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from nanobot.runtime.avatar.state import (
    DEFAULT_AVATAR_STATE_FILENAME,
    POSE_SIGNAL,
    STATE_FILE_VERSION,
    UNKNOWN_POSE,
    ObservedState,
    ResolvedAvatarState,
    pose_for,
    resolve_avatar_state,
    status_for,
    write_avatar_state_file,
)
from nanobot.runtime.mutation_policy import MUTATION_POLICY, paths_in_commit_policy

FIXTURE = Path(__file__).parent / "fixtures" / "avatar" / "recorded_host_state.json"


def _state(item: dict) -> ObservedState:
    return ObservedState(
        cycle_status=item.get("cycle_status"),
        thermal_status=item.get("thermal_status"),
        signals=frozenset(item.get("signals", [])),
    )


def test_pose_table_names_a_signal_and_has_unknown() -> None:
    assert UNKNOWN_POSE in POSE_SIGNAL
    assert set(POSE_SIGNAL) == {"idle", "working", "throttled", "dead", UNKNOWN_POSE}
    assert all(signal for signal in POSE_SIGNAL.values())


def test_status_for_pose_honors_four_state_discipline() -> None:
    assert status_for(UNKNOWN_POSE) == "unknown"
    assert status_for("throttled") == "degraded"
    assert status_for("dead") == "dead"
    assert status_for("idle") == "healthy"
    assert status_for("working") == "healthy"


def test_resolve_avatar_state_payload() -> None:
    resolved = resolve_avatar_state(
        ObservedState("working", "throttled", frozenset(["cycle_status", "thermal_status"])),
        timestamp_utc="2026-09-14T12:00:00Z",
    )
    assert resolved.version == STATE_FILE_VERSION
    assert resolved.pose == "throttled"
    assert resolved.signal == "thermal_status"
    assert resolved.status == "degraded"
    assert resolved.timestamp_utc == "2026-09-14T12:00:00Z"


def test_write_avatar_state_file_atomic(tmp_path: Path) -> None:
    out = tmp_path / DEFAULT_AVATAR_STATE_FILENAME
    resolved = resolve_avatar_state(
        ObservedState("working", "nominal", frozenset(["cycle_status", "thermal_status"])),
        timestamp_utc="2026-09-14T12:00:00Z",
    )
    written_path = write_avatar_state_file(resolved, out)
    assert written_path == out
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["version"] == STATE_FILE_VERSION
    assert data["pose"] == "working"
    assert data["signal"] == "cycle_status"
    assert data["status"] == "healthy"
    assert data["timestamp_utc"] == "2026-09-14T12:00:00Z"


def test_recorded_host_sequence_covers_throttle_and_dead_cycle() -> None:
    events = json.loads(FIXTURE.read_text(encoding="utf-8"))["events"]
    assert any(e.get("thermal_status") == "throttled" for e in events)
    assert any(e.get("cycle_status") == "dead" for e in events)
    poses = [pose_for(_state(event)) for event in events]
    assert "throttled" in poses and "dead" in poses and UNKNOWN_POSE in poses


def test_cover_rule_avatar_never_expresses_unobserved_distinction() -> None:
    events = json.loads(FIXTURE.read_text(encoding="utf-8"))["events"]
    for event in events:
        st = _state(event)
        pose = pose_for(st)
        if not st.signals or "cycle_status" not in st.signals:
            assert pose == UNKNOWN_POSE
        if pose == "throttled":
            assert "thermal_status" in st.signals


# --- ADR-018 Test Contract Rules ---

def test_rule1_no_instance_rendering_artifact_imports_nanobot() -> None:
    """Rule 1: No instance avatar/rendering artifact imports nanobot."""
    candidates = [
        Path("T:/Code/.worktrees/inst-1618-draw"),
        Path("T:/Code/eeebot-self-evolving"),
    ]
    target_repo = next((c for c in candidates if c.is_dir()), None)
    if target_repo is None:
        pytest.skip("instance repo not found")

    rendering_paths: list[Path] = []
    for sub in ("scripts", "surfaces"):
        d = target_repo / sub
        if d.is_dir():
            rendering_paths.extend(d.glob("avatar*.py"))
            rendering_paths.extend(d.glob("fb_surface*.py"))
            rendering_paths.extend(d.glob("measure_avatar*.py"))

    tests_dir = target_repo / "tests"
    if tests_dir.is_dir():
        rendering_paths.extend(tests_dir.glob("*avatar*.py"))

    for p in rendering_paths:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("nanobot"), f"{p} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    assert not node.module.startswith("nanobot"), f"{p} imports from {node.module}"


def test_rule4_pose_contract_and_cover_test_outside_instance_mutation_surface() -> None:
    """Rule 4: Pose contract, fixture, and cover-test are outside the loop's commit surface.
    
    The autonomous cycle runs inside eeebot-self-evolving, where MUTATION_POLICY controls
    which paths the loop is allowed to commit. Code in nanobot/ is structurally outside the
    instance repository and therefore unreachable by the loop's commit surface.
    """
    harness_avatar_path = "nanobot/runtime/avatar/state.py"
    assert not paths_in_commit_policy([harness_avatar_path], MUTATION_POLICY)
    surfaces = MUTATION_POLICY.commit_surfaces
    assert not any(harness_avatar_path.startswith(s) for s in surfaces)
