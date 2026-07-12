"""Tests for #720: minimal cycle ledger — append-only JSONL with enum outcomes.

Covers the standalone ``nanobot.runtime.cycle_ledger`` module (append/read
round-trip, fail-open behavior, rotation, one-terminal-row-per-cycle
semantics via the typed helpers, enum enforcement) plus a light
bridge-integration check that a full green cycle and an ``already_done``
pre-spawn skip each leave the expected started/dedup/gate/outcome rows in
``<state_dir>/ledger/cycles.jsonl``.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import bridge, cycle_ledger, llm_proposer


def _read_ledger(state_dir: Path) -> list[dict]:
    path = state_dir / "ledger" / "cycles.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ─── append_event / round-trip ────────────────────────────────────────────────


class TestAppendEvent:
    def test_append_and_read_round_trip(self, tmp_path):
        cycle_ledger.append_event(tmp_path, {"phase": "started", "cycle_id": "c1"})
        cycle_ledger.append_event(tmp_path, {"phase": "outcome", "cycle_id": "c1", "outcome": "success"})

        rows = _read_ledger(tmp_path)
        assert len(rows) == 2
        assert rows[0]["phase"] == "started"
        assert rows[0]["cycle_id"] == "c1"
        assert "ts" in rows[0]
        assert rows[1]["outcome"] == "success"

    def test_creates_parent_dir(self, tmp_path):
        state_dir = tmp_path / "does" / "not" / "exist" / "yet"
        cycle_ledger.append_event(state_dir, {"phase": "started"})
        assert (state_dir / "ledger" / "cycles.jsonl").exists()

    def test_fail_open_on_unwritable_dir(self, tmp_path, monkeypatch):
        """An append that can't write (e.g. mkdir raising) must never propagate."""

        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "mkdir", _boom)
        # Must not raise.
        cycle_ledger.append_event(tmp_path, {"phase": "started"})

    def test_fail_open_on_unserializable_event(self, tmp_path):
        """A non-JSON-serializable value falls back to str() via default=str,
        but if something still blows up, append_event swallows it."""
        cycle_ledger.append_event(tmp_path, {"phase": "started", "weird": object()})
        rows = _read_ledger(tmp_path)
        assert len(rows) == 1
        assert "weird" in rows[0]


# ─── typed helpers ─────────────────────────────────────────────────────────────


class TestTypedHelpers:
    def test_record_cycle_started_writes_phase_started(self, tmp_path):
        cycle_ledger.record_cycle_started(tmp_path, "cycle-1", "req-1", None)
        rows = _read_ledger(tmp_path)
        assert rows[0] == {
            "phase": "started",
            "cycle_id": "cycle-1",
            "request_id": "req-1",
            "branch": None,
            "ts": rows[0]["ts"],
        }

    def test_record_dedup_decision_valid_values(self, tmp_path):
        for decision in cycle_ledger.VALID_DEDUP_DECISIONS:
            cycle_ledger.record_dedup_decision(tmp_path, "c1", decision, "match-x")
        rows = _read_ledger(tmp_path)
        assert {r["decision"] for r in rows} == set(cycle_ledger.VALID_DEDUP_DECISIONS)

    def test_record_dedup_decision_unknown_coerced_to_proceeded(self, tmp_path):
        cycle_ledger.record_dedup_decision(tmp_path, "c1", "bogus-decision", None)
        rows = _read_ledger(tmp_path)
        assert rows[0]["decision"] == "proceeded"

    def test_record_gate_decision_allow_and_block(self, tmp_path):
        cycle_ledger.record_gate_decision(tmp_path, "c1", True, "smoke_passed", [])
        cycle_ledger.record_gate_decision(tmp_path, "c1", False, "mutation_surface_violation", ["nanobot/x.py"])
        rows = _read_ledger(tmp_path)
        assert rows[0]["allowed"] is True
        assert rows[0]["violations"] == []
        assert rows[1]["allowed"] is False
        assert rows[1]["violations"] == ["nanobot/x.py"]

    def test_record_cycle_outcome_valid_enum(self, tmp_path):
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1")
        rows = _read_ledger(tmp_path)
        assert rows[0]["outcome"] == "success"
        assert rows[0]["files_changed"] == ["a.py"]
        assert rows[0]["branch"] == "selfevo/cycle-1"

    def test_record_cycle_outcome_invalid_enum_coerced_to_failed(self, tmp_path):
        """#720 test-plan contract: an invalid outcome value is coerced to
        'failed' rather than raising or writing free text."""
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "not-a-real-outcome", "weird", [], None)
        rows = _read_ledger(tmp_path)
        assert rows[0]["outcome"] == "failed"

    def test_one_terminal_row_per_cycle(self, tmp_path):
        """Exercising the full helper set for one cycle produces exactly one
        'outcome'-phase row (the others are 'started'/'dedup'/'gate')."""
        cycle_ledger.record_cycle_started(tmp_path, "c1", "req-1", None)
        cycle_ledger.record_dedup_decision(tmp_path, "c1", "proceeded", None)
        cycle_ledger.record_gate_decision(tmp_path, "c1", True, "smoke_passed", [])
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1")

        rows = _read_ledger(tmp_path)
        outcome_rows = [r for r in rows if r["phase"] == "outcome"]
        assert len(outcome_rows) == 1
        assert [r["phase"] for r in rows] == ["started", "dedup", "gate", "outcome"]


