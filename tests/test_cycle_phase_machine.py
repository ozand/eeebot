"""ADR-058 #2180: bounded phase checkpoint and memo durability tests."""
from __future__ import annotations

import json

import pytest

from nanobot.runtime import bridge, phase_checkpoint

_OWNER = "planner-owner-opaque-001"
_CYCLE = "cycle-opaque-001"
_BRANCH = "selfevo/cycle-cycle-opaque-001"
_BASE = "a" * 40
_HEAD = "b" * 40


def _cycle_repo(tmp_path, *, branch_content="candidate\\n", repo_name="repo"):
    import subprocess

    repo = tmp_path / repo_name
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


def test_route_lifecycle_snapshot_recovers_only_verified_completed_checkpoint(tmp_path, monkeypatch):
    import os
    import threading
    from contextlib import contextmanager

    from nanobot.runtime import bridge, intent_journal

    if os.name == "nt":
        locks = {}
        guard = threading.Lock()

        @contextmanager
        def owner_lock(path):
            key = str(path.resolve())
            path.touch(exist_ok=True)
            with guard:
                lock = locks.setdefault(key, threading.Lock())
            with lock:
                yield

        monkeypatch.setattr(intent_journal, "_owner_lock", owner_lock)
        monkeypatch.setattr(intent_journal, "_fsync_directory", lambda _path: None)

    repo, base, head = _cycle_repo(tmp_path, repo_name="eeebot-self-evolving")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Fresh state has no lifecycle; provisioning is not the same as started.
    assert intent_journal.read_route_parent_state(state_dir, "bridge.cycle") is None
    owner = intent_journal.provision_route_parent(state_dir, "bridge.cycle")
    fresh_snapshot = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
    assert fresh_snapshot is not None
    assert fresh_snapshot.owner_id == owner
    assert fresh_snapshot.route_status == "active" and not fresh_snapshot.parent_started

    # A start transition without a matching completion receipt simulates the
    # conservative crash window. The lifecycle reader can identify it but not
    # make it retryable.
    with intent_journal.bridge_parent_context(state_dir, owner, route="bridge.cycle"):
        pass
    _begin(state_dir, repo, base, head, owner=owner, cycle="cycle-recovered")
    running_snapshot = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
    assert running_snapshot is not None and running_snapshot.parent_started
    checkpoint_path = state_dir / "subagent_bridge" / "phase_checkpoint.json"
    checkpoint_before = checkpoint_path.read_bytes()
    owner_rows_before = intent_journal.read_owner_records(state_dir, owner)

    # Running checkpoint means uncertain producer completion; lifecycle evidence
    # may not be rotated, completed, or converted into phase receipts.
    with pytest.raises(intent_journal.JournalUnavailableError, match="checkpoint recovery remains held"):
        bridge._begin_bridge_cycle_parent(state_dir)
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert intent_journal.read_owner_records(state_dir, owner) == owner_rows_before
    still_active = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
    assert still_active is not None and still_active.owner_id == owner and still_active.route_status == "active"
    assert still_active.parent_started and not still_active.phase_receipts
    assert intent_journal.provision_route_parent(state_dir, "bridge.cycle") == owner

    # A valid prefix is still not the complete-cycle terminal: preserve same
    # owner and refuse any new caller dispatch while smoke/integration remain.
    completed = phase_checkpoint.complete_phase(
        state_dir, owner_id=owner, cycle_id="cycle-recovered", branch=_BRANCH,
        base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
    )
    assert completed.status == "complete"
    checkpoint_before_prefix_hold = checkpoint_path.read_bytes()
    owner_rows_before_prefix_hold = intent_journal.read_owner_records(state_dir, owner)
    with pytest.raises(intent_journal.JournalUnavailableError, match="selected bridge phase prefix is incomplete"):
        bridge._begin_bridge_cycle_parent(state_dir)
    same_owner = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
    assert same_owner is not None and same_owner.owner_id == owner and same_owner.route_status == "active"
    assert same_owner.parent_started and same_owner.phase_receipts == ()
    assert checkpoint_path.read_bytes() == checkpoint_before_prefix_hold
    assert intent_journal.read_owner_records(state_dir, owner) == owner_rows_before_prefix_hold
    assert intent_journal.provision_route_parent(state_dir, "bridge.cycle") == owner


