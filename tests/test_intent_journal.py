"""Contract tests for the strict per-owner intent journal (ADR-056)."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

import nanobot.runtime.intent_journal as journal
from nanobot.runtime.intent_journal import (
    MAX_OWNER_BYTES,
    MAX_OWNER_RECORDS,
    JournalCorruptError,
    JournalUnavailableError,
    admit_attempt,
    create_parent,
    operation_context,
    read_owner_records,
    record_returned,
    reset_parent_context,
    set_parent_context,
)

_ORIGINAL_OWNER_LOCK = journal._owner_lock
_ORIGINAL_DIRECTORY_FSYNC = journal._fsync_directory


@pytest.fixture(autouse=True)
def _test_backend(monkeypatch):
    """Model the supported POSIX primitives for logic tests on Windows.

    Windows itself is separately verified to fail closed because this module
    requires directory fsync and flock; this fake is not durability evidence.
    """
    if os.name == "nt":
        locks: dict[str, threading.Lock] = {}
        locks_guard = threading.Lock()

        @contextmanager
        def fake_owner_lock(path: Path):
            key = str(path.resolve())
            path.touch(exist_ok=True)
            with locks_guard:
                lock = locks.setdefault(key, threading.Lock())
            with lock:
                yield

        monkeypatch.setattr(journal, "_owner_lock", fake_owner_lock)
        monkeypatch.setattr(journal, "_fsync_directory", lambda path: None)


def _ids() -> tuple[str, str, str]:
    return str(uuid4()), str(uuid4()), str(uuid4())


def _intent(root: Path, owner: str, operation: str, attempt: str, **kwargs) -> None:
    admit_attempt(
        root, owner_id=owner, operation_id=operation, attempt_id=attempt,
        component=kwargs.pop("component", "agent"),
        target=kwargs.pop("target", "provider.chat"), _test_only=True, **kwargs,
    )


# @lat: [[architecture#Intent-before-effect evidence]]
def test_intent_and_terminal_are_durable_metadata_only(tmp_path: Path) -> None:
    owner, operation, attempt = _ids()
    _intent(tmp_path, owner, operation, attempt)
    rows = read_owner_records(tmp_path, owner)
    assert [row["event"] for row in rows] == ["intent"]
    record_returned(
        tmp_path, owner_id=owner, operation_id=operation, attempt_id=attempt,
        component="agent", target="provider.chat",
    )
    rows = read_owner_records(tmp_path, owner)
    assert [row["event"] for row in rows] == ["intent", "terminal"]
    encoded = (tmp_path / "intent-journal" / f"{owner}.jsonl").read_text(encoding="utf-8")
    for forbidden in ("prompt", "arguments", "secret", "payload", "fingerprint"):
        assert forbidden not in encoded.lower()


def test_intent_must_be_admitted_before_another_owner_effect(tmp_path: Path) -> None:
    owner, operation, attempt = _ids()
    _intent(tmp_path, owner, operation, attempt)
    with pytest.raises(JournalUnavailableError, match="durable parent-resolution"):
        _intent(tmp_path, owner, str(uuid4()), str(uuid4()))
    other_owner, other_operation, other_attempt = _ids()
    _intent(tmp_path, other_owner, other_operation, other_attempt)
    assert len(read_owner_records(tmp_path, other_owner)) == 1


def test_terminal_requires_exact_unsettled_intent_and_target(tmp_path: Path) -> None:
    owner, operation, attempt = _ids()
    _intent(tmp_path, owner, operation, attempt, target="tool.dispatch")
    with pytest.raises(JournalUnavailableError, match="does not match"):
        record_returned(
            tmp_path, owner_id=owner, operation_id=operation, attempt_id=str(uuid4()),
            component="agent", target="tool.dispatch",
        )
    with pytest.raises(JournalUnavailableError, match="differs"):
        record_returned(
            tmp_path, owner_id=owner, operation_id=operation, attempt_id=attempt,
            component="agent", target="provider.chat",
        )
    record_returned(
        tmp_path, owner_id=owner, operation_id=operation, attempt_id=attempt,
        component="agent", target="tool.dispatch",
    )
    with pytest.raises(JournalUnavailableError, match="does not match"):
        record_returned(
            tmp_path, owner_id=owner, operation_id=operation, attempt_id=attempt,
            component="agent", target="tool.dispatch",
        )


def test_corrupt_truncated_and_missing_records_never_mean_empty(tmp_path: Path) -> None:
    owner, operation, attempt = _ids()
    with pytest.raises(JournalUnavailableError, match="missing"):
        read_owner_records(tmp_path, owner)
    _intent(tmp_path, owner, operation, attempt)
    journal = tmp_path / "intent-journal" / f"{owner}.jsonl"
    with journal.open("ab") as stream:
        stream.write(b'{"partial":')
    with pytest.raises(JournalCorruptError, match="truncated"):
        read_owner_records(tmp_path, owner)
    assert journal.read_bytes().endswith(b'{"partial":')


# @lat: [[architecture#Intent-before-effect evidence]]
def test_byte_limit_corruption_is_preserved_without_large_decode(tmp_path: Path) -> None:
    owner = str(uuid4())
    path = tmp_path / "intent-journal" / f"{owner}.jsonl"
    path.parent.mkdir()
    path.write_bytes(b"x" * (MAX_OWNER_BYTES + 1))
    with pytest.raises(JournalCorruptError, match="byte limit"):
        read_owner_records(tmp_path, owner)
    assert path.stat().st_size == MAX_OWNER_BYTES + 1


# @lat: [[architecture#Intent-before-effect evidence]]
def test_owner_lock_serializes_separate_processes(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows test backend only models thread locking")
    path = tmp_path / "owner.lock"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    script = (
        "import sys,time; from pathlib import Path; "
        "from nanobot.runtime.intent_journal import _owner_lock; "
        "p=Path(sys.argv[1]); ready=Path(sys.argv[2]); release=Path(sys.argv[3]); "
        "\nwith _owner_lock(p):\n ready.write_text('1')\n "
        "\n while not release.exists(): time.sleep(0.01)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    proc = subprocess.Popen([sys.executable, "-c", script, str(path), str(ready), str(release)], env=env)
    try:
        for _ in range(500):
            if ready.exists():
                break
            if proc.poll() is not None:
                pytest.fail("lock holder process exited before readiness")
            __import__("time").sleep(0.01)
        assert ready.exists()
        fd = os.open(path, os.O_RDWR)
        try:
            import fcntl
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        release.write_text("1")
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            release.write_text("1")
            proc.terminate()
            proc.wait(timeout=5)


# @lat: [[architecture#Intent-before-effect evidence]]
def test_child_death_preserves_intent_and_terminal_without_replay(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("requires POSIX flock and directory fsync")
    owner, operation, attempt = _ids()
    ready = tmp_path / "ready"
    script = """
import sys, time
from pathlib import Path
from nanobot.runtime.intent_journal import admit_attempt, record_returned, _owner_lock
root = Path(sys.argv[1]); owner, operation, attempt = sys.argv[2:5]
ready = Path(sys.argv[6]); terminal = sys.argv[5] == "terminal"
admit_attempt(root, owner_id=owner, operation_id=operation, attempt_id=attempt, component="agent", target="provider.chat")
if terminal:
    record_returned(root, owner_id=owner, operation_id=operation, attempt_id=attempt, component="agent", target="provider.chat")
