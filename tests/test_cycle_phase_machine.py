"""ADR-058 #2180: bounded phase checkpoint and memo durability tests."""
from __future__ import annotations

import json

import pytest

from nanobot.runtime import phase_checkpoint

_OWNER = "planner-owner-opaque-001"
_CYCLE = "cycle-opaque-001"
_BRANCH = "selfevo/cycle-cycle-opaque-001"
_BASE = "a" * 40
_HEAD = "b" * 40


def _cycle_repo(tmp_path, *, branch_content="candidate\\n"):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    git("init", "-b", "main")
    git("config", "user.email", "checkpoint@test.local")
    git("config", "user.name", "checkpoint-test")
    (repo / "tracked.txt").write_text("base\\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD").stdout.strip()
    git("checkout", "-b", _BRANCH)
    (repo / "tracked.txt").write_text(branch_content, encoding="utf-8")
    git("commit", "-am", "candidate")
    head = git("rev-parse", "HEAD").stdout.strip()
    return repo, base, head


def _begin(state_dir, repo, base, head, phase="post_execution", *, owner=_OWNER, cycle=_CYCLE):
    return phase_checkpoint.begin_phase(
        state_dir, owner_id=owner, cycle_id=cycle, branch=_BRANCH,
        base_sha=base, head_sha=head, phase=phase, repo_root=repo,
    )


def test_phase_checkpoint_and_memo_idempotency(tmp_path):
    """A restart preserves bounded first-write-wins data and holds running work."""
    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    first = _begin(state_dir, repo, base, head)
    assert first.status == "running"
    assert phase_checkpoint.memo(state_dir, owner_id=_OWNER, key="gate_result", value="passed") == "passed"
    assert phase_checkpoint.memo(state_dir, owner_id=_OWNER, key="gate_result", value="changed") == "passed"

    # New call/process reloads the durable owner-bound phase, not an empty default.
    loaded = phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER)
    assert loaded is not None
    assert loaded.memos == {"gate_result": "passed"}
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="uncertain settlement"):
        _begin(state_dir, repo, base, head)

    raw = json.loads((state_dir / "subagent_bridge" / "phase_checkpoint.json").read_text())
    assert raw["schema"] == "bridge-phase-checkpoint-v1"
    assert raw["status"] == "running"
    assert raw["owner_id"] == _OWNER


def test_checkpoint_admission_fsync_failure_blocks_effect(tmp_path, monkeypatch):
    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    effects = []

    def fail_fsync(_fd):
        raise OSError("injected checkpoint fsync failure")

    monkeypatch.setattr(phase_checkpoint.os, "fsync", fail_fsync)
    try:
        _begin(state_dir, repo, base, head)
    except phase_checkpoint.CheckpointError as exc:
        assert "persistence failed" in str(exc)
    else:
        effects.append("admitted")

    assert effects == []
    assert not (state_dir / "subagent_bridge" / "phase_checkpoint.json").exists()


def test_crash_after_effect_before_phase_receipt_does_not_repeat_effect(tmp_path):
    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    effects = tmp_path / "local_effect.txt"

    # Process 1 persists admission before its external effect.
    _begin(state_dir, repo, base, head)
    with effects.open("a", encoding="utf-8") as handle:
        handle.write("effect\n")

    # Simulated crash: no complete_phase receipt. A restarted parent reloads
    # the same stable owner and must hold rather than re-admit the effect.
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="uncertain settlement"):
        _begin(state_dir, repo, base, head)
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect"]


def test_phase_schema_and_memos_are_bounded_and_first_write_wins(tmp_path):
    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    assert phase_checkpoint.memo(state_dir, owner_id=_OWNER, key="decision", value="approved") == "approved"
    with pytest.raises(phase_checkpoint.CheckpointError, match="allowlisted scalar"):
        phase_checkpoint.memo(state_dir, owner_id=_OWNER, key="payload", value="x" * 129)
    with pytest.raises(phase_checkpoint.CheckpointError, match="allowlisted scalar"):
        phase_checkpoint.memo(state_dir, owner_id=_OWNER, key="large", value=1 << 129)
    with pytest.raises(phase_checkpoint.CheckpointError, match="unsupported checkpoint phase"):
        _begin(state_dir, repo, base, head, phase="provider_retry")

    reloaded = phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER)
    assert reloaded is not None and reloaded.memos == {"decision": "approved"}
    assert phase_checkpoint.memo(state_dir, owner_id=_OWNER, key="decision", value="replaced") == "approved"


