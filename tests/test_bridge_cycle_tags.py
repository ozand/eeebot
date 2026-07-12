"""Tests for #721: pre/post cycle git tags — exact dedup anchor + rollback audit.

Covers the tag helper trio added to nanobot/runtime/bridge.py
(``_tag_cycle_pre``, ``_tag_cycle_post``, ``_prune_cycle_tags``) plus the
tag-first pre-spawn dedup path, against real temp git repos. Reuses the
bridge-integration harness from tests/test_cycle_ledger.py (bare "origin" +
working clone standing in for the shared ``eeebot-self-evolving`` checkout).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from nanobot.runtime import bridge
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _git,
    _init_selfevo_repo,
    _read_ledger,
    _run,
    _seed_bridge_request,
)


def _commit_days_ago(work: Path, filename: str, content: str, message: str, days_ago: int) -> str:
    """Create a commit backdated ``days_ago`` days via GIT_AUTHOR_DATE/
    GIT_COMMITTER_DATE (git's env vars want ``@<epoch> <tz>``, not a fuzzy
    "N days ago" string — for pruning tests).
    """
    (work / filename).write_text(content)
    _run(work, "add", filename)
    epoch = int(time.time()) - (days_ago * 86400)
    date = f"@{epoch} +0000"
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(_git(work) + ["commit", "-m", message], env=env, capture_output=True, text=True, check=True)
    return _run(work, "rev-parse", "HEAD").stdout.strip()


class _ExplodingSubagentManager:
    """Stand-in that fails the test if a subagent is ever spawned — used to
    prove the tag-first dedup path skips BEFORE any spawn attempt."""

    def __init__(self, *, workspace, **_kwargs):
        self.workspace = workspace

    async def spawn(self, **_kwargs):
        raise AssertionError("subagent should not have been spawned — tag-first dedup should have skipped")


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    """Mirrors tests/test_cycle_ledger.py / tests/test_bridge_cycle_branch.py:
    point the bounded gate's core-smoke set at the one test file these
    fixtures create.
    """
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


# ─── unit-level tag helper tests ───────────────────────────────────────────────


class TestSafeRefId:
    def test_sanitizes_unsafe_characters(self):
        assert bridge._safe_ref_id("abc/def xyz") == "abc-def-xyz"

    def test_empty_falls_back_to_unknown(self):
        assert bridge._safe_ref_id("") == "unknown"
        assert bridge._safe_ref_id(None) == "unknown"


class TestTagCyclePre:
    def test_creates_tag_at_given_sha(self, tmp_path):
        origin, work = _init_selfevo_repo(tmp_path)
        main_sha = _run(work, "rev-parse", "main").stdout.strip()

        bridge._tag_cycle_pre(work, "cycle-1", main_sha)

        tags = _run(work, "tag", "--list").stdout.split()
        assert "pre-cycle-cycle-1" in tags
        assert _run(work, "rev-parse", "pre-cycle-cycle-1").stdout.strip() == main_sha

    def test_force_overwrites_on_retry(self, tmp_path):
        """A retried cycle re-using the same cycle_id must not error (`-f`)."""
        origin, work = _init_selfevo_repo(tmp_path)
        main_sha = _run(work, "rev-parse", "main").stdout.strip()

        bridge._tag_cycle_pre(work, "cycle-1", main_sha)
        # Should not raise even though the tag already exists.
        bridge._tag_cycle_pre(work, "cycle-1", main_sha)

        tags = _run(work, "tag", "--list").stdout.split()
        assert tags.count("pre-cycle-cycle-1") == 1

    def test_fail_open_on_missing_repo(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        bridge._tag_cycle_pre(missing, "cycle-1", "deadbeef")  # must not raise

    def test_no_op_on_empty_sha(self, tmp_path):
        origin, work = _init_selfevo_repo(tmp_path)
        bridge._tag_cycle_pre(work, "cycle-1", "")
        assert _run(work, "tag", "--list").stdout.split() == []


class TestTagCyclePost:
    def test_creates_tag_encoding_outcome(self, tmp_path):
        origin, work = _init_selfevo_repo(tmp_path)
        sha = _run(work, "rev-parse", "main").stdout.strip()

        bridge._tag_cycle_post(work, "cycle-1", "success", sha)

        tags = _run(work, "tag", "--list").stdout.split()
        assert "cycle-cycle-1-success" in tags
        assert _run(work, "rev-parse", "cycle-cycle-1-success").stdout.strip() == sha

    def test_invalid_outcome_coerced_to_failed(self, tmp_path):
        origin, work = _init_selfevo_repo(tmp_path)
        sha = _run(work, "rev-parse", "main").stdout.strip()

        bridge._tag_cycle_post(work, "cycle-1", "not-a-real-outcome", sha)

        tags = _run(work, "tag", "--list").stdout.split()
        assert "cycle-cycle-1-failed" in tags

    def test_defaults_to_current_head_when_sha_omitted(self, tmp_path):
        origin, work = _init_selfevo_repo(tmp_path)
        head = _run(work, "rev-parse", "HEAD").stdout.strip()

        bridge._tag_cycle_post(work, "cycle-1", "skipped-duplicate")

        assert _run(work, "rev-parse", "cycle-cycle-1-skipped-duplicate").stdout.strip() == head

    def test_fail_open_on_missing_repo(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        bridge._tag_cycle_post(missing, "cycle-1", "success")  # must not raise


class TestCycleTagExists:
    def test_true_when_tag_present(self, tmp_path):
        origin, work = _init_selfevo_repo(tmp_path)
        sha = _run(work, "rev-parse", "main").stdout.strip()
        bridge._tag_cycle_post(work, "cycle-1", "success", sha)
        assert bridge._cycle_tag_exists(work, "cycle-cycle-1-success") is True

    def test_false_when_absent(self, tmp_path):
        origin, work = _init_selfevo_repo(tmp_path)
        assert bridge._cycle_tag_exists(work, "cycle-nope-success") is False

    def test_false_fail_open_on_missing_repo(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        assert bridge._cycle_tag_exists(missing, "cycle-1-success") is False


class TestPruneCycleTags:
    def test_deletes_old_keeps_recent(self, tmp_path):
        origin, work = _init_selfevo_repo(tmp_path)

        old_sha = _commit_days_ago(work, "old.txt", "old", "old commit", 40)
        _run(work, "tag", "pre-cycle-old")
        _run(work, "tag", "cycle-old-success")

        recent_sha = _commit_days_ago(work, "recent.txt", "recent", "recent commit", 1)
        _run(work, "tag", "pre-cycle-recent")
        assert old_sha and recent_sha

        bridge._prune_cycle_tags(work, keep_days=30)

        tags = _run(work, "tag", "--list").stdout.split()
        assert "pre-cycle-old" not in tags
        assert "cycle-old-success" not in tags
        assert "pre-cycle-recent" in tags

    def test_respects_env_override(self, tmp_path, monkeypatch):
        origin, work = _init_selfevo_repo(tmp_path)
        _commit_days_ago(work, "old.txt", "old", "old commit", 10)
        _run(work, "tag", "pre-cycle-old")

        monkeypatch.setenv("CYCLE_TAG_RETENTION_DAYS", "5")
        bridge._prune_cycle_tags(work)

        assert "pre-cycle-old" not in _run(work, "tag", "--list").stdout.split()

    def test_fail_open_on_missing_repo(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        bridge._prune_cycle_tags(missing)  # must not raise

    def test_bounded_by_cap(self, tmp_path, monkeypatch):
        """Even with a large tag namespace, pruning must not blow up — bounded
        by _PRUNE_TAG_CAP. Exercised at a much smaller scale for test speed."""
        origin, work = _init_selfevo_repo(tmp_path)
        monkeypatch.setattr(bridge, "_PRUNE_TAG_CAP", 2)
        sha = _run(work, "rev-parse", "main").stdout.strip()
        for i in range(5):
            _run(work, "tag", f"pre-cycle-t{i}", sha)
        bridge._prune_cycle_tags(work, keep_days=30)  # must not raise; recent tags kept regardless
        assert len(_run(work, "tag", "--list").stdout.split()) == 5


# ─── full bridge-cycle integration ──────────────────────────────────────────────


class TestBridgeCycleTagIntegration:
    def test_full_green_cycle_leaves_pre_and_post_tags(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        origin, work = _init_selfevo_repo(base)
        main_sha_before = _run(work, "rev-parse", "main").stdout.strip()

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

        _seed_bridge_request(state_dir, "req-tags", "cycle-tags")

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        tags = _run(work, "tag", "--list").stdout.split()
        assert "pre-cycle-cycle-tags" in tags
        assert "cycle-cycle-tags-success" in tags

        assert _run(work, "rev-parse", "pre-cycle-cycle-tags").stdout.strip() == main_sha_before

        # The post tag is written right after the terminal ledger row — before
        # the (separate, best-effort) structured-lesson commit that may land
        # on main afterward — so assert ancestry rather than exact equality
        # with the final origin/main tip.
        post_sha = _run(work, "rev-parse", "cycle-cycle-tags-success").stdout.strip()
        assert post_sha != main_sha_before
        ancestor = subprocess.run(
            _git(work) + ["merge-base", "--is-ancestor", post_sha, "main"], capture_output=True,
        )
        assert ancestor.returncode == 0

    def test_tag_first_dedup_skips_before_spawn(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        origin, work = _init_selfevo_repo(base)
        # Pre-seed a success tag for this exact cycle_id, simulating a retried/
        # replayed request whose cycle already completed successfully.
        _run(work, "tag", "cycle-cycle-dup-success")

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _ExplodingSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

        _seed_bridge_request(state_dir, "req-dup-tag", "cycle-dup")

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        phases = [r["phase"] for r in rows]
        assert phases == ["started", "dedup", "outcome"]
        assert rows[1]["decision"] == "skipped_duplicate"
        assert rows[1]["matched_against"] == "tag:cycle-cycle-dup-success"
        assert rows[2]["outcome"] == "skipped-duplicate"

    def test_fail_open_tag_failure_never_breaks_a_green_cycle(self, tmp_path, monkeypatch):
        """A tagging failure (here: refs/tags made unwritable, so every
        ``git tag`` call in the cycle fails) must never surface as a bridge
        cycle failure — tags are pure observability, layered on top of the
        real ``git tag`` calls (not mocked out), so this exercises the actual
        internal try/except in _tag_cycle_pre/_tag_cycle_post/_prune_cycle_tags."""
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        origin, work = _init_selfevo_repo(base)

        tags_dir = work / ".git" / "refs" / "tags"
        tags_dir.mkdir(parents=True, exist_ok=True)
        tags_dir.chmod(0o500)
        try:
            monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
            monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
            monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
            monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
            monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

            _seed_bridge_request(state_dir, "req-tagfail", "cycle-tagfail")

            result = asyncio.run(bridge._main_impl())
            assert result == 0

            rows = _read_ledger(state_dir)
            outcome_rows = [r for r in rows if r["phase"] == "outcome"]
            assert outcome_rows[-1]["outcome"] == "success"
            # The permission block means no tag was actually written — proving
            # the failure was swallowed rather than silently skipped by luck.
            assert _run(work, "tag", "--list").stdout.strip() == ""
        finally:
            tags_dir.chmod(0o700)