with _owner_lock(root / "intent-journal" / f"{owner}.lock"):
    ready.write_text("ready")
    while True:
        time.sleep(1)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    for terminal in ("intent", "terminal"):
        ready.unlink(missing_ok=True)
        proc = subprocess.Popen([
            sys.executable, "-c", script, str(tmp_path), owner, operation,
            attempt, terminal, str(ready),
        ], env=env)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                if proc.poll() is not None:
                    pytest.fail("child exited before holding its owner lock")
                time.sleep(0.01)
            assert ready.exists()
            proc.kill()
            assert proc.wait(timeout=5) is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        rows = read_owner_records(tmp_path, owner)
        expected = ["intent", "terminal"] if terminal == "terminal" else ["intent"]
        assert [row["event"] for row in rows] == expected
        with pytest.raises(JournalUnavailableError, match="durable parent-resolution"):
            _intent(tmp_path, owner, str(uuid4()), str(uuid4()))
        # Isolate the second subprocess run under a distinct stable owner.
        owner, operation, attempt = _ids()


# @lat: [[architecture#Intent-before-effect evidence]]
def test_terminal_fsync_failure_leaves_only_unsettled_intent(tmp_path: Path, monkeypatch) -> None:
    owner, operation, attempt = _ids()
    _intent(tmp_path, owner, operation, attempt)
    real_open = os.open
    real_fsync = os.fsync
    real_replace = os.replace
    temp_fd: int | None = None
    injected = False
    replaced: list[Path] = []
    journal_path = tmp_path / "intent-journal" / f"{owner}.jsonl"
    temp_name = f".{journal_path.name}.tmp"

    def observe_open(path, flags, *args, **kwargs):
        nonlocal temp_fd
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path).name == temp_name:
            temp_fd = fd
        return fd

    def fail_terminal_record_fsync(fd: int) -> None:
        nonlocal injected
        if fd == temp_fd:
            injected = True
            raise OSError("injected terminal record fsync failure before replace")
        real_fsync(fd)

    def observe_replace(source, target) -> None:
        if Path(target) == journal_path:
            replaced.append(Path(source))
        real_replace(source, target)

    monkeypatch.setattr(journal.os, "open", observe_open)
    monkeypatch.setattr(journal.os, "fsync", fail_terminal_record_fsync)
    monkeypatch.setattr(journal.os, "replace", observe_replace)
    with pytest.raises(JournalUnavailableError, match="not verified"):
        record_returned(
            tmp_path, owner_id=owner, operation_id=operation, attempt_id=attempt,
            component="agent", target="provider.chat",
        )
    monkeypatch.setattr(journal.os, "open", real_open)
    monkeypatch.setattr(journal.os, "fsync", real_fsync)
    monkeypatch.setattr(journal.os, "replace", real_replace)
    assert temp_fd is not None
    assert injected
    assert replaced == []
    assert not (journal_path.parent / temp_name).exists()
    # The terminal temp-file sync failed before replace; the persisted file
    # remains intent-only, and its owner stays held for parent resolution.
    rows = read_owner_records(tmp_path, owner)
    assert [row["event"] for row in rows] == ["intent"]
    with pytest.raises(JournalUnavailableError, match="durable parent-resolution"):
        _intent(tmp_path, owner, str(uuid4()), str(uuid4()))


def test_append_fsync_failure_rolls_back_unverified_record(tmp_path: Path, monkeypatch) -> None:
    owner, operation, attempt = _ids()
    path = tmp_path / "intent-journal" / f"{owner}.jsonl"
    path.parent.mkdir()
    path.touch()
    before = path.read_bytes()
    row = journal._record(owner, operation, attempt, "intent", "agent", "provider.chat", None)
    real_fsync = os.fsync
    calls = 0

    def fail_first_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(journal.os, "fsync", fail_first_fsync)
    with pytest.raises(JournalUnavailableError, match="not verified"):
        journal._append(path, row, [])
    assert path.read_bytes() == before


