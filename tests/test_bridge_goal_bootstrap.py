"""Tests for #913: drop the outbox bootstrap dependency.

``bridge._main_impl`` used to require BOTH a non-empty
``outbox/report.index.json:source`` AND a resolvable goal id before running
any cycle — the only writer of ``outbox/`` was the decommissioned
coordinator, so a fresh/rebuilt state dir (no ``outbox/`` at all) could never
start a cycle even with a perfectly valid ``goals/registry.json`` (maintained
by the live goal machinery). Goal id resolution is now registry-first, with
the outbox's legacy ``goal.goal_id`` as a fallback only; ``report_source`` is
no longer part of the gate (see tests/test_bridge_recent_activity_context.py
for the build_task prompt-line side of this change).

Exercises this through the same seam tests/test_bridge_bulk_skip.py and
friends already use: ``bridge._main_impl()`` called directly with STATE_DIR/
BRIDGE_STATE_DIR/TARGET_WORKSPACE monkeypatched onto a tmp_path — no pending
request is seeded, so a resolvable goal id falls through to the
``already_handled`` print, while an unresolvable one prints
``no_active_goal``. Both are printed before find_pending_request is ever
consulted, so no selfevo checkout / subagent scaffolding is needed either.
"""
from __future__ import annotations

import asyncio
import json

from nanobot.runtime import bridge


def _set_common_paths(monkeypatch, state_dir, base):
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")


class TestGoalIdBootstrapPrecedence:
    def test_registry_only_no_outbox_dir_runs_normally(self, tmp_path, monkeypatch, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        (state_dir / "goals").mkdir(parents=True)
        (state_dir / "goals" / "registry.json").write_text(
            json.dumps({"active_goal_id": "goal-registry-only"}), encoding="utf-8",
        )
        # No outbox/ directory at all — the fresh-install case #913 fixes.
        assert not (state_dir / "outbox").exists()

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        out = capsys.readouterr().out
        assert "no_active_goal" not in out
        assert "already_handled" in out

    def test_outbox_only_legacy_fallback_runs_normally(self, tmp_path, monkeypatch, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        # No goals/registry.json at all — only the legacy outbox goal_id.
        (state_dir / "outbox").mkdir(parents=True)
        (state_dir / "outbox" / "report.index.json").write_text(
            json.dumps({"source": "legacy-report", "goal": {"goal_id": "goal-legacy"}}),
            encoding="utf-8",
        )
        assert not (state_dir / "goals" / "registry.json").exists()

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        out = capsys.readouterr().out
        assert "no_active_goal" not in out
        assert "already_handled" in out

    def test_registry_takes_precedence_over_outbox(self, tmp_path, monkeypatch):
        """Registry active_goal_id wins even when a (stale) outbox goal_id
        is also present — registry is now PRIMARY, outbox is fallback only.
        """
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        (state_dir / "goals").mkdir(parents=True)
        (state_dir / "goals" / "registry.json").write_text(
            json.dumps({"active_goal_id": "goal-fresh"}), encoding="utf-8",
        )
        (state_dir / "outbox").mkdir(parents=True)
        (state_dir / "outbox" / "report.index.json").write_text(
            json.dumps({"source": "stale-report", "goal": {"goal_id": "goal-stale"}}),
            encoding="utf-8",
        )

        captured_goal_ids = []
        real_write = bridge.write_backlog_snapshot

        def _spy(state_dir_arg, selfevo_repo_arg):
            data = bridge.load_json(state_dir_arg / "goals" / "registry.json") or {}
            captured_goal_ids.append(data.get("active_goal_id"))
            return real_write(state_dir_arg, selfevo_repo_arg)

        monkeypatch.setattr(bridge, "write_backlog_snapshot", _spy)

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert captured_goal_ids == ["goal-fresh"]

    def test_neither_present_prints_no_active_goal(self, tmp_path, monkeypatch, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        # Neither goals/registry.json nor outbox/report.index.json exists.
        result = asyncio.run(bridge._main_impl())
        assert result == 0

        out = capsys.readouterr().out
        assert "no_active_goal" in out

    def test_neither_present_does_not_crash(self, tmp_path, monkeypatch):
        """No crash even on a completely empty state dir (defense-in-depth,
        redundant with the capsys assertion above but pins the return code
        contract independently of print output)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        assert asyncio.run(bridge._main_impl()) == 0


class TestBacklogSnapshotCalledEveryRun:
    def test_snapshot_written_even_on_no_active_goal_path(self, tmp_path, monkeypatch):
        """#913: the snapshot call wraps _main_impl_body in a `finally`, so
        it must fire even on the earliest possible return (no_active_goal),
        not just on a successful cycle."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        assert asyncio.run(bridge._main_impl()) == 0

        assert (state_dir / "hypotheses" / "backlog.json").is_file()

    def test_snapshot_written_on_already_handled_path(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)
        (state_dir / "goals").mkdir(parents=True)
        (state_dir / "goals" / "registry.json").write_text(
            json.dumps({"active_goal_id": "goal-1"}), encoding="utf-8",
        )

        assert asyncio.run(bridge._main_impl()) == 0

        assert (state_dir / "hypotheses" / "backlog.json").is_file()

    def test_snapshot_failure_never_breaks_the_cycle_result(self, tmp_path, monkeypatch):
        """Even if write_backlog_snapshot itself raises (defense-in-depth —
        it already fails open internally, but the call site wraps it too),
        the cycle's own return value is unaffected."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(bridge, "write_backlog_snapshot", _boom)

        assert asyncio.run(bridge._main_impl()) == 0


class TestUsageEvidenceRefreshedEveryRun:
    def test_usage_evidence_refreshed_on_already_handled_and_no_goal_paths(self, tmp_path, monkeypatch):
        """#1083: refresh_usage and confirm_serves run early in _main_impl_body,
        so usage feeds do not go stale when bridge returns early."""
        from nanobot.runtime import usage_evidence

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        calls = []

        def _mock_refresh(s, r):
            calls.append(("refresh_usage", s, r))

        def _mock_confirm(s, r):
            calls.append(("confirm_serves", s, r))

        monkeypatch.setattr(usage_evidence, "refresh_usage", _mock_refresh)
        monkeypatch.setattr(usage_evidence, "confirm_serves", _mock_confirm)

        assert asyncio.run(bridge._main_impl()) == 0
        assert len(calls) == 2
        assert calls[0][0] == "refresh_usage"
        assert calls[1][0] == "confirm_serves"

    def test_usage_evidence_failure_never_breaks_the_cycle_result(self, tmp_path, monkeypatch):
        """#1083: fail-open defense for usage refresh at start of cycle."""
        from nanobot.runtime import usage_evidence

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        def _boom(*_a, **_k):
            raise RuntimeError("usage refresh boom")

        monkeypatch.setattr(usage_evidence, "refresh_usage", _boom)
        monkeypatch.setattr(usage_evidence, "confirm_serves", _boom)

        assert asyncio.run(bridge._main_impl()) == 0