@pytest.mark.parametrize("checkpoint_state", ["missing", "corrupt"])
def test_started_owner_without_valid_checkpoint_holds_without_rotation(tmp_path, monkeypatch, checkpoint_state):
    import os
    import threading
    from contextlib import contextmanager

    from nanobot.runtime import bridge, intent_journal

    if os.name == "nt":
        locks = {}
        guard = threading.Lock()

        @contextmanager
        def owner_lock(path):
            key = str(path.resolve())
            path.touch(exist_ok=True)
            with guard:
                lock = locks.setdefault(key, threading.Lock())
            with lock:
                yield

        monkeypatch.setattr(intent_journal, "_owner_lock", owner_lock)
        monkeypatch.setattr(intent_journal, "_fsync_directory", lambda _path: None)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    owner = intent_journal.provision_route_parent(state_dir, "bridge.cycle")
    with intent_journal.bridge_parent_context(state_dir, owner, route="bridge.cycle"):
        pass
    checkpoint_path = state_dir / "subagent_bridge" / "phase_checkpoint.json"
    if checkpoint_state == "corrupt":
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text("{not-json", encoding="utf-8")
    before = checkpoint_path.read_bytes() if checkpoint_path.exists() else None
    with pytest.raises(intent_journal.JournalUnavailableError, match="checkpoint recovery remains held"):
        bridge._begin_bridge_cycle_parent(state_dir)
    assert (checkpoint_path.read_bytes() if checkpoint_path.exists() else None) == before
    snapshot = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
    assert snapshot is not None and snapshot.owner_id == owner and snapshot.route_status == "active"
    assert snapshot.parent_started and not snapshot.phase_receipts
    assert intent_journal.provision_route_parent(state_dir, "bridge.cycle") == owner


@pytest.mark.parametrize("terminal_phase", ["post_execution", "smoke_gate"])
def test_verified_incomplete_bridge_phase_prefix_holds_same_owner_without_dispatch(
    tmp_path, monkeypatch, terminal_phase,
):
    import os
    import threading
    from contextlib import contextmanager

    from nanobot.runtime import bridge, intent_journal

    if os.name == "nt":
        locks = {}
        guard = threading.Lock()

        @contextmanager
        def owner_lock(path):
            key = str(path.resolve())
            path.touch(exist_ok=True)
            with guard:
                lock = locks.setdefault(key, threading.Lock())
            with lock:
                yield

        monkeypatch.setattr(intent_journal, "_owner_lock", owner_lock)
        monkeypatch.setattr(intent_journal, "_fsync_directory", lambda _path: None)

    repo, base, head = _cycle_repo(tmp_path, repo_name="eeebot-self-evolving")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    owner = intent_journal.provision_route_parent(state_dir, "bridge.cycle")
    with intent_journal.bridge_parent_context(state_dir, owner, route="bridge.cycle"):
        pass
    phase_checkpoint.begin_phase(
        state_dir, owner_id=owner, cycle_id="cycle-prefix-hold", branch=_BRANCH,
        base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
    )
    phase_checkpoint.complete_phase(
        state_dir, owner_id=owner, cycle_id="cycle-prefix-hold", branch=_BRANCH,
        base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
    )
    if terminal_phase == "smoke_gate":
        phase_checkpoint.begin_phase(
            state_dir, owner_id=owner, cycle_id="cycle-prefix-hold", branch=_BRANCH,
            base_sha=base, head_sha=head, phase="smoke_gate", repo_root=repo,
        )
        phase_checkpoint.complete_phase(
            state_dir, owner_id=owner, cycle_id="cycle-prefix-hold", branch=_BRANCH,
            base_sha=base, head_sha=head, phase="smoke_gate", repo_root=repo,
            smoke_gate_result={"passed": True, "tested_head": head},
        )
    checkpoint_before = (state_dir / "subagent_bridge" / "phase_checkpoint.json").read_bytes()
    owner_rows_before = intent_journal.read_owner_records(state_dir, owner)

    proposer_calls = []
    planner_calls = []
    executor_spawns = []
    monkeypatch.setattr(bridge.llm_proposer, "maybe_propose", lambda *_a, **_k: proposer_calls.append(1))
    monkeypatch.setattr(bridge, "_run_planning_session", lambda **kwargs: planner_calls.append(kwargs))
    monkeypatch.setattr(bridge, "SubagentManager", lambda **kwargs: executor_spawns.append(kwargs))

    with pytest.raises(intent_journal.JournalUnavailableError, match="selected bridge phase prefix is incomplete"):
        bridge._begin_bridge_cycle_parent(state_dir)
    snapshot = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
    assert snapshot is not None and snapshot.owner_id == owner and snapshot.route_status == "active"
    assert snapshot.parent_started and snapshot.phase_receipts == ()
    assert intent_journal.provision_route_parent(state_dir, "bridge.cycle") == owner
    assert intent_journal.read_owner_records(state_dir, owner) == owner_rows_before
    assert (state_dir / "subagent_bridge" / "phase_checkpoint.json").read_bytes() == checkpoint_before
    assert proposer_calls == [] and planner_calls == [] and executor_spawns == []


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
    for index in range(phase_checkpoint._MAX_MEMOS - len(phase_checkpoint._RESERVED_MEMO_KEYS)):
        phase_checkpoint.memo(
            state_dir, owner_id=_OWNER, key=f"decision_{index}", value=f"value_{index}",
        )
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="memo capacity"):
        phase_checkpoint.memo(state_dir, owner_id=_OWNER, key="last_decision", value="held")
    loaded = phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER)
    assert loaded is not None and len(loaded.memos) == phase_checkpoint._MAX_MEMOS - len(phase_checkpoint._RESERVED_MEMO_KEYS)


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
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="smoke-gate pass"):
        phase_checkpoint.complete_phase(
            state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
            base_sha=base, head_sha=head, phase="smoke_gate", repo_root=repo,
            smoke_gate_result={"passed": False, "tested_head": head},
        )
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="smoke-gate result is missing"):
        phase_checkpoint.complete_phase(
            state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
            base_sha=base, head_sha=head, phase="smoke_gate", repo_root=repo,
        )
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="stale"):
        phase_checkpoint.complete_phase(
            state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
            base_sha=base, head_sha=head, phase="smoke_gate", repo_root=repo,
            smoke_gate_result={"passed": True, "tested_head": "0" * 40},
        )
    completed = phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="smoke_gate", repo_root=repo,
        smoke_gate_result={"passed": True, "tested_head": head},
    )
    assert completed.status == "complete"
    assert completed.memos["smoke_gate_receipt"] == {"passed": True, "tested_head": head}