# @lat: [[architecture#Intent-before-effect evidence]]
def test_directory_fsync_failure_denies_admission_and_holds_visible_intent(tmp_path: Path, monkeypatch) -> None:
    if os.name == "nt":
        pytest.skip("Windows test backend replaces directory sync")
    owner, operation, attempt = _ids()
    _intent(tmp_path, owner, operation, attempt)
    real_sync = journal._fsync_directory
    injected = False
    journal_path = tmp_path / "intent-journal" / f"{owner}.jsonl"

    def fail_after_replace(path: Path) -> None:
        nonlocal injected
        # Inject only once the terminal is visible in the target journal. This
        # targets the post-replace sync without depending on setup sync counts.
        if b'"event":"terminal"' in journal_path.read_bytes():
            injected = True
            raise OSError("injected post-replace directory fsync failure")
        real_sync(path)

    monkeypatch.setattr(journal, "_fsync_directory", fail_after_replace)
    with pytest.raises(JournalUnavailableError, match="not verified"):
        record_returned(
            tmp_path, owner_id=owner, operation_id=operation, attempt_id=attempt,
            component="agent", target="provider.chat",
        )
    monkeypatch.setattr(journal, "_fsync_directory", real_sync)
    assert injected
    # The atomic replace succeeded before directory sync failed. The API
    # denied result release, but the complete terminal is visible in-process.
    # A fresh process also reopens it and sees the same held owner; neither
    # assertion claims power-loss or crash-durability after the failed fsync.
    assert [row["event"] for row in read_owner_records(tmp_path, owner)] == ["intent", "terminal"]
    fresh_operation, fresh_attempt = str(uuid4()), str(uuid4())
    with pytest.raises(JournalUnavailableError, match="durable parent-resolution"):
        admit_attempt(
            tmp_path, owner_id=owner, operation_id=fresh_operation,
            attempt_id=fresh_attempt, component="agent", target="provider.chat",
        )
    script = """
import sys
from pathlib import Path
from nanobot.runtime.intent_journal import JournalUnavailableError, admit_attempt, read_owner_records
root = Path(sys.argv[1]); owner, operation, attempt = sys.argv[2:5]
assert [row["event"] for row in read_owner_records(root, owner)] == ["intent", "terminal"]
try:
    admit_attempt(root, owner_id=owner, operation_id=operation, attempt_id=attempt, component="agent", target="provider.chat")
except JournalUnavailableError as exc:
    assert "durable parent-resolution" in str(exc)
else:
    raise SystemExit("fresh process admitted an unresolved owner")
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    subprocess.run([
        sys.executable, "-c", script, str(tmp_path), owner,
        str(uuid4()), str(uuid4()),
    ], env=env, check=True, capture_output=True, text=True, timeout=10)


# @lat: [[architecture#Intent-before-effect evidence]]
def test_stale_temp_file_holds_owner_without_overwrite(tmp_path: Path) -> None:
    owner, operation, attempt = _ids()
    _intent(tmp_path, owner, operation, attempt)
    path = tmp_path / "intent-journal" / f".{owner}.jsonl.tmp"
    path.write_bytes(b"untrusted temporary state")
    journal_path = tmp_path / "intent-journal" / f"{owner}.jsonl"
    with pytest.raises(JournalUnavailableError, match="not verified"):
        record_returned(
            tmp_path, owner_id=owner, operation_id=operation, attempt_id=attempt,
            component="agent", target="provider.chat",
        )
    assert path.read_bytes() == b"untrusted temporary state"
    with pytest.raises(JournalUnavailableError, match="temp file"):
        read_owner_records(tmp_path, owner)
    assert journal_path.exists()


# @lat: [[architecture#Intent-before-effect evidence]]
def test_temp_file_for_another_owner_does_not_block_healthy_partition(tmp_path: Path) -> None:
    owner, operation, attempt = _ids()
    other_owner = str(uuid4())
    _intent(tmp_path, owner, operation, attempt)
    other_path = tmp_path / "intent-journal" / f".{other_owner}.jsonl.tmp"
    other_path.write_bytes(b"orphan from unrelated owner")
    assert len(read_owner_records(tmp_path, owner)) == 1
    assert other_path.read_bytes() == b"orphan from unrelated owner"


def test_corruption_is_owner_scoped_and_preserved(tmp_path: Path) -> None:
    owner_a, op_a, attempt_a = _ids()
    owner_b, op_b, attempt_b = _ids()
    _intent(tmp_path, owner_a, op_a, attempt_a)
    _intent(tmp_path, owner_b, op_b, attempt_b)
    path_a = tmp_path / "intent-journal" / f"{owner_a}.jsonl"
    path_a.write_bytes(b"not-json\n")
    with pytest.raises(JournalCorruptError):
        read_owner_records(tmp_path, owner_a)
    assert read_owner_records(tmp_path, owner_b)[0]["owner_id"] == owner_b
    assert path_a.read_bytes() == b"not-json\n"


def test_uuid_identity_and_metadata_are_allowlisted(tmp_path: Path) -> None:
    owner, operation, attempt = _ids()
    with pytest.raises(ValueError, match="canonical UUID"):
        _intent(tmp_path, "not-an-id", operation, attempt)
    with pytest.raises(ValueError, match="allowlisted"):
        _intent(tmp_path, owner, operation, attempt, component="private prompt")
    with pytest.raises(ValueError, match="allowlisted"):
        _intent(tmp_path, owner, operation, attempt, target="provider chat with secret")
    assert not (tmp_path / "intent-journal" / f"{owner}.jsonl").exists()


def test_capacity_limits_fail_closed_without_eviction(tmp_path: Path) -> None:
    owner = str(uuid4())
    path = tmp_path / "intent-journal" / f"{owner}.jsonl"
    path.parent.mkdir()
    path.touch()
    records = []
    for _ in range(MAX_OWNER_RECORDS // 3):
        operation, attempt = str(uuid4()), str(uuid4())
        intent = journal._record(owner, operation, attempt, "intent", "agent", "provider.chat", None)
        journal._append(path, intent, records)
        records.append(intent)
        terminal = journal._record(owner, operation, attempt, "terminal", "agent", "provider.chat", None)
        journal._append(path, terminal, records)
        records.append(terminal)
        consumed = journal._record(owner, operation, attempt, "consumed", "agent", "provider.chat", None)
        journal._append(path, consumed, records)
        records.append(consumed)
    before = path.read_bytes()
    next_row = journal._record(owner, str(uuid4()), str(uuid4()), "intent", "agent", "provider.chat", None)
    with pytest.raises(JournalUnavailableError, match="capacity"):
        journal._append(path, next_row, records)
    assert path.read_bytes() == before
    assert len(read_owner_records(tmp_path, owner)) == (MAX_OWNER_RECORDS // 3) * 3
    assert MAX_OWNER_BYTES == 1_048_576


# @lat: [[architecture#Intent-before-effect evidence]]
@pytest.mark.asyncio
async def test_real_planner_manager_journals_provider_retry_attempts(tmp_path: Path, monkeypatch) -> None:
    """The production planner parent and manager wrap real provider retry attempts."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMProvider, LLMResponse

    state_root = tmp_path / "state"
    state_root.mkdir()
    owner = journal.provision_route_parent(state_root, "planner")
    parent_context = journal.bridge_parent_context(state_root, owner)
    if os.name == "nt":
        locks: dict[str, threading.Lock] = {}
        guard = threading.Lock()

        @contextmanager
        def fake_owner_lock(path: Path):
            path.touch(exist_ok=True)
            with guard:
                lock = locks.setdefault(str(path.resolve()), threading.Lock())
            with lock:
                yield

        monkeypatch.setattr(journal, "_owner_lock", fake_owner_lock)
        monkeypatch.setattr(journal, "_fsync_directory", lambda _path: None)
        monkeypatch.setattr("nanobot.agent.subagent.intent_journal._owner_lock", fake_owner_lock)
        monkeypatch.setattr("nanobot.agent.subagent.intent_journal._fsync_directory", lambda _path: None)
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "calls"))
    delays: list[int] = []

    async def fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", fake_sleep)

    class PlannerProvider(LLMProvider):
        def __init__(self):
            super().__init__()
            self.responses = [
                LLMResponse(content="429 rate limit", finish_reason="error"),
                LLMResponse(content='{"plan":"bounded","iterations_planned":1}'),
            ]
            self.physical_calls = 0

        async def chat(self, **kwargs):
            self.physical_calls += 1
            return self.responses.pop(0)

        def get_default_model(self) -> str:
            return "test-model"

    provider = PlannerProvider()
    manager = SubagentManager(
        provider=provider, workspace=tmp_path, bus=MessageBus(),
        telemetry_component="planner", planner_deadline=__import__("time").monotonic() + 600.0,
        tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}",
        intent_state_root=state_root, intent_owner_id=owner,
    )
    await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    with parent_context:
        await __import__("asyncio").gather(*manager._running_tasks.values(), return_exceptions=True)

    rows = read_owner_records(state_root, owner)
    assert provider.physical_calls == 2
    assert delays == [1]
    assert [row["event"] for row in rows] == ["intent", "terminal", "consumed"] * 2
    attempts = [row for row in rows if row["event"] == "intent"]
    assert len({row["attempt_id"] for row in attempts}) == 2
    assert len({row["operation_id"] for row in attempts}) == 1
    parent = __import__("json").loads(
        (state_root / "intent-journal" / f"{owner}.parent.json").read_text()
    )
    assert parent["active_operation_id"] is None
    assert parent["last_operation_id"] == attempts[0]["operation_id"]


@pytest.mark.asyncio
# @lat: [[architecture#Intent-before-effect evidence]]
async def test_real_planner_manager_journals_tool_dispatch_between_provider_turns(
    tmp_path: Path, monkeypatch,
) -> None:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    state_root = tmp_path / "state"
    state_root.mkdir()
    owner = journal.provision_route_parent(state_root, "planner")
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "calls"))
    tool_results: list[str] = []

    class PlannerProvider(LLMProvider):
        def __init__(self):
            super().__init__()
            self.responses = [
                LLMResponse(content="", tool_calls=[ToolCallRequest(
                    id="read-1", name="read_file", arguments={"path": "fixture"},
                )]),
                LLMResponse(content='{"plan":"bounded","iterations_planned":1}'),
            ]
            self.physical_calls = 0

        async def chat(self, **kwargs):
            self.physical_calls += 1
            return self.responses.pop(0)

        def get_default_model(self) -> str:
            return "test-model"

    class ResultRegistry(ToolRegistry):
        async def execute(self, name, params):
            tool_results.append(name)
            return "fixture result"

    monkeypatch.setattr("nanobot.agent.subagent.ToolRegistry", ResultRegistry)
    manager = SubagentManager(
        provider=PlannerProvider(), workspace=tmp_path, bus=MessageBus(),
        telemetry_component="planner", planner_deadline=__import__("time").monotonic() + 600.0,
        tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}",
        intent_state_root=state_root, intent_owner_id=owner,
    )
    await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    await __import__("asyncio").gather(*manager._running_tasks.values(), return_exceptions=True)

    rows = read_owner_records(state_root, owner)
    assert tool_results == ["read_file"]
    assert [row["target"] for row in rows if row["event"] == "intent"] == [
        "provider.chat", "tool.dispatch", "provider.chat",
    ]
    assert [row["event"] for row in rows] == ["intent", "terminal", "consumed"] * 3
    assert not any("path" in row or "fixture" in row for row in rows)
    assert len({row["operation_id"] for row in rows if row["event"] == "intent"}) == 3


