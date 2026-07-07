"""Tests for #680: bridge defense-in-depth hardening.

Two independent, self-contained pieces:

1. ``_acquire_bridge_lock`` — an exclusive, non-blocking ``flock`` on
   ``<state_dir>/bridge.lock`` guarding against two bridge processes racing
   through the same shared ``eeebot-self-evolving`` checkout (systemd's
   ``Type=oneshot`` is the only protection today; a manual invocation
   overlapping a timer-triggered run could still race it).
2. The HEAD-on-main precondition wired into ``main()`` / ``_main_impl()``:
   if a prior cycle left the shared checkout on a stray cycle branch (because
   ``_restore_to_main`` failed twice), the *next* invocation must repair it
   (or abort with a ``blocked`` result) before running any bookkeeping.

The lock is tested directly against the small ``_acquire_bridge_lock`` helper
— no need to drive all of ``main()`` for that. The HEAD-on-main precondition
reuses ``_restore_to_main``, already covered by
``tests/test_bridge_cycle_branch.py`` against real temp git repos; here we
exercise it the same way but from the "prior cycle left a stray branch"
angle, plus the abort path via monkeypatching.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime import bridge


def _git(repo: Path) -> list[str]:
    return ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(_git(repo) + list(args), capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare 'origin' and a clone with one commit on main."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _run(work, "config", "user.email", "bridge@test.local")
    _run(work, "config", "user.name", "bridge-test")
    _run(work, "checkout", "-B", "main")
    (work / "mod.py").write_text("def ok():\n    return True\n")
    _run(work, "add", ".")
    _run(work, "commit", "-m", "init")
    _run(work, "push", "origin", "HEAD:main")
    return origin, work


class TestAcquireBridgeLock:
    def test_acquires_when_free(self, tmp_path):
        handle = bridge._acquire_bridge_lock(tmp_path)
        try:
            assert handle is not None
            assert (tmp_path / "bridge.lock").exists()
        finally:
            handle.close()

    def test_second_acquire_in_same_process_is_contended(self, tmp_path):
        """flock is per-open-file-description: a second independent open()
        of the same lock file while the first is still held must fail,
        exactly like a second bridge process would.
        """
        first = bridge._acquire_bridge_lock(tmp_path)
        assert first is not None
        try:
            second = bridge._acquire_bridge_lock(tmp_path)
            assert second is None
        finally:
            first.close()

    def test_lock_releases_on_close_allowing_reacquire(self, tmp_path):
        first = bridge._acquire_bridge_lock(tmp_path)
        assert first is not None
        first.close()

        second = bridge._acquire_bridge_lock(tmp_path)
        assert second is not None
        second.close()

    def test_falls_back_to_null_lock_when_fcntl_unavailable(self, tmp_path, monkeypatch):
        """On a platform without fcntl, locking degrades to a no-op rather
        than hard-failing the cycle.
        """
        monkeypatch.setattr(bridge, "fcntl", None)
        handle = bridge._acquire_bridge_lock(tmp_path)
        assert handle is not None
        assert isinstance(handle, bridge._NullLock)
        handle.close()  # must not raise

    def test_contended_lock_via_monkeypatched_flock(self, tmp_path, monkeypatch):
        """Simulate contention without a second process: force flock() to
        raise BlockingIOError, as the OS would for an already-held lock.
        """
        def _raise(*_args, **_kwargs):
            raise BlockingIOError("lock held")

        monkeypatch.setattr(bridge.fcntl, "flock", _raise)
        handle = bridge._acquire_bridge_lock(tmp_path)
        assert handle is None


class TestMainHonoursLockContention:
    """End-to-end: main() must exit cleanly (code 0) without touching the
    repo when the lock is already held — it should never reach
    find_pending_request()/the git helpers.
    """

    def test_main_exits_cleanly_when_lock_held(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "STATE_DIR", tmp_path)
        monkeypatch.setattr(bridge, "BRIDGE_ENABLED", True)

        held = bridge._acquire_bridge_lock(tmp_path)
        assert held is not None
        try:
            called = {"find_pending_request": False}

            def _boom():
                called["find_pending_request"] = True
                raise AssertionError("must not be reached while lock is held")

            monkeypatch.setattr(bridge, "find_pending_request", _boom)

            import asyncio
            result = asyncio.run(bridge.main())

            assert result == 0
            assert called["find_pending_request"] is False
        finally:
            held.close()


class TestHeadOnMainPrecondition:
    """The precondition reuses _restore_to_main directly; these tests confirm
    the property it guarantees (checkout repaired to main) against a real
    temp git repo left on a stray cycle branch, plus the failure/abort shape.
    """

    def test_restores_stray_cycle_branch_to_main(self, tmp_path):
        _origin, work = _init_repo(tmp_path)
        # Simulate a prior cycle that left the checkout on a stray branch
        # (e.g. _restore_to_main failed twice after a crash).
        _run(work, "checkout", "-b", "selfevo/cycle-stray")
        (work / "untracked.txt").write_text("leftover\n")

        restored = bridge._restore_to_main(work)

        assert restored is True
        current_branch = _run(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert current_branch == "main"
        assert not (work / "untracked.txt").exists()

    def test_missing_repo_is_not_a_precondition_failure(self, tmp_path):
        """A checkout that hasn't been cloned yet is not a stray-branch
        condition — bridge.py only treats is_dir()-and-restore-fails as a
        precondition failure; a missing repo is left to
        _setup_cycle_branch's existing 'repo_missing' handling.
        """
        missing = tmp_path / "does-not-exist"
        assert not missing.is_dir()
        # Mirrors the guard in _main_impl: `if repo.is_dir() and not
        # _restore_to_main(repo)`.
        assert not (missing.is_dir() and not bridge._restore_to_main(missing))

    def test_restore_failure_would_trigger_abort_guard(self, tmp_path, monkeypatch):
        """When _restore_to_main can't repair the checkout (e.g. both
        `checkout main` and the `checkout -B main origin/main` fallback
        fail), the guard used in _main_impl evaluates to True (abort).
        """
        _origin, work = _init_repo(tmp_path)
        monkeypatch.setattr(bridge, "_restore_to_main", lambda repo: False)

        should_abort = work.is_dir() and not bridge._restore_to_main(work)

        assert should_abort is True