def test_new_cycle_cannot_replace_running_checkpoint(tmp_path):
    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    checkpoint_path = state_dir / "subagent_bridge" / "phase_checkpoint.json"
    before = checkpoint_path.read_bytes()
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="uncertain settlement"):
        _begin(state_dir, repo, base, head, owner="bridge-owner-next", cycle="next-cycle-opaque")
    assert checkpoint_path.read_bytes() == before
    preserved = phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER)
    assert preserved is not None and preserved.status == "running"


def test_selected_integration_prefix_rotates_only_to_distinct_owner(tmp_path):
    import subprocess

    repo, base, head = _cycle_repo(tmp_path)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True,
        )

    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
    )
    _begin(state_dir, repo, base, head, phase="smoke_gate")
    phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="smoke_gate", repo_root=repo,
        smoke_gate_result={"passed": True, "tested_head": head},
    )
    _begin(state_dir, repo, base, head, phase="integration")
    git("checkout", "main")
    git(
        "merge", "--no-ff", "-m", f"merge: integrate {_BRANCH}",
        "-m", f"Selfevo-Cycle: {_CYCLE}", _BRANCH,
    )
    git("update-ref", "refs/remotes/origin/main", "main")
    main_sha = git("rev-parse", "main").stdout.strip()
    git("checkout", _BRANCH)
    integration = phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="integration", repo_root=repo,
        integration_receipt={"ok": True, "push_confirmed": True, "main_sha_after": main_sha},
    )
    assert integration.status == "complete" and integration.phase == "integration"

    next_cycle = "cycle-next"
    next_branch = f"selfevo/cycle-{next_cycle}"
    git("checkout", "-b", next_branch, "main")
    next_base = git("rev-parse", "main").stdout.strip()
    next_head = git("rev-parse", "HEAD").stdout.strip()
    next_checkpoint = phase_checkpoint.begin_phase(
        state_dir, owner_id="bridge-owner-next", cycle_id=next_cycle,
        branch=next_branch, base_sha=next_base, head_sha=next_head,
        phase="post_execution", repo_root=repo,
    )
    assert next_checkpoint.cycle_id == next_cycle
    assert next_checkpoint.owner_id == "bridge-owner-next"
    assert next_checkpoint.memos == {}


