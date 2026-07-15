"""Tests for #736: pre-spawn dedup false-positives — the keyword heuristic in
``_task_already_done`` was matching genuinely-new LLM proposals against a
keyword-saturated 7-day git log even though the proposal's target file did
not exist in the instance repo.

LLM-proposed requests (``nanobot.runtime.llm_proposer.write_request``) always
carry a ``Target path: <path>`` line in their ``task`` field. This is used to
make the pre-spawn dedup gate target_path-aware:

- target_path present, file missing -> keyword heuristic is bypassed entirely
  (task cannot possibly be already done).
- target_path present, file exists -> keyword heuristic runs, but scoped to
  ``git log -- <target_path>`` instead of the whole log.
- no target_path -> unchanged behavior (whole-log ``_task_already_done``).

Reuses the bridge-integration harness from tests/test_cycle_ledger.py.
"""
from __future__ import annotations

import asyncio

import pytest

from nanobot.runtime import bridge
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _run,
    _seed_bridge_request,
)


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


# ─── _extract_target_path unit tests ──────────────────────────────────────────


class TestExtractTargetPath:
    def test_extracts_from_llm_proposer_shaped_request(self):
        req = {
            "task_title": "Implement and commit: Create a memory pressure checker",
            "task": (
                "Add a script that checks RAM and swap usage to detect memory "
                "pressure.\n\nTarget path: scripts/check_memory_pressure.py"
            ),
            "recommended_next_action": (
                "Implement and commit: Create a memory pressure checker "
                "(target: scripts/check_memory_pressure.py)"
            ),
        }
        assert bridge._extract_target_path(req) == "scripts/check_memory_pressure.py"

    def test_falls_back_to_recommended_next_action(self):
        req = {
            "task": "no target path marker here at all",
            "recommended_next_action": "Implement and commit: X (target: scripts/x.py)",
        }
        assert bridge._extract_target_path(req) == "scripts/x.py"

    def test_returns_none_when_absent(self):
        req = {"task": "just a plain task with no marker", "recommended_next_action": ""}
        assert bridge._extract_target_path(req) is None

    def test_returns_none_for_empty_request(self):
        assert bridge._extract_target_path({}) is None

    def test_fail_open_on_garbage_input(self):
        class _Weird:
            def get(self, *_a, **_k):
                raise RuntimeError("boom")

        # Must not raise — fail-open to None.
        assert bridge._extract_target_path(_Weird()) is None


# ─── pre-spawn dedup integration tests ────────────────────────────────────────


TITLE = "Create a script to check memory pressure levels"
TASK_TEXT_TEMPLATE = (
    "Add a script that checks RAM and swap usage to detect memory "
    "pressure.\n\nTarget path: {target_path}"
)


def _setup(base, monkeypatch):
    state_dir = base / "state"
    state_dir.mkdir()
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
    return state_dir


