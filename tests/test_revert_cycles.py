"""#1772: a cheap, safe undo for a range of loop integrations.

Covers: resolving targets from the ledger (join correctness, the
untracked/None-vs-False confirmed-in-use distinction, ordering); the merge-
commit shape verification; the scratch-repo round trip (apply, then the
tree matches the state before the reverted range); mid-range conflict
handling (clean abort, no partial commit); the bridge-active refusal; and
the dry-run default leaving the repo untouched.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.revert_cycles import (
    RevertRefusedError,
    Target,
    apply_revert,
    bridge_is_active,
    format_dry_run,
    resolve_targets,
    verify_merge_commit,
)


# ─── scratch git repo helpers ───────────────────────────────────────────────


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "chore: seed")
    return path


def _cycle_merge(repo: Path, cycle_id: str, filename: str, content: str, *, lane: str = "cycle") -> str:
    """Simulate one bridge integration: a branch, one commit, merged back
    into main with the bridge's own merge-commit subject shape."""
    branch = f"selfevo/{lane}-{cycle_id}"
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / filename).parent.mkdir(parents=True, exist_ok=True)
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", f"feat: change for {cycle_id}")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", "-q", "-m", f"merge: integrate {branch}", branch)
    _git(repo, "branch", "-q", "-D", branch)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _tree_hash(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", f"{ref}^{{tree}}").stdout.strip()


def _target(cycle_id: str, sha: str, *, ts: str = "2026-09-19T00:00:00Z",
            files: tuple[str, ...] = (), tier: str | None = "code-bearing",
            confirmed: bool | None = None) -> Target:
    return Target(cycle_id=cycle_id, sha=sha, ts=ts, files_changed=files, change_tier=tier, confirmed=confirmed)


# ─── resolve_targets: the ledger join ───────────────────────────────────────


def _write_ledger(state_dir: Path, rows: list[dict]) -> None:
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "cycles.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8",
    )


