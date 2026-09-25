"""Tests for the bridge's goal bootstrap: the active goal id comes from
``goals/goal_text.json`` (#1222).

#913 dropped the outbox bootstrap dependency and made ``goals/registry.json``
primary — but both files were written only by the coordinator and froze on
2026-08-22 when it was deleted (#916/#923). The operator canon,
``goals/goal_text.json`` (seeded by ``deploy_release.sh``), has carried
``goal_id`` all along; ``goal_review.active_goal_id`` reads it and the bridge
consults nothing else. A frozen registry or outbox present on the host is
ignored, and a fresh state dir with only ``goal_text.json`` starts a cycle.

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
from tests.test_cycle_ledger import _init_selfevo_repo


def _mock_planning_infra(monkeypatch, base):
    """ADR-035 rule 1 (#1942): the planning session (and the repository
    preparation/provider it needs) now runs on EVERY resolvable-goal cycle,
    before any request is selected — a resolvable goal id no longer falls
    straight through to an ``already_handled`` print with no infra touched
    (that print, and the rotation path it belonged to, is retired). Tests
    that only care about goal-id resolution stand in a real selfevo
    checkout for repo prep and stub the provider/planning session to the
    cheap ``no plan`` outcome, so they still exercise nothing past goal
    bootstrap."""
    _init_selfevo_repo(base)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

    async def _fake_planning_session(**_kwargs):
        return {
            'ran': False, 'iterations_used': None, 'iterations_planned': None,
            'tampered_files': [], 'plan': None,
        }

    monkeypatch.setattr(bridge, "_run_planning_session", _fake_planning_session)


def _set_common_paths(monkeypatch, state_dir, base, *, charter=True):
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    # ADR-034 rule 3: the executor now refuses to run at all without a real
    # release charter, checked before this module's own goal-id logic ever
    # runs (see TestCharterPrecedesNoActiveGoal below). Every test here that
    # is actually about goal-id resolution needs a charter in place so it
    # can reach that logic; charter=False opts back out for the tests that
    # cover the charter-absence precedence itself. Either way RELEASE_ROOT
    # is explicitly pinned to a directory this test controls — never left
    # at bridge.RELEASE_ROOT's ambient default, which in the full suite can
    # carry another test's leftover charter dir (module-level state another
    # test's monkeypatch/importlib.reload interaction restored it to,
    # observed live as a CI-only failure here, #1947).
    release_root = base / "_adr034_release_root"
    release_root.mkdir(exist_ok=True)
    if charter:
        (release_root / "goals.md").write_text("test charter", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", release_root)


def _write_goal_text(state_dir, goal_id: str) -> None:
    (state_dir / "goals").mkdir(parents=True, exist_ok=True)
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"schema_version": "goal-text-v1", "goal_id": goal_id, "text": "test goal"}),
        encoding="utf-8",
    )


class TestGoalIdBootstrap:
    def test_goal_text_only_runs_normally(self, tmp_path, monkeypatch, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)
        _mock_planning_infra(monkeypatch, tmp_path)

        _write_goal_text(state_dir, "goal-canon")
        # No outbox/, no registry.json — the fresh-install case.
        assert not (state_dir / "outbox").exists()
        assert not (state_dir / "goals" / "registry.json").exists()

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        # ADR-035 rule 1 (#1942): a resolvable goal id now proceeds to a
        # real planning session (stubbed here to `no plan`) rather than the
        # retired `already_handled` rotation-queue print.
        out = capsys.readouterr().out
        assert "no_active_goal" not in out
        assert "no plan produced this cycle" in out

    def test_frozen_registry_and_outbox_are_not_a_goal_source(self, tmp_path, monkeypatch, capsys):
        """The coordinator's files may still sit on the host; they are not read."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        (state_dir / "goals").mkdir(parents=True)
        (state_dir / "goals" / "registry.json").write_text(
            json.dumps({"active_goal_id": "goal-frozen"}), encoding="utf-8",
        )
        (state_dir / "outbox").mkdir(parents=True)
        (state_dir / "outbox" / "report.index.json").write_text(
            json.dumps({"source": "stale-report", "goal": {"goal_id": "goal-stale"}}),
            encoding="utf-8",
        )
        assert not (state_dir / "goals" / "goal_text.json").exists()

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        out = capsys.readouterr().out
        assert "no_active_goal" in out

    def test_goal_text_wins_over_frozen_registry(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)
        _mock_planning_infra(monkeypatch, tmp_path)

        _write_goal_text(state_dir, "goal-canon")
        (state_dir / "goals" / "registry.json").write_text(
            json.dumps({"active_goal_id": "goal-frozen"}), encoding="utf-8",
        )

        from nanobot.runtime import goal_review

        captured_goal_ids = []
        real_active = goal_review.active_goal_id

        def _spy(state_dir_arg):
            goal_id = real_active(state_dir_arg)
            captured_goal_ids.append(goal_id)
            return goal_id

        # bridge imports active_goal_id from goal_review at call time
        monkeypatch.setattr(goal_review, "active_goal_id", _spy)

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert captured_goal_ids and set(captured_goal_ids) == {"goal-canon"}

    def test_goal_text_without_id_prints_no_active_goal(self, tmp_path, monkeypatch, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        (state_dir / "goals").mkdir(parents=True)
        (state_dir / "goals" / "goal_text.json").write_text(
            json.dumps({"text": "a goal with no id"}), encoding="utf-8",
        )

        assert asyncio.run(bridge._main_impl()) == 0
        assert "no_active_goal" in capsys.readouterr().out

    def test_empty_state_dir_prints_no_active_goal(self, tmp_path, monkeypatch, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        out = capsys.readouterr().out
        assert "no_active_goal" in out

    def test_empty_state_dir_does_not_crash(self, tmp_path, monkeypatch):
        """No crash even on a completely empty state dir (defense-in-depth,
        redundant with the capsys assertion above but pins the return code
        contract independently of print output)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)

        assert asyncio.run(bridge._main_impl()) == 0


class TestCharterPrecedesNoActiveGoal:
    """ADR-034 rule 3 (pA review comment on #1947): a missing/unreadable
    release charter is a more fundamental precondition failure than a
    missing goal id — it must be reported, and the run must stop, BEFORE
    the no_active_goal/already_handled early-outs below, and before any
    request is even looked up. Previously the charter check lived deep
    inside the per-request loop (gated behind both a resolvable goal id
    AND a pending request existing), so an absent charter with no
    resolvable goal id printed the misleading no_active_goal instead of
    the real blocker."""

    def test_charter_absent_and_goal_id_absent_prints_no_charter(self, tmp_path, monkeypatch, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path, charter=False)

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        out = capsys.readouterr().out
        assert "no_charter" in out
        assert "no_active_goal" not in out

    def test_charter_absent_takes_priority_even_with_a_resolvable_goal_id(self, tmp_path, monkeypatch, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path, charter=False)
        _write_goal_text(state_dir, "goal-canon")

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        out = capsys.readouterr().out
        assert "no_charter" in out
        assert "already_handled" not in out


class TestNoBacklogSnapshot:
    def test_no_backlog_json_is_written_by_a_run(self, tmp_path, monkeypatch):
        """#1356: the per-run snapshot is retired with its writer."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _set_common_paths(monkeypatch, state_dir, tmp_path)
        _mock_planning_infra(monkeypatch, tmp_path)
        _write_goal_text(state_dir, "goal-1")

        assert asyncio.run(bridge._main_impl()) == 0

        assert not (state_dir / "hypotheses" / "backlog.json").exists()
        assert not hasattr(bridge, "write_backlog_snapshot")

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