@pytest.mark.asyncio
# @lat: [[architecture#Intent-before-effect evidence]]
async def test_planner_tool_cancellation_leaves_attempt_unknown_and_held(
    tmp_path: Path, monkeypatch,
) -> None:
    import asyncio

    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    state_root = tmp_path / "state"
    state_root.mkdir()
    owner = journal.provision_route_parent(state_root, "planner")
    other_owner = str(uuid4())
    create_parent(state_root, other_owner)

    class Provider(LLMProvider):
        async def chat(self, **kwargs):
            return LLMResponse(content="", tool_calls=[ToolCallRequest(
                id="cancelled-tool", name="read_file", arguments={"path": "fixture"},
            )])

        def get_default_model(self) -> str:
            return "test-model"

    class CancelledRegistry(ToolRegistry):
        async def execute(self, name, params):
            raise asyncio.CancelledError

    monkeypatch.setattr("nanobot.agent.subagent.ToolRegistry", CancelledRegistry)
    manager = SubagentManager(
        provider=Provider(), workspace=tmp_path, bus=MessageBus(),
        telemetry_component="planner", planner_deadline=__import__("time").monotonic() + 600.0,
        tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}",
        intent_state_root=state_root, intent_owner_id=owner,
    )
    await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    results = await asyncio.gather(*manager._running_tasks.values(), return_exceptions=True)

    assert any(isinstance(result, asyncio.CancelledError) for result in results)
    rows = read_owner_records(state_root, owner)
    assert [row["target"] for row in rows if row["event"] == "intent"] == [
        "provider.chat", "tool.dispatch",
    ]
    assert [row["event"] for row in rows] == [
        "intent", "terminal", "consumed", "intent",
    ]
    parent = __import__("json").loads(
        (state_root / "intent-journal" / f"{owner}.parent.json").read_text()
    )
    assert parent["active_operation_id"] == rows[-1]["operation_id"]

    if os.name == "nt":
        locks: dict[str, threading.Lock] = {}
        guard = threading.Lock()

        @contextmanager
        def fake_owner_lock(path: Path):
            path.touch(exist_ok=True)
            with guard:
                lock = locks.setdefault(str(path.resolve()), threading.Lock())
            with lock:
                yield

        monkeypatch.setattr(journal, "_owner_lock", fake_owner_lock)
        monkeypatch.setattr(journal, "_fsync_directory", lambda _path: None)
    assert journal.provision_route_parent(state_root, "planner") == owner
    with pytest.raises(JournalUnavailableError):
        with journal.bridge_parent_context(state_root, owner):
            pytest.fail("cancelled planner parent must remain held")
    assert read_owner_records(state_root, other_owner) == []


@pytest.mark.asyncio
# @lat: [[architecture#Intent-before-effect evidence]]
async def test_tool_intent_persistence_failure_prevents_dispatch(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    state_root = tmp_path / "state"
    state_root.mkdir()
    owner = journal.provision_route_parent(state_root, "planner")
    executed: list[str] = []

    class Provider(LLMProvider):
        async def chat(self, **kwargs):
            return LLMResponse(content="", tool_calls=[ToolCallRequest(
                id="blocked-tool", name="read_file", arguments={"path": "fixture"},
            )])

        def get_default_model(self) -> str:
            return "test-model"

    class TrackingRegistry(ToolRegistry):
        async def execute(self, name, params):
            executed.append(name)
            return "unexpected"

    monkeypatch.setattr("nanobot.agent.subagent.ToolRegistry", TrackingRegistry)
    real_append = journal._append

    def fail_tool_intent(path, row, records):
        if row["event"] == "intent" and row["target"] == "tool.dispatch":
            raise JournalUnavailableError("injected tool intent persistence failure")
        real_append(path, row, records)

    monkeypatch.setattr(journal, "_append", fail_tool_intent)
    manager = SubagentManager(
        provider=Provider(), workspace=tmp_path, bus=MessageBus(),
        telemetry_component="planner", planner_deadline=__import__("time").monotonic() + 600.0,
        tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}",
        intent_state_root=state_root, intent_owner_id=owner,
    )
    await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    await asyncio.gather(*manager._running_tasks.values(), return_exceptions=True)

    assert executed == []
    rows = read_owner_records(state_root, owner)
    assert [row["target"] for row in rows if row["event"] == "intent"] == ["provider.chat"]
    parent = __import__("json").loads(
        (state_root / "intent-journal" / f"{owner}.parent.json").read_text()
    )
    assert parent["active_operation_id"] is not None