class TestResolveTargets:
    def test_joins_evolution_tree_sha_and_completed_confirmed(self, tmp_path):
        state = tmp_path / "state"
        _write_ledger(state, [
            {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
             "files_changed": ["scripts/a.py"], "change_tier": "code-bearing",
             "ts": "2026-09-19T01:00:00Z"},
            {"phase": "evolution_tree", "cycle_id": "c1", "sha": "abc123"},
        ])
        (state / "demand").mkdir()
        (state / "demand" / "completed.json").write_text(json.dumps({
            "entries": {"demand-x": {"cycle_id": "c1", "confirmed": True, "signal": "reference"}},
        }), encoding="utf-8")

        targets = resolve_targets(state)
        assert len(targets) == 1
        t = targets[0]
        assert t.cycle_id == "c1" and t.sha == "abc123"
        assert t.files_changed == ("scripts/a.py",) and t.change_tier == "code-bearing"
        assert t.confirmed is True

    def test_missing_completed_entry_is_none_not_false(self, tmp_path):
        """#1772 AC: a failed join is missing data, never a negative."""
        state = tmp_path / "state"
        _write_ledger(state, [
            {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
             "files_changed": [], "change_tier": "documentation", "ts": "2026-09-19T01:00:00Z"},
            {"phase": "evolution_tree", "cycle_id": "c1", "sha": "abc123"},
        ])
        (state / "demand").mkdir()
        (state / "demand" / "completed.json").write_text(json.dumps({"entries": {}}), encoding="utf-8")

        targets = resolve_targets(state)
        assert targets[0].confirmed is None

    def test_no_evolution_tree_row_is_never_a_target(self, tmp_path):
        """Nothing to revert without a commit to point at."""
        state = tmp_path / "state"
        _write_ledger(state, [
            {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
             "files_changed": [], "ts": "2026-09-19T01:00:00Z"},
        ])
        assert resolve_targets(state) == []

    def test_failed_and_partial_outcomes_are_excluded(self, tmp_path):
        state = tmp_path / "state"
        _write_ledger(state, [
            {"phase": "outcome", "cycle_id": "c1", "outcome": "failed", "ts": "2026-09-19T01:00:00Z"},
            {"phase": "evolution_tree", "cycle_id": "c1", "sha": "abc123"},
            {"phase": "outcome", "cycle_id": "c2", "outcome": "partial", "ts": "2026-09-19T01:00:00Z"},
            {"phase": "evolution_tree", "cycle_id": "c2", "sha": "def456"},
        ])
        assert resolve_targets(state) == []

    def test_newest_first_ordering(self, tmp_path):
        state = tmp_path / "state"
        _write_ledger(state, [
            {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-09-19T01:00:00Z"},
            {"phase": "evolution_tree", "cycle_id": "c1", "sha": "sha1"},
            {"phase": "outcome", "cycle_id": "c2", "outcome": "success", "ts": "2026-09-19T03:00:00Z"},
            {"phase": "evolution_tree", "cycle_id": "c2", "sha": "sha2"},
            {"phase": "outcome", "cycle_id": "c3", "outcome": "success", "ts": "2026-09-19T02:00:00Z"},
            {"phase": "evolution_tree", "cycle_id": "c3", "sha": "sha3"},
        ])
        assert [t.cycle_id for t in resolve_targets(state)] == ["c2", "c3", "c1"]

    def test_cycle_id_filter(self, tmp_path):
        state = tmp_path / "state"
        _write_ledger(state, [
            {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-09-19T01:00:00Z"},
            {"phase": "evolution_tree", "cycle_id": "c1", "sha": "sha1"},
            {"phase": "outcome", "cycle_id": "c2", "outcome": "success", "ts": "2026-09-19T02:00:00Z"},
            {"phase": "evolution_tree", "cycle_id": "c2", "sha": "sha2"},
        ])
        assert [t.cycle_id for t in resolve_targets(state, cycle_ids=["c2"])] == ["c2"]

    def test_until_is_exclusive(self, tmp_path):
        state = tmp_path / "state"
        _write_ledger(state, [
            {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-09-19T01:00:00Z"},
            {"phase": "evolution_tree", "cycle_id": "c1", "sha": "sha1"},
        ])
        until = datetime(2026, 9, 19, 1, 0, 0, tzinfo=timezone.utc)
        assert resolve_targets(state, until=until) == []
        assert len(resolve_targets(state, until=until + timedelta(seconds=1))) == 1


# ─── merge-commit shape verification ────────────────────────────────────────


class TestVerifyMergeCommit:
    def test_a_real_bridge_merge_verifies_clean(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha = _cycle_merge(repo, "abc123def", "scripts/a.py", "x = 1\n")
        t = _target("cycle-abc123def", sha)
        assert verify_merge_commit(repo, t) is None

    def test_rejects_a_non_merge_commit(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        t = _target("cycle-x", sha)
        problem = verify_merge_commit(repo, t)
        assert problem is not None and "2-parent" in problem

    def test_rejects_a_merge_with_the_wrong_subject_shape(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _git(repo, "checkout", "-q", "-b", "side")
        (repo / "x.py").write_text("1\n", encoding="utf-8")
        _git(repo, "add", "x.py")
        _git(repo, "commit", "-q", "-m", "feat: x")
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "--no-ff", "-q", "-m", "Merge branch 'side'", "side")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        t = _target("cycle-x", sha)
        problem = verify_merge_commit(repo, t)
        assert problem is not None and "does not match" in problem

    def test_rejects_when_the_sha_names_a_different_cycle(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha = _cycle_merge(repo, "aaa111", "a.py", "1\n")  # subject names cycle-aaa111
        t = _target("cycle-bbb222", sha)  # claiming a different cycle
        problem = verify_merge_commit(repo, t)
        assert problem is not None and "does not name cycle" in problem


# ─── the scratch-repo round trip (the AC's own required proof) ─────────────


class TestApplyRevertRoundTrip:
    def test_reverting_the_newest_two_restores_the_tree_from_before_them(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _cycle_merge(repo, "aaa111", "a.py", "a=1\n")
        tree_before_range = _tree_hash(repo)  # state right after cycle 1
        sha2 = _cycle_merge(repo, "bbb222", "b.py", "b=1\n")
        sha3 = _cycle_merge(repo, "ccc333", "c.py", "c=1\n")

        targets = [
            _target("cycle-ccc333", sha3, ts="2026-09-19T03:00:00Z"),
            _target("cycle-bbb222", sha2, ts="2026-09-19T02:00:00Z"),
        ]
        result = apply_revert(repo, targets, push=False, bridge_active_override=False)

        assert result.applied is True and result.error is None
        assert len(result.reverted) == 2
        # The tree now matches the tree from right after cycle 1 -- b.py and
        # c.py are gone again, a.py is unchanged.
        assert _tree_hash(repo) == tree_before_range
        assert not (repo / "b.py").exists() and not (repo / "c.py").exists()
        assert (repo / "a.py").read_text(encoding="utf-8") == "a=1\n"

    def test_revert_commit_names_every_reverted_cycle_and_reads_as_a_revert(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha1 = _cycle_merge(repo, "aaa111", "a.py", "a=1\n")
        targets = [_target("cycle-aaa111", sha1)]
        result = apply_revert(repo, targets, push=False, bridge_active_override=False)

        subject = _git(repo, "log", "-1", "--format=%s", result.commit_sha).stdout.strip()
        body = _git(repo, "log", "-1", "--format=%b", result.commit_sha).stdout
        assert subject.startswith("revert:")
        assert "cycle-aaa111" in body

        from scripts.change_shape import classify_subject
        assert classify_subject(subject) == "maintenance"

    def test_push_updates_the_remote(self, tmp_path):
        bare = tmp_path / "bare.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        repo = _init_repo(tmp_path / "repo")
        _git(repo, "remote", "add", "origin", str(bare))
        _git(repo, "push", "-q", "-u", "origin", "main")
        sha1 = _cycle_merge(repo, "aaa111", "a.py", "a=1\n")
        _git(repo, "push", "-q", "origin", "main")

        result = apply_revert(repo, [_target("cycle-aaa111", sha1)], push=True, bridge_active_override=False)
        assert result.applied is True and result.error is None

        remote_head = subprocess.run(
            ["git", "log", "-1", "--format=%s", "main"], cwd=bare, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert remote_head.startswith("revert:")

    def test_mid_range_conflict_aborts_cleanly_with_no_partial_commit(self, tmp_path):
        """The measured case: an older revert conflicts because a later
        commit touched the same lines. All-or-nothing -- HEAD and the tree
        are exactly as they were before this call, and nothing is
        committed."""
        repo = _init_repo(tmp_path / "repo")
        sha1 = _cycle_merge(repo, "aaa111", "shared.py", "line1\n")
        sha2 = _cycle_merge(repo, "bbb222", "shared.py", "line1\nline2\n")
        # cycle 3 rewrites the SAME file/line cycle 1 touched, independent
        # of cycle 2's own change -- reverting cycle 1 alone (without also
        # reverting cycle 3, which is not in this range) will conflict.
        _git(repo, "checkout", "-q", "-b", "selfevo/cycle-ccc333")
        (repo / "shared.py").write_text("line1-rewritten\nline2\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-q", "-m", "feat: change for ccc333")
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "--no-ff", "-q", "-m", "merge: integrate selfevo/cycle-ccc333", "selfevo/cycle-ccc333")

        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        tree_before = _tree_hash(repo)
        targets = [_target("cycle-aaa111", sha1), _target("cycle-bbb222", sha2)]  # cycle 3 deliberately excluded

        result = apply_revert(repo, targets, push=False, bridge_active_override=False)

        assert result.applied is False
        assert result.commit_sha is None and result.reverted == []
        assert result.error is not None and "conflict" in result.error
        # Exactly as it was -- no partial revert, nothing committed.
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
        assert _tree_hash(repo) == tree_before
        status = _git(repo, "status", "--porcelain")
        assert status.stdout.strip() == ""

    def test_malformed_target_refuses_before_touching_the_repo(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha1 = _cycle_merge(repo, "aaa111", "a.py", "a=1\n")
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        bad_target = _target("cycle-wrong-id", sha1)  # sha belongs to a different cycle

        result = apply_revert(repo, [bad_target], push=False, bridge_active_override=False)
        assert result.applied is False
        assert "does not name cycle" in result.error
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


# ─── the bridge-active guard ────────────────────────────────────────────────


class TestBridgeGuard:
    def test_refuses_outright_when_active(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        with pytest.raises(RevertRefusedError, match="active"):
            apply_revert(repo, [_target("c1", "deadbeef")], bridge_active_override=True)

    def test_bridge_is_active_maps_states(self, monkeypatch):
        import nanobot.runtime.health as health_mod

        for state, expected in [("active", True), ("inactive", False), ("unknown", True),
                                 ("activating", True), ("failed", True)]:
            monkeypatch.setattr(
                health_mod, "read_service_status",
                lambda *_a, _state=state, **_k: {"active_state": _state},
            )
            assert bridge_is_active() is expected, state

    def test_bridge_is_active_treats_a_read_exception_as_active(self, monkeypatch):
        import nanobot.runtime.health as health_mod

        def _boom(*_a, **_k):
            raise OSError("systemctl unavailable")

        monkeypatch.setattr(health_mod, "read_service_status", _boom)
        assert bridge_is_active() is True


# ─── dry run leaves the repo untouched ──────────────────────────────────────


def test_dry_run_report_never_touches_the_repo(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    sha1 = _cycle_merge(repo, "aaa111", "a.py", "a=1\n")
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    report = format_dry_run([_target("cycle-aaa111", sha1, files=("a.py",), confirmed=None)])
    assert "cycle-aaa111" in report and "untracked" in report
    assert "Pass --apply" in report
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_dry_run_report_with_no_targets():
    assert "no matching integrations" in format_dry_run([])