class TestMissingTargetPathBypassesKeywordHeuristic:
    def test_missing_target_file_not_skipped_despite_saturated_log(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        _origin, work = _init_selfevo_repo(base)

        # Saturate the 7-day git log with commits that share >=3 keywords with
        # TITLE but never touch the (nonexistent) target path.
        (work / "mod.py").write_text("def ok():\n    return True\n")
        _run(work, "add", "mod.py")
        _run(
            work, "commit", "--allow-empty", "-m",
            "chore: script to check memory pressure bookkeeping levels",
        )
        _run(work, "push", "origin", "HEAD:main")

        target_path = "scripts/check_memory_pressure.py"  # does NOT exist in repo
        _seed_bridge_request(
            state_dir, "req-novel", "cycle-novel",
            task_title=f"Implement and commit: {TITLE}",
            task=TASK_TEXT_TEMPLATE.format(target_path=target_path),
            recommended_next_action=f"Implement and commit: {TITLE} (target: {target_path})",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r.get("cycle_id") == "cycle-novel" and r["phase"] == "outcome"]
        assert len(outcome_rows) == 1
        # Proceeded to spawn (success), NOT skipped as a false-positive duplicate.
        assert outcome_rows[0]["outcome"] == "success"


class TestExistingTargetPathScopesKeywordHeuristic:
    def test_matching_commit_touching_path_fires_dedup(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        _origin, work = _init_selfevo_repo(base)

        target_path = "scripts/check_memory_pressure.py"
        (work / "scripts").mkdir(exist_ok=True)
        (work / "scripts" / "check_memory_pressure.py").write_text("def main():\n    pass\n")
        _run(work, "add", "scripts/check_memory_pressure.py")
        _run(
            work, "commit", "-m",
            "feat: add script to check memory pressure levels",
        )
        _run(work, "push", "origin", "HEAD:main")

        _seed_bridge_request(
            state_dir, "req-dup", "cycle-dup",
            task_title=f"Implement and commit: {TITLE}",
            task=TASK_TEXT_TEMPLATE.format(target_path=target_path),
            recommended_next_action=f"Implement and commit: {TITLE} (target: {target_path})",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r.get("cycle_id") == "cycle-dup" and r["phase"] == "outcome"]
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["outcome"] == "skipped-duplicate"

    def test_keyword_matching_commits_not_touching_path_do_not_fire(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        _origin, work = _init_selfevo_repo(base)

        target_path = "scripts/check_memory_pressure.py"
        # Target file exists, but was NOT touched by the keyword-matching commit.
        (work / "scripts").mkdir(exist_ok=True)
        (work / "scripts" / "check_memory_pressure.py").write_text("def main():\n    pass\n")
        _run(work, "add", "scripts/check_memory_pressure.py")
        _run(work, "commit", "-m", "feat: unrelated initial add")

        (work / "mod.py").write_text("def ok():\n    return 1\n")
        _run(work, "add", "mod.py")
        _run(
            work, "commit", "-m",
            "feat: script to check memory pressure levels (unrelated file)",
        )
        _run(work, "push", "origin", "HEAD:main")

        _seed_bridge_request(
            state_dir, "req-novel2", "cycle-novel2",
            task_title=f"Implement and commit: {TITLE}",
            task=TASK_TEXT_TEMPLATE.format(target_path=target_path),
            recommended_next_action=f"Implement and commit: {TITLE} (target: {target_path})",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r.get("cycle_id") == "cycle-novel2" and r["phase"] == "outcome"]
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["outcome"] == "success"


class TestDemandVettedRequestBypassesAlreadyDone:
    def test_demand_serves_request_not_killed_by_word_overlap(self, tmp_path, monkeypatch):
        """#760 follow-up, fired live 2026-07-15 20:42Z: the P14 proposal
        ('Extend eeebot_dashboard.py with demand and idle visibility
        section', serves 'demand priority-…') was killed by
        _task_already_done_for_path — its title shares 4 words with a P11
        commit touching the same dashboard file. Demand-vetted requests were
        already judged not-done by the demand collector's strong filter
        (#748/#769); the bridge must not second-guess with the weaker word
        heuristic."""
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        _origin, work = _init_selfevo_repo(base)

        target_path = "scripts/eeebot_dashboard.py"
        (work / "scripts").mkdir(exist_ok=True)
        (work / "scripts" / "eeebot_dashboard.py").write_text("def main():\n    pass\n")
        _run(work, "add", target_path)
        # A P11-style commit touching the SAME file, word-overlapping the
        # new proposal's title (extend/dashboard/section/...).
        _run(
            work, "commit", "-m",
            "feat: extend eeebot_dashboard.py with loop health section",
        )
        _run(work, "push", "origin", "HEAD:main")

        title = "Extend eeebot_dashboard.py with demand and idle visibility section"
        _seed_bridge_request(
            state_dir, "req-demand", "cycle-demand",
            task_title=f"Implement and commit: {title}",
            task=(
                f"Add the demand section.\n\nTarget path: {target_path}\n"
                "Serves: demand priority-b7942f7bf37b"
            ),
            recommended_next_action=f"Implement and commit: {title} (target: {target_path})",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r.get("cycle_id") == "cycle-demand" and r["phase"] == "outcome"]
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["outcome"] == "success"

    def test_same_request_without_serves_still_deduped(self, tmp_path, monkeypatch):
        """Belt: absent the demand marker, the word heuristic behaves as
        before — the bypass is scoped strictly to demand-vetted requests."""
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        _origin, work = _init_selfevo_repo(base)

        target_path = "scripts/eeebot_dashboard.py"
        (work / "scripts").mkdir(exist_ok=True)
        (work / "scripts" / "eeebot_dashboard.py").write_text("def main():\n    pass\n")
        _run(work, "add", target_path)
        _run(
            work, "commit", "-m",
            "feat: extend eeebot_dashboard.py with demand and idle visibility section",
        )
        _run(work, "push", "origin", "HEAD:main")

        title = "Extend eeebot_dashboard.py with demand and idle visibility section"
        _seed_bridge_request(
            state_dir, "req-legacy", "cycle-legacy",
            task_title=f"Implement and commit: {title}",
            task=f"Add the demand section.\n\nTarget path: {target_path}",
            recommended_next_action=f"Implement and commit: {title} (target: {target_path})",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r.get("cycle_id") == "cycle-legacy" and r["phase"] == "outcome"]
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["outcome"] == "skipped-duplicate"


class TestNoTargetPathFallsBackUnchanged:
    def test_request_without_target_path_uses_task_already_done(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        _init_selfevo_repo(base)

        monkeypatch.setattr(bridge, "_task_already_done", lambda *_a, **_k: True)

        _seed_bridge_request(
            state_dir, "req-plain", "cycle-plain",
            task_title="Some deterministic planner task with no target path",
            task="Just do the thing, no target path marker here.",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r.get("cycle_id") == "cycle-plain" and r["phase"] == "outcome"]
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["outcome"] == "skipped-duplicate"

    def test_request_without_target_path_proceeds_when_not_already_done(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        _init_selfevo_repo(base)

        monkeypatch.setattr(bridge, "_task_already_done", lambda *_a, **_k: False)

        _seed_bridge_request(
            state_dir, "req-plain2", "cycle-plain2",
            task_title="Some deterministic planner task with no target path",
            task="Just do the thing, no target path marker here.",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r.get("cycle_id") == "cycle-plain2" and r["phase"] == "outcome"]
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["outcome"] == "success"