def test_parent_operation_consumes_and_closes_before_next_provider_call(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("requires POSIX flock and directory fsync")
    owner = str(uuid4())
    create_parent(tmp_path, owner)
    token = set_parent_context(tmp_path, owner)
    try:
        with operation_context("subagent", "provider.chat") as operation:
            assert operation is not None
            attempt = operation.admit()
            operation.returned(attempt)
            operation.consume(attempt)
        with operation_context("subagent", "provider.chat") as second:
            assert second is not None
            attempt = second.admit()
            second.returned(attempt)
            second.consume(attempt)
        rows = read_owner_records(tmp_path, owner)
        assert [row["event"] for row in rows] == [
            "intent", "terminal", "consumed", "intent", "terminal", "consumed",
        ]
    finally:
        reset_parent_context(token)


def test_parent_operation_keeps_unknown_attempt_held(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("requires POSIX flock and directory fsync")
    owner = str(uuid4())
    create_parent(tmp_path, owner)
    token = set_parent_context(tmp_path, owner)
    try:
        with pytest.raises(RuntimeError, match="uncertain"):
            with operation_context("subagent", "provider.chat") as operation:
                assert operation is not None
                operation.admit()
                raise RuntimeError("uncertain")
        with pytest.raises(JournalUnavailableError, match="active or unresolved"):
            with operation_context("subagent", "provider.chat"):
                pass
    finally:
        reset_parent_context(token)


# @lat: [[architecture#Intent-before-effect evidence]]
def test_bridge_route_holds_existing_owner_and_rotates_only_after_consumption(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("requires POSIX flock and directory fsync")
    state_root = tmp_path / "state"
    state_root.mkdir()

    owner = journal.provision_route_parent(state_root, "planner")
    route_path, _route_lock = journal._route_paths(state_root, "planner")
    assert journal.provision_route_parent(state_root, "planner") == owner
    assert journal._read_route(route_path, "planner")["owner_id"] == owner

    parent_path = state_root / "intent-journal" / f"{owner}.parent.json"
    parent_before = __import__("json").loads(parent_path.read_text(encoding="utf-8"))
    assert parent_before["status"] == "active"
    assert parent_before["parent_consumed"] is False

    assert journal.close_route_parent(state_root, "planner", owner) is True
    parent_after = __import__("json").loads(parent_path.read_text(encoding="utf-8"))
    assert parent_after["status"] == "resolved"
    assert parent_after["parent_consumed"] is True

    next_owner = journal.provision_route_parent(state_root, "planner")
    assert next_owner != owner


# @lat: [[architecture#Intent-before-effect evidence]]
def test_bridge_cycle_parent_resumes_same_owner_and_reacknowledges_exact_phase(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    cycle_id = "cycle-opaque-001"
    head = "a" * 40

    with journal.bridge_cycle_parent_context(state_root) as first_owner:
        pass
    with pytest.raises(JournalUnavailableError, match="not complete"):
        with journal.bridge_cycle_parent_context(
            state_root, cycle_id=cycle_id, completed_phase="post_execution",
            checkpoint_head_sha=head, checkpoint_status="running",
        ):
            pytest.fail("running checkpoint must not be consumed")
    assert journal.read_owner_records(state_root, first_owner) == []

    with journal.bridge_cycle_parent_context(
        state_root, cycle_id=cycle_id, completed_phase="post_execution",
        checkpoint_head_sha=head, checkpoint_status="complete",
    ) as resumed_owner:
        assert resumed_owner == first_owner
        journal.consume_bridge_cycle_phase(
            state_root, resumed_owner, cycle_id=cycle_id, phase="post_execution",
            checkpoint_head_sha=head, checkpoint_status="complete",
        )

    records = journal.read_owner_records(state_root, first_owner)
    receipts = [row for row in records if row["event"] == "phase_consumed"]
    assert len(receipts) == 1
    assert receipts[0]["checkpoint_head_sha"] == head
    with pytest.raises(JournalUnavailableError, match="conflicts"):
        journal.consume_bridge_cycle_phase(
            state_root, first_owner, cycle_id=cycle_id, phase="post_execution",
            checkpoint_head_sha="b" * 40, checkpoint_status="complete",
        )
    assert journal.read_owner_records(state_root, first_owner) == records
    assert journal.provision_route_parent(state_root, "bridge.cycle") == first_owner


def test_bridge_cycle_close_requires_exact_selected_phase_receipts(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    cycle_id = "cycle-opaque-002"
    heads = {phase: str(index + 1) * 40 for index, phase in enumerate(
        ("post_execution", "smoke_gate", "integration", "reflection"),
    )}
    with journal.bridge_cycle_parent_context(state_root) as owner:
        pass
    for phase, head in heads.items():
        journal.consume_bridge_cycle_phase(
            state_root, owner, cycle_id=cycle_id, phase=phase,
            checkpoint_head_sha=head, checkpoint_status="complete",
        )
    wrong_heads = {**heads, "integration": "f" * 40}
    assert journal.close_route_parent(
        state_root, "bridge.cycle", owner, cycle_id=cycle_id,
        required_phase_heads=wrong_heads,
    ) is False
    assert journal.close_route_parent(
        state_root, "bridge.cycle", owner, cycle_id=cycle_id,
        required_phase_heads={"post_execution": heads["post_execution"]},
    ) is False
    assert journal.close_route_parent(
        state_root, "bridge.cycle", owner, cycle_id=cycle_id,
        required_phase_heads=heads,
    ) is True


def test_bridge_cycle_no_execution_receipt_closes_and_recovers_same_terminal(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cycle_id = "cycle-rest-opaque-1"
    with journal.bridge_cycle_parent_context(state_dir) as owner:
        pass
    journal.consume_bridge_cycle_no_execution(
        state_dir, owner, cycle_id=cycle_id, outcome="rest_unchanged",
    )
    before = journal.read_owner_records(state_dir, owner)
    journal.consume_bridge_cycle_no_execution(
        state_dir, owner, cycle_id=cycle_id, outcome="rest_unchanged",
    )
    assert journal.read_owner_records(state_dir, owner) == before
    with pytest.raises(JournalUnavailableError, match="attempted work"):
        journal.consume_bridge_cycle_no_execution(
            state_dir, owner, cycle_id=cycle_id, outcome="no_plan",
        )
    with journal.bridge_cycle_parent_context(
        state_dir, no_execution_cycle_id=cycle_id, no_execution_outcome="rest_unchanged",
    ) as resumed_owner:
        assert resumed_owner == owner
    assert journal.close_route_parent(
        state_dir, "bridge.cycle", owner,
        no_execution_cycle_id=cycle_id, no_execution_outcome="rest_unchanged",
    ) is True
    assert journal.provision_route_parent(state_dir, "bridge.cycle") != owner


def test_bridge_cycle_no_execution_rejects_empty_wrong_and_attempted_close(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cycle_id = "cycle-no-plan-opaque"
    with journal.bridge_cycle_parent_context(state_dir) as owner:
        pass
    assert journal.close_route_parent(
        state_dir, "bridge.cycle", owner, cycle_id=cycle_id, required_phase_heads={},
    ) is False
    for outcome in ("exception", "refused", "malformed", "spawn_failed", "timed_out"):
        with pytest.raises(JournalUnavailableError, match="not allowlisted"):
            journal.consume_bridge_cycle_no_execution(
                state_dir, owner, cycle_id=cycle_id, outcome=outcome,
            )
    operation_id, attempt_id = str(uuid4()), str(uuid4())
    journal.admit_attempt(
        state_dir, owner_id=owner, operation_id=operation_id, attempt_id=attempt_id,
        component="bridge", target="bridge.cycle", _test_only=True,
    )
    with pytest.raises(JournalUnavailableError, match="attempted work"):
        journal.consume_bridge_cycle_no_execution(
            state_dir, owner, cycle_id=cycle_id, outcome="no_plan",
        )
    assert journal.close_route_parent(
        state_dir, "bridge.cycle", owner,
        no_execution_cycle_id=cycle_id, no_execution_outcome="no_plan",
    ) is False


def test_bridge_cycle_no_execution_cannot_follow_selected_phase_receipt(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cycle_id = "cycle-mixed-opaque"
    with journal.bridge_cycle_parent_context(state_dir) as owner:
        pass
    journal.consume_bridge_cycle_phase(
        state_dir, owner, cycle_id=cycle_id, phase="post_execution",
        checkpoint_head_sha="c" * 40, checkpoint_status="complete",
    )
    with pytest.raises(JournalUnavailableError, match="attempted work"):
        journal.consume_bridge_cycle_no_execution(
            state_dir, owner, cycle_id=cycle_id, outcome="no_plan",
        )
    assert journal.provision_route_parent(state_dir, "bridge.cycle") == owner


def test_bridge_cycle_cancel_without_terminal_receipt_stays_held(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cycle_id = "cycle-cancel-opaque"
    with journal.bridge_cycle_parent_context(state_dir) as owner:
        pass
    assert journal.close_route_parent(
        state_dir, "bridge.cycle", owner,
        no_execution_cycle_id=cycle_id, no_execution_outcome="no_plan",
    ) is False
    with pytest.raises(JournalUnavailableError, match="terminal receipt is missing or mismatched"):
        with journal.bridge_cycle_parent_context(
            state_dir, no_execution_cycle_id=cycle_id, no_execution_outcome="no_plan",
        ):
            pytest.fail("missing terminal receipt must not reopen owner")


@pytest.mark.parametrize(
    "events",
    [
        [
            {"event": "no_execution_consumed", "cycle_id": "cycle-a", "outcome": "no_plan"},
            {"event": "phase_consumed", "cycle_id": "cycle-a", "phase": "post_execution", "checkpoint_head_sha": "a" * 40},
        ],
        [
            {"event": "phase_consumed", "cycle_id": "cycle-a", "phase": "post_execution", "checkpoint_head_sha": "a" * 40},
            {"event": "no_execution_consumed", "cycle_id": "cycle-a", "outcome": "no_plan"},
        ],
        [
            {"event": "no_execution_consumed", "cycle_id": "cycle-a", "outcome": "no_plan"},
            {"event": "phase_consumed", "cycle_id": "cycle-b", "phase": "post_execution", "checkpoint_head_sha": "b" * 40},
        ],
        [
            {"event": "phase_consumed", "cycle_id": "cycle-b", "phase": "post_execution", "checkpoint_head_sha": "b" * 40},
            {"event": "no_execution_consumed", "cycle_id": "cycle-a", "outcome": "no_plan"},
        ],
        [
            {"event": "phase_consumed", "cycle_id": "cycle-a", "phase": "post_execution", "checkpoint_head_sha": "a" * 40},
            {"event": "phase_consumed", "cycle_id": "cycle-b", "phase": "post_execution", "checkpoint_head_sha": "b" * 40},
        ],
    ],
)
def test_bridge_cycle_mixed_receipt_history_is_corrupt_and_preserved(tmp_path: Path, events) -> None:
    import json

    owner = str(uuid4())
    journal_dir = tmp_path / "intent-journal"
    journal_dir.mkdir()
    path = journal_dir / f"{owner}.jsonl"
    ts = "2026-11-15T08:00:00Z"
    rows = [
        {"version": 1, "owner_id": owner, **event, "ts": ts}
        for event in events
    ]
    original = b"".join(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n" for row in rows)
    path.write_bytes(original)
    with pytest.raises(JournalCorruptError, match="mixed cycle or terminal receipts"):
        journal._decode(path, owner)
    assert path.read_bytes() == original


def test_bridge_cycle_malformed_mixed_history_holds_reopen_and_close(tmp_path: Path) -> None:
    import json

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with journal.bridge_cycle_parent_context(state_dir) as owner:
        pass
    path = state_dir / "intent-journal" / f"{owner}.jsonl"
    ts = "2026-11-15T08:00:00Z"
    rows = [
        {"version": 1, "owner_id": owner, "cycle_id": "cycle-a", "outcome": "no_plan",
         "event": "no_execution_consumed", "ts": ts},
        {"version": 1, "owner_id": owner, "cycle_id": "cycle-b", "phase": "post_execution",
         "checkpoint_head_sha": "b" * 40, "event": "phase_consumed", "ts": ts},
    ]
    original = b"".join(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n" for row in rows)
    path.write_bytes(original)
    with pytest.raises(JournalCorruptError, match="mixed cycle or terminal receipts"):
        with journal.bridge_cycle_parent_context(
            state_dir, no_execution_cycle_id="cycle-a", no_execution_outcome="no_plan",
        ):
            pytest.fail("mixed receipt history must not reopen")
    with pytest.raises(JournalCorruptError, match="mixed cycle or terminal receipts"):
        journal.close_route_parent(
            state_dir, "bridge.cycle", owner, no_execution_cycle_id="cycle-a",
            no_execution_outcome="no_plan",
        )
    assert path.read_bytes() == original


def test_bridge_cycle_unconsumed_phase_cannot_be_replaced_by_fresh_owner(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    with journal.bridge_cycle_parent_context(state_root) as owner:
        pass
    with pytest.raises(JournalUnavailableError, match="requires checkpoint (or no-execution )?evidence"):
        with journal.bridge_cycle_parent_context(state_root):
            pytest.fail("started cycle owner must not be reopened without checkpoint receipt")
    assert journal.provision_route_parent(state_root, "bridge.cycle") == owner


def test_route_capacity_refuses_new_parent_without_pruning_settled_records(
    tmp_path: Path, monkeypatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setattr(journal, "MAX_ROUTE_PARENTS", 1)
    owner = journal.provision_route_parent(state_root, "planner")
    with journal.bridge_parent_context(state_root, owner):
        pass
    assert journal.close_route_parent(state_root, "planner", owner)

    with pytest.raises(JournalUnavailableError, match="capacity exhausted"):
        journal.provision_route_parent(state_root, "planner")
    assert journal._settled_route_owners(state_root, "planner") == {owner}
    assert (state_root / "intent-journal" / f"{owner}.jsonl").exists()
    assert (state_root / "intent-journal" / f"{owner}.parent.json").exists()


def test_proposer_fallback_sdk_budget_is_journaled_per_dispatch(tmp_path: Path, monkeypatch) -> None:
    import httpx
    import openai
    from nanobot.runtime import llm_proposer

    responses = [429, 200]
    sends = []
    def respond(request):
        sends.append(request)
        status = responses.pop(0)
        body = ({"error": {"message": "cooldown"}} if status == 429 else {
            "model": "an/backup", "choices": [{"finish_reason": "stop", "message": {"content": '{"task_title":"ok"}'}}],
        })
        headers = {"Retry-After": "60"} if status == 429 else {}
        return httpx.Response(status, json=body, headers=headers, request=request)

    class FakeHttpClient(openai.DefaultHttpxClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(openai, "DefaultHttpxClient", FakeHttpClient)
    monkeypatch.setattr(llm_proposer, "record_llm_call", lambda **kwargs: None)
    monkeypatch.setattr(llm_proposer, "record_llm_prompt", lambda **kwargs: None)
    failures = []
    monkeypatch.setattr(llm_proposer, "_record_failed_proposer_call", lambda exc, *a, **k: failures.append((type(exc).__name__, type(exc.__cause__).__name__ if exc.__cause__ else None)))
    state_root = tmp_path / "state"
    state_root.mkdir()
    state = {"primary": "primary", "alternate": "backup", "routes": {
        "primary": "cl/primary", "backup": "an/backup"}, "threshold": 60,
        "deadline": time.monotonic() + 30, "attempts": 0, "id": str(uuid4())}

    with journal.route_parent_context(state_root, "proposer"):
        result = llm_proposer._propose_with_fallback(
            state, openai.OpenAI, "https://gateway.invalid", "fake", "system", "user", "proposer",
        )
    assert result == {"task_title": "ok"}, (failures, len(sends), responses)
    assert state["attempts"] == 2 and len(sends) == 2
    owner = journal._read_route(journal._route_paths(state_root, "proposer")[0], "proposer")["owner_id"]
    rows = journal.read_owner_records(state_root, owner)
    assert [row["event"] for row in rows] == ["intent", "terminal", "consumed"] * 2
    assert len({row["operation_id"] for row in rows}) == 2


def test_proposer_primary_sdk_retry_is_journaled_without_changing_sdk_budget(tmp_path: Path, monkeypatch) -> None:
    import httpx
    import openai
    from nanobot.runtime import llm_proposer

    statuses = [429, 200]
    sends = []
    def respond(request):
        sends.append(request)
        status = statuses.pop(0)
        body = ({"error": {"message": "retry"}} if status == 429 else {
            "model": "cl/test", "choices": [{"finish_reason": "stop", "message": {"content": '{"task_title":"ok"}'}}],
        })
        return httpx.Response(status, json=body, headers={"Retry-After": "0"}, request=request)

    class FakeHttpClient(openai.DefaultHttpxClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(openai, "DefaultHttpxClient", FakeHttpClient)
    monkeypatch.setattr(llm_proposer, "_build_proposer_role_prompt", lambda *args: "system")
    monkeypatch.setattr(llm_proposer, "_model_name", lambda: "cl/test")
    monkeypatch.setattr(llm_proposer, "record_llm_call", lambda **kwargs: None)
    monkeypatch.setattr(llm_proposer, "record_llm_prompt", lambda **kwargs: None)
    monkeypatch.setattr(llm_proposer, "_record_failed_proposer_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm_proposer, "resolve_context_window", lambda *args, **kwargs: None)
    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.invalid")
    monkeypatch.setenv("LITELLM_API_KEY", "fake")
    state_root = tmp_path / "state"
    state_root.mkdir()

    with journal.route_parent_context(state_root, "proposer"):
        result = llm_proposer.propose("context")
    assert result == {"task_title": "ok"}
    assert len(sends) == 2 and statuses == []
    owner = journal._read_route(journal._route_paths(state_root, "proposer")[0], "proposer")["owner_id"]
    rows = journal.read_owner_records(state_root, owner)
    assert [row["event"] for row in rows] == ["intent", "terminal", "consumed"] * 2
    assert len({row["operation_id"] for row in rows}) == 1
    assert len({row["attempt_id"] for row in rows if row["event"] == "intent"}) == 2


def test_route_record_rejects_cross_route_label_corruption(tmp_path: Path) -> None:
    import json

    state_root = tmp_path / "state"
    state_root.mkdir()
    journal.provision_route_parent(state_root, "planner")
    route_path, _lock = journal._route_paths(state_root, "planner")
    record = journal._read_route(route_path, "planner")
    record["route"] = "proposer"
    route_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(JournalCorruptError, match="malformed"):
        journal.provision_route_parent(state_root, "planner")


def test_proposer_unknown_transport_settlement_holds_same_parent(tmp_path: Path, monkeypatch) -> None:
    import httpx
    import openai
    from nanobot.runtime import llm_proposer

    sends = []
    def fail(request):
        sends.append(request)
        raise httpx.ConnectError("ambiguous transport failure", request=request)

    class FakeHttpClient(openai.DefaultHttpxClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(fail), **kwargs)

    monkeypatch.setattr(openai, "DefaultHttpxClient", FakeHttpClient)
    monkeypatch.setattr(openai._base_client.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(llm_proposer, "_build_proposer_role_prompt", lambda *args: "system")
    monkeypatch.setattr(llm_proposer, "_model_name", lambda: "cl/test")
    monkeypatch.setattr(llm_proposer, "_record_failed_proposer_call", lambda *args, **kwargs: None)
    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.invalid")
    monkeypatch.setenv("LITELLM_API_KEY", "fake")
    state_root = tmp_path / "state"
    state_root.mkdir()

    with pytest.raises(JournalUnavailableError, match="not verified"):
        with journal.route_parent_context(state_root, "proposer"):
            assert llm_proposer.propose("context") is None
    assert len(sends) == 1
    route_path, _lock = journal._route_paths(state_root, "proposer")
    owner = journal._read_route(route_path, "proposer")["owner_id"]
    rows = journal.read_owner_records(state_root, owner)
    assert [row["event"] for row in rows] == ["intent"]
    assert journal.provision_route_parent(state_root, "proposer") == owner
    with pytest.raises(JournalUnavailableError, match="already started|unresolved active operation"):
        with journal.bridge_parent_context(state_root, owner, route="proposer"):
            pytest.fail("unknown proposer settlement must remain held")


def test_bridge_cycle_resume_uses_same_owner_only_after_consumed_attempt(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    with journal.bridge_cycle_parent_context(state_root) as first_owner:
        with operation_context("bridge", "bridge.cycle") as operation:
            assert operation is not None
            attempt = operation.admit()
            operation.returned(attempt)
            operation.consume(attempt)
            operation.close()
    route_path, _lock = journal._route_paths(state_root, "bridge.cycle")
    assert journal._read_route(route_path, "bridge.cycle")["owner_id"] == first_owner
    before = journal.read_owner_records(state_root, first_owner)

    with journal.bridge_cycle_parent_context(
        state_root, cycle_id="cycle-bridge-resume", completed_phase="post_execution",
        checkpoint_head_sha="a" * 40, checkpoint_status="complete",
    ) as resumed_owner:
        assert resumed_owner == first_owner
        resumed_records = journal.read_owner_records(state_root, resumed_owner)
        assert resumed_records[:-1] == before
        assert resumed_records[-1]["event"] == "phase_consumed"
        assert resumed_records[-1]["cycle_id"] == "cycle-bridge-resume"
    assert journal.provision_route_parent(state_root, "bridge.cycle") == first_owner


def test_bridge_cycle_resume_holds_unsettled_attempt_and_preserves_evidence(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    with journal.bridge_cycle_parent_context(state_root) as owner:
        with operation_context("bridge", "bridge.cycle") as operation:
            assert operation is not None
            operation.admit()
    before = journal.read_owner_records(state_root, owner)
    with pytest.raises(JournalUnavailableError, match="unresolved operation"):
        with journal.bridge_cycle_parent_context(
            state_root, cycle_id="cycle-unsettled", completed_phase="post_execution",
            checkpoint_head_sha="d" * 40, checkpoint_status="complete",
        ):
            pytest.fail("unsettled bridge-cycle attempt must remain held")
    assert journal.read_owner_records(state_root, owner) == before
    assert journal.provision_route_parent(state_root, "bridge.cycle") == owner


def test_autonomous_goal_review_uses_planner_owner_and_sdk_journal(tmp_path: Path, monkeypatch) -> None:
    import httpx
    import openai
    from nanobot.runtime import goal_review, llm_proposer

    sends = []
    def respond(request):
        sends.append(request)
        return httpx.Response(200, json={"model": "cl/test", "choices": [{
            "finish_reason": "stop", "message": {"content": '{"no_change":true}'},
        }]}, request=request)

    class FakeHttpClient(openai.DefaultHttpxClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(openai, "DefaultHttpxClient", FakeHttpClient)
    monkeypatch.setattr(llm_proposer, "_build_proposer_role_prompt", lambda *args: "system")
    monkeypatch.setattr(llm_proposer, "_model_name", lambda: "cl/test")
    monkeypatch.setattr(llm_proposer, "record_llm_call", lambda **kwargs: None)
    monkeypatch.setattr(llm_proposer, "record_llm_prompt", lambda **kwargs: None)
    monkeypatch.setattr(llm_proposer, "_record_failed_proposer_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm_proposer, "resolve_context_window", lambda *args, **kwargs: None)
    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.invalid")
    monkeypatch.setenv("LITELLM_API_KEY", "fake")
    state_root = tmp_path / "state"
    state_root.mkdir()
    owner = journal.provision_route_parent(state_root, "planner")

    with journal.bridge_parent_context(state_root, owner):
        reply = goal_review._call_llm("goal-review context")
        assert journal.close_route_parent(state_root, "planner", owner) is True
    assert reply == {"no_change": True} and len(sends) == 1
    rows = journal.read_owner_records(state_root, owner)
    assert [row["event"] for row in rows] == ["intent", "terminal", "consumed"]
    assert rows[0]["component"] == "goal_review"


def test_parent_routes_are_independent_and_tokens_are_not_cross_authorized(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    planner = journal.provision_route_parent(state_root, "planner")
    proposer = journal.provision_route_parent(state_root, "proposer")
    assert planner != proposer

    with journal.bridge_parent_context(state_root, planner, route="planner"):
        pass
    with pytest.raises(JournalUnavailableError, match="active route owner"):
        with journal.bridge_parent_context(state_root, planner, route="proposer"):
            pytest.fail("planner token must not authorize proposer route")
    with journal.bridge_parent_context(state_root, proposer, route="proposer"):
        pass


def test_bridge_parent_start_is_durable_before_effects(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    owner = journal.provision_route_parent(state_root, "planner")

    with journal.bridge_parent_context(state_root, owner):
        parent_path = state_root / "intent-journal" / f"{owner}.parent.json"
        assert __import__("json").loads(parent_path.read_text())["parent_started"] is True
        with operation_context("subagent", "provider.chat") as operation:
            assert operation is not None
            operation.admit()

    assert journal.provision_route_parent(state_root, "planner") == owner
    with pytest.raises(JournalUnavailableError, match="already started|unresolved active operation"):
        with journal.bridge_parent_context(state_root, owner):
            pytest.fail("started parent must not be reissued")


# @lat: [[architecture#Intent-before-effect evidence]]
def test_bridge_route_holds_missing_active_parent_journal(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    owner = journal.provision_route_parent(state_root, "planner")
    journal_path = state_root / "intent-journal" / f"{owner}.jsonl"
    journal_path.unlink()

    with pytest.raises(JournalUnavailableError, match="journal is missing"):
        journal.provision_route_parent(state_root, "planner")


# @lat: [[architecture#Intent-before-effect evidence]]
def test_completed_attempt_without_parent_consumption_is_held_in_fresh_process(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    owner = journal.provision_route_parent(state_root, "planner")
    other_owner = str(uuid4())
    create_parent(state_root, other_owner)
    with journal.bridge_parent_context(state_root, owner):
        with operation_context("subagent", "provider.chat") as operation:
            assert operation is not None
            attempt = operation.admit()
            operation.returned(attempt)
    assert [row["event"] for row in read_owner_records(state_root, owner)] == ["intent", "terminal"]

    probe = tmp_path / "recovery.json"
    script = """
import json, os, sys, threading
from contextlib import contextmanager
from pathlib import Path
import nanobot.runtime.intent_journal as journal
root = Path(sys.argv[1]); expected = sys.argv[2]; other = sys.argv[3]; out = Path(sys.argv[4])
if os.name == "nt":
    locks = {}; guard = threading.Lock()
    @contextmanager
    def fake_lock(path):
        path.touch(exist_ok=True)
        with guard:
            lock = locks.setdefault(str(path.resolve()), threading.Lock())
        with lock:
            yield
    journal._owner_lock = fake_lock
    journal._fsync_directory = lambda _path: None
actual = journal.provision_route_parent(root, "planner")
try:
    with journal.bridge_parent_context(root, actual):
        held = False
except journal.JournalUnavailableError:
    held = True
healthy = journal.read_owner_records(root, other)
out.write_text(json.dumps({"same_owner": actual == expected, "held": held, "other_records": len(healthy)}), encoding="utf-8")
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    if os.name == "nt":
        script = script.replace(
            "root = Path(sys.argv[1]); expected = sys.argv[2]; other = sys.argv[3]; out = Path(sys.argv[4])",
            "root = Path(sys.argv[1]); expected = sys.argv[2]; other = sys.argv[3]; out = Path(sys.argv[4])\n"
            "locks = {}; guard = threading.Lock()\n"
            "@contextmanager\n"
            "def fake_lock(path):\n"
            "    path.touch(exist_ok=True)\n"
            "    with guard: lock = locks.setdefault(str(path.resolve()), threading.Lock())\n"
            "    with lock: yield\n"
            "journal._owner_lock = fake_lock\n"
            "journal._fsync_directory = lambda _path: None",
        )
    result = subprocess.run(
        [sys.executable, "-c", script, str(state_root), owner, other_owner, str(probe)],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert __import__("json").loads(probe.read_text(encoding="utf-8")) == {
        "same_owner": True, "held": True, "other_records": 0,
    }


# @lat: [[architecture#Intent-before-effect evidence]]
def test_bridge_route_holds_corrupt_parent_evidence(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    owner = journal.provision_route_parent(state_root, "planner")
    parent_path = state_root / "intent-journal" / f"{owner}.parent.json"
    parent = __import__("json").loads(parent_path.read_text(encoding="utf-8"))
    parent["parent_consumed"] = True
    parent_path.write_text(__import__("json").dumps(parent), encoding="utf-8")

    with pytest.raises(JournalCorruptError, match="lifecycle evidence"):
        journal.provision_route_parent(state_root, "planner")


def test_owner_lock_identity_survives_settlement_and_partitions_threads(tmp_path: Path) -> None:
    owners = [str(uuid4()) for _ in range(8)]
    barrier = threading.Barrier(len(owners))
    failures: list[BaseException] = []

    def writer(owner: str) -> None:
        operation, attempt = str(uuid4()), str(uuid4())
        try:
            barrier.wait(timeout=5)
            _intent(tmp_path, owner, operation, attempt)
            record_returned(
                tmp_path, owner_id=owner, operation_id=operation, attempt_id=attempt,
                component="agent", target="provider.chat",
            )
        except BaseException as exc:  # collected for assertion in the main thread
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(owner,)) for owner in owners]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    for owner in owners:
        lock = tmp_path / "intent-journal" / f"{owner}.lock"
        before = lock.stat().st_ino
        assert len(read_owner_records(tmp_path, owner)) == 2
        assert lock.stat().st_ino == before


def test_unsupported_lock_fails_closed_before_effect(tmp_path: Path, monkeypatch) -> None:
    import nanobot.runtime.intent_journal as journal

    owner, operation, attempt = _ids()
    monkeypatch.setattr(journal, "fcntl", None)
    if os.name == "nt":
        monkeypatch.setattr(journal, "_owner_lock", _ORIGINAL_OWNER_LOCK)
    with pytest.raises(JournalUnavailableError, match="unsupported"):
        _intent(tmp_path, owner, operation, attempt)
    assert not (tmp_path / "intent-journal" / f"{owner}.jsonl").exists()


# @lat: [[architecture#Intent-before-effect evidence]]
def test_file_fsync_before_admission_and_directory_fsync_capability(tmp_path: Path, monkeypatch) -> None:
    import nanobot.runtime.intent_journal as journal

    owner, operation, attempt = _ids()
    if os.name == "nt":
        monkeypatch.setattr(journal, "_fsync_directory", _ORIGINAL_DIRECTORY_FSYNC)
        monkeypatch.setattr(journal, "fcntl", object())
        with pytest.raises(JournalUnavailableError, match="durably"):
            _intent(tmp_path, owner, operation, attempt)
        # Setup failed before admission; after restoring the test-only sync
        # shim, the created-but-unsynced empty journal is not treated as intent.
        monkeypatch.setattr(journal, "_fsync_directory", lambda path: None)
        assert read_owner_records(tmp_path, owner) == []
        return

    actual_fsync = os.fsync
    calls: list[int] = []
    def observe(fd: int) -> None:
        calls.append(fd)
        actual_fsync(fd)
    monkeypatch.setattr(journal.os, "fsync", observe)
    _intent(tmp_path, owner, operation, attempt)
    assert len(calls) >= 2  # persistent lock + journal/directory syncs
    assert len(read_owner_records(tmp_path, owner)) == 1


# @lat: [[architecture#Intent-before-effect evidence]]
def test_independent_owner_partitions_do_not_cross_block(tmp_path: Path) -> None:
    owner, operation, attempt = _ids()
    _intent(tmp_path, owner, operation, attempt)
    with pytest.raises(JournalUnavailableError, match="durable parent-resolution"):
        _intent(tmp_path, owner, str(uuid4()), str(uuid4()))
    other_owner, op2, attempt2 = _ids()
    _intent(tmp_path, other_owner, op2, attempt2)
    assert len(read_owner_records(tmp_path, other_owner)) == 1