# ─── rotation ──────────────────────────────────────────────────────────────────


class TestRotation:
    def test_rotates_stale_active_file_to_gz(self, tmp_path):
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        active = ledger_dir / "cycles.jsonl"
        active.write_text('{"phase": "started"}\n', encoding="utf-8")

        import os

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
        os.utime(active, (yesterday, yesterday))

        # New append triggers rotation before writing the new line.
        cycle_ledger.append_event(tmp_path, {"phase": "started", "cycle_id": "today"})

        gz_files = list(ledger_dir.glob("cycles-*.jsonl.gz"))
        assert len(gz_files) == 1
        with gzip.open(gz_files[0], "rt", encoding="utf-8") as fh:
            archived = fh.read()
        assert '"phase": "started"' in archived

        # Active file now contains only today's fresh row.
        rows = _read_ledger(tmp_path)
        assert len(rows) == 1
        assert rows[0]["cycle_id"] == "today"

    def test_prunes_archives_past_retention(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "5")
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()

        old_day = (datetime.now(timezone.utc).date() - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_day = (datetime.now(timezone.utc).date() - timedelta(days=1)).strftime("%Y-%m-%d")
        old_gz = ledger_dir / f"cycles-{old_day}.jsonl.gz"
        recent_gz = ledger_dir / f"cycles-{recent_day}.jsonl.gz"
        with gzip.open(old_gz, "wt", encoding="utf-8") as fh:
            fh.write('{"phase": "started"}\n')
        with gzip.open(recent_gz, "wt", encoding="utf-8") as fh:
            fh.write('{"phase": "started"}\n')

        cycle_ledger.append_event(tmp_path, {"phase": "started", "cycle_id": "new"})

        assert not old_gz.exists()
        assert recent_gz.exists()

    def test_retention_env_default_and_override(self, monkeypatch):
        monkeypatch.delenv("CYCLE_LEDGER_RETENTION_DAYS", raising=False)
        assert cycle_ledger._retention_days() == 90
        monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "10")
        assert cycle_ledger._retention_days() == 10
        monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "not-a-number")
        assert cycle_ledger._retention_days() == 90


# ─── light bridge integration ─────────────────────────────────────────────────


def _git(repo: Path) -> list[str]:
    return ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(_git(repo) + list(args), capture_output=True, text=True)