def test_dangling_checkpoint_symlink_is_held(tmp_path):
    checkpoint_path = tmp_path / "subagent_bridge" / "phase_checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    target = checkpoint_path.parent / "missing-target.json"
    try:
        checkpoint_path.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    assert checkpoint_path.is_symlink()
    assert not checkpoint_path.exists()
    with pytest.raises(phase_checkpoint.CheckpointError, match="unsafe checkpoint file"):
        phase_checkpoint.load_checkpoint(tmp_path, owner_id=_OWNER)


def test_memo_capacity_reserves_integration_receipt_slot(tmp_path):
    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    for index in range(phase_checkpoint._MAX_MEMOS - 1):
        phase_checkpoint.memo(
            state_dir, owner_id=_OWNER, key=f"decision_{index}", value=f"value_{index}",
        )
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="memo capacity"):
        phase_checkpoint.memo(state_dir, owner_id=_OWNER, key="last_decision", value="held")
    loaded = phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER)
    assert loaded is not None and len(loaded.memos) == phase_checkpoint._MAX_MEMOS - 1


def test_corrupt_or_different_owner_checkpoint_is_held(tmp_path):
    checkpoint_path = tmp_path / "subagent_bridge" / "phase_checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text("{truncated", encoding="utf-8")
    with pytest.raises(phase_checkpoint.CheckpointError, match="corrupt"):
        phase_checkpoint.load_checkpoint(tmp_path, owner_id=_OWNER)

    checkpoint_path.unlink()
    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="different durable parent"):
        phase_checkpoint.load_checkpoint(state_dir, owner_id="other-owner-opaque")
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="uncertain settlement"):
        _begin(state_dir, repo, base, head)


def test_completed_phase_advances_once_and_does_not_reset_memos(tmp_path):
    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    phase_checkpoint.memo(state_dir, owner_id=_OWNER, key="receipt", value="verified")
    phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
    )
    next_phase = _begin(state_dir, repo, base, head, phase="smoke_gate")
    assert next_phase.status == "running"
    assert next_phase.memos == {"receipt": "verified"}
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="uncertain settlement"):
        _begin(state_dir, repo, base, head, phase="smoke_gate")


@pytest.mark.parametrize("replacement_owner", [_OWNER, "replacement-owner"])
def test_new_cycle_cannot_replace_checkpoint_before_all_phases_complete(tmp_path, replacement_owner):
    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    phase_checkpoint.memo(state_dir, owner_id=_OWNER, key="receipt", value="persisted")
    phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
    )
    checkpoint_path = state_dir / "subagent_bridge" / "phase_checkpoint.json"
    before = checkpoint_path.read_bytes()
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="previous cycle has unfinished phases"):
        _begin(state_dir, repo, base, head, owner=replacement_owner, cycle="next-cycle-opaque")
    assert checkpoint_path.read_bytes() == before
    preserved = phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER)
    assert preserved is not None and preserved.memos == {"receipt": "persisted"}


def test_post_execution_completion_records_clean_committed_descendant(tmp_path):
    import subprocess

    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    (repo / "tracked.txt").write_text("producer output\\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-am", "producer output"],
        check=True, capture_output=True, text=True,
    )
    new_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    completed = phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
    )
    assert completed.status == "complete" and completed.head_sha == new_head
    assert completed.head_sha != head
    next_phase = _begin(state_dir, repo, base, new_head, phase="smoke_gate")
    assert next_phase.status == "running" and next_phase.head_sha == new_head


def test_post_execution_completion_holds_uncommitted_tree_without_receipt(tmp_path):
    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    (repo / "tracked.txt").write_text("uncommitted producer output\\n", encoding="utf-8")
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="uncommitted changes"):
        phase_checkpoint.complete_phase(
            state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
            base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
        )
    held = phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER)
    assert held is not None and held.status == "running" and held.head_sha == head


def test_phase_completion_requires_exact_local_branch_head(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    git("init", "-b", "main")
    git("config", "user.email", "checkpoint@test.local")
    git("config", "user.name", "checkpoint-test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD").stdout.strip()
    git("checkout", "-b", _BRANCH)
    (repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    git("commit", "-am", "candidate")
    head = git("rev-parse", "HEAD").stdout.strip()
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)

    phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
    )
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="branch head changed"):
        phase_checkpoint.verify_local_branch_head(
            repo, branch=_BRANCH, expected_base=base, expected_head=base,
        )
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="unavailable"):
        phase_checkpoint.verify_local_branch_head(
            repo, branch="missing", expected_base=base, expected_head=head,
        )
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="checkout branch identity changed"):
        phase_checkpoint.verify_local_branch_head(
            repo, branch=_BRANCH, expected_base=base, expected_head=head,
        )