def test_checkpoint_requires_a_distinct_owner_for_a_new_cycle(tmp_path):
    import subprocess

    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
    )
    next_cycle = "cycle-next"
    next_branch = f"selfevo/cycle-{next_cycle}"
    git = subprocess.run
    git(
        ["git", "-C", str(repo), "checkout", "-b", next_branch, "main"],
        check=True, capture_output=True, text=True,
    )
    next_base = git(
        ["git", "-C", str(repo), "rev-parse", "main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    next_head = git(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    rotated = phase_checkpoint.begin_phase(
        state_dir, owner_id="bridge-owner-next", cycle_id=next_cycle,
        branch=next_branch, base_sha=next_base, head_sha=next_head,
        phase="post_execution", repo_root=repo,
    )
    assert rotated.cycle_id == next_cycle


def test_bridge_post_execution_admits_retained_tip_without_replacing_integration_base(tmp_path):
    repo, base, retained_tip = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"

    admitted_tip = bridge._begin_bridge_post_execution_checkpoint(
        state_dir, _OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        integration_base_sha=base, repo_root=repo,
    )

    assert retained_tip != base
    assert admitted_tip == retained_tip
    checkpoint = phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER)
    assert checkpoint is not None
    assert checkpoint.base_sha == base
    assert checkpoint.head_sha == retained_tip
    completed = phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=admitted_tip, phase="post_execution", repo_root=repo,
    )
    assert completed.base_sha == base
    assert completed.head_sha == retained_tip


def test_bridge_post_execution_rejects_wrong_checkout_before_admission(tmp_path):
    import subprocess

    repo, base, _head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)

    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="checkout branch identity changed"):
        bridge._begin_bridge_post_execution_checkpoint(
            state_dir, _OWNER, cycle_id=_CYCLE, branch=_BRANCH,
            integration_base_sha=base, repo_root=repo,
        )
    assert phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER) is None


def test_post_execution_completion_rejects_wrong_checkout_without_advancing(tmp_path):
    import subprocess

    repo, base, admitted_tip = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    bridge._begin_bridge_post_execution_checkpoint(
        state_dir, _OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        integration_base_sha=base, repo_root=repo,
    )
    checkpoint_path = state_dir / "subagent_bridge" / "phase_checkpoint.json"
    before = checkpoint_path.read_bytes()
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)

    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="checkout branch identity changed"):
        phase_checkpoint.complete_phase(
            state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
            base_sha=base, head_sha=admitted_tip, phase="post_execution", repo_root=repo,
        )
    assert checkpoint_path.read_bytes() == before
    held = phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER)
    assert held is not None and held.status == "running" and held.head_sha == admitted_tip


def test_post_execution_completion_rejects_non_descendant_retained_tip_shift(tmp_path):
    import subprocess

    repo, base, admitted_tip = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    bridge._begin_bridge_post_execution_checkpoint(
        state_dir, _OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        integration_base_sha=base, repo_root=repo,
    )
    tree = subprocess.run(
        ["git", "-C", str(repo), "show", "-s", "--format=%T", admitted_tip],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    shifted_tip = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-p", base, "-m", "unrelated retained-tip shift"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", f"refs/heads/{_BRANCH}", shifted_tip],
        check=True, capture_output=True, text=True,
    )
    checkpoint_path = state_dir / "subagent_bridge" / "phase_checkpoint.json"
    before = checkpoint_path.read_bytes()

    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="does not descend from admitted head"):
        phase_checkpoint.complete_phase(
            state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
            base_sha=base, head_sha=admitted_tip, phase="post_execution", repo_root=repo,
        )
    assert checkpoint_path.read_bytes() == before
    held = phase_checkpoint.load_checkpoint(state_dir, owner_id=_OWNER)
    assert held is not None and held.status == "running" and held.head_sha == admitted_tip


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


def test_integration_admission_holds_when_cycle_head_moved_after_smoke_pass(tmp_path):
    import subprocess

    repo, base, head = _cycle_repo(tmp_path)
    state_dir = tmp_path / "state"
    _begin(state_dir, repo, base, head)
    phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
    )
    _begin(state_dir, repo, base, head, phase="smoke_gate")
    phase_checkpoint.complete_phase(
        state_dir, owner_id=_OWNER, cycle_id=_CYCLE, branch=_BRANCH,
        base_sha=base, head_sha=head, phase="smoke_gate", repo_root=repo,
        smoke_gate_result={"passed": True, "tested_head": head},
    )
    checkpoint_path = state_dir / "subagent_bridge" / "phase_checkpoint.json"
    before = checkpoint_path.read_bytes()
    (repo / "tracked.txt").write_text("changed after smoke pass\\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-am", "late change"],
        check=True, capture_output=True, text=True,
    )
    with pytest.raises(phase_checkpoint.CheckpointHeldError, match="cycle branch head changed"):
        _begin(state_dir, repo, base, head, phase="integration")
    assert checkpoint_path.read_bytes() == before


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