def _init_selfevo_repo(base: Path) -> tuple[Path, Path]:
    """Build a bare 'origin' + a working clone at base/'eeebot-self-evolving',
    matching bridge.py's ``STATE_DIR.parent / 'eeebot-self-evolving'`` layout.
    """
    origin = base / "origin.git"
    work = base / "eeebot-self-evolving"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _run(work, "config", "user.email", "bridge@test.local")
    _run(work, "config", "user.name", "bridge-test")
    _run(work, "checkout", "-B", "main")
    (work / "tests").mkdir()
    (work / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert True\n")
    (work / "mod.py").write_text("def ok():\n    return True\n")
    _run(work, "add", ".")
    _run(work, "commit", "-m", "init")
    _run(work, "push", "origin", "HEAD:main")
    return origin, work


def _seed_bridge_request(state_dir: Path, request_id: str, cycle_id: str, **extra) -> None:
    (state_dir / "outbox").mkdir(parents=True, exist_ok=True)
    (state_dir / "outbox" / "report.index.json").write_text(
        json.dumps({"source": "test-source", "goal": {"goal_id": "goal-1"}}), encoding="utf-8",
    )
    (state_dir / "goals").mkdir(parents=True, exist_ok=True)
    (state_dir / "goals" / "registry.json").write_text(
        json.dumps({"active_goal_id": "goal-1", "goals": {"goal-1": {"text": "test goal"}}}),
        encoding="utf-8",
    )
    (state_dir / "subagents" / "requests").mkdir(parents=True, exist_ok=True)
    req = {"request_id": request_id, "cycle_id": cycle_id, **extra}
    (state_dir / "subagents" / "requests" / f"{request_id}.json").write_text(
        json.dumps(req), encoding="utf-8",
    )


class _FakeSubagentManager:
    """Stand-in for nanobot.agent.subagent.SubagentManager: simulates a
    subagent that commits one real change directly to the (already
    cycle-branched) workspace repo, without spawning a real LLM turn.
    """

    def __init__(self, *, workspace, **_kwargs):
        self.workspace = workspace
        self._running_tasks: dict = {}

    async def spawn(self, **_kwargs):
        (self.workspace / "scripts").mkdir(exist_ok=True)
        (self.workspace / "scripts" / "feature.py").write_text("def feature():\n    return 42\n")
        _run(self.workspace, "add", "scripts/feature.py")
        _run(self.workspace, "commit", "-m", "feat: add feature")
        return "fake subagent spawned"


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    """Mirrors tests/test_bridge_cycle_branch.py: point the bounded gate's
    core-smoke set at the one test file these fixtures create.
    """
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


class TestBridgeIntegrationLedgerRows:
    def test_full_green_cycle_writes_started_gate_success(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        _init_selfevo_repo(base)

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        # No real LLM call happens (spawn() is faked above) — avoid the
        # "no API key configured" hard-exit in _make_provider by stubbing it.
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

        _seed_bridge_request(state_dir, "req-green", "cycle-green")

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        phases = [r["phase"] for r in rows]
        assert phases[0] == "started"
        assert "dedup" in phases
        assert "gate" in phases
        assert phases[-1] == "outcome"

        gate_rows = [r for r in rows if r["phase"] == "gate"]
        assert gate_rows[-1]["allowed"] is True

        outcome_rows = [r for r in rows if r["phase"] == "outcome"]
        assert outcome_rows[-1]["outcome"] == "success"
        assert "scripts/feature.py" in outcome_rows[-1]["files_changed"]

    def test_already_done_skip_writes_started_then_skipped(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        # No eeebot-self-evolving repo needed: _task_already_done is stubbed
        # directly, and _selfevo_repo_check.is_dir() is False so the
        # HEAD-on-main precondition is skipped.

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "_task_already_done", lambda *_a, **_k: True)

        _seed_bridge_request(
            state_dir, "req-dup", "cycle-dup", task_title="implement thing xyz already done",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        phases = [r["phase"] for r in rows]
        assert phases == ["started", "dedup", "outcome"]
        assert rows[1]["decision"] == "skipped_duplicate"
        assert rows[2]["outcome"] == "skipped-duplicate"

    def test_already_done_skip_invokes_llm_proposer(self, tmp_path, monkeypatch):
        """#707 canary fix: the already_done pre-spawn skip must also give the
        LLM proposer a chance to fire (a queue full of stale duplicates is
        novelty exhaustion too) — not just the no-pending-request path."""
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "_task_already_done", lambda *_a, **_k: True)

        calls = []
        monkeypatch.setattr(
            llm_proposer, "maybe_propose", lambda *a, **k: calls.append((a, k)) or False
        )

        _seed_bridge_request(
            state_dir, "req-dup2", "cycle-dup2", task_title="implement thing xyz already done",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert len(calls) == 1
        assert calls[0][0][0] == state_dir
