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
    read_owner_records,
    record_returned,
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
        target=kwargs.pop("target", "provider.chat"), **kwargs,
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
    real_fsync = os.fsync
    calls = 0

    def fail_terminal_sync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected terminal fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(journal.os, "fsync", fail_terminal_sync)
    with pytest.raises(JournalUnavailableError, match="not verified"):
        record_returned(
            tmp_path, owner_id=owner, operation_id=operation, attempt_id=attempt,
            component="agent", target="provider.chat",
        )
    monkeypatch.setattr(journal.os, "fsync", real_fsync)
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
    for _ in range(MAX_OWNER_RECORDS // 2):
        operation, attempt = str(uuid4()), str(uuid4())
        intent = journal._record(owner, operation, attempt, "intent", "agent", "provider.chat", None)
        journal._append(path, intent, records)
        records.append(intent)
        terminal = journal._record(owner, operation, attempt, "terminal", "agent", "provider.chat", None)
        journal._append(path, terminal, records)
        records.append(terminal)
    before = path.read_bytes()
    next_row = journal._record(owner, str(uuid4()), str(uuid4()), "intent", "agent", "provider.chat", None)
    with pytest.raises(JournalUnavailableError, match="capacity"):
        journal._append(path, next_row, records)
    assert path.read_bytes() == before
    assert len(read_owner_records(tmp_path, owner)) == MAX_OWNER_RECORDS
    assert MAX_OWNER_BYTES == 1_048_576


# @lat: [[architecture#Intent-before-effect evidence]]
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
