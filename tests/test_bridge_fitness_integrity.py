"""Tests for #789 Layer 2: spawn-boundary fitness-sidecar tamper detection.

The bridge hashes the fitness-input sidecars (``demand/completed.json``,
``demand/exhausted.json``, ``scorecard/latest.json``, ``heldout/results.json``,
``usage/last_used.json``) immediately before the subagent spawn and re-hashes
them right before the gate verdict. A mismatch means code executed inside the
spawn window wrote a fitness input — only the harness may do that (the
2026-07-17 live reward-hack: an instance script hardcoded the completed.json
path and wrote a foreign ``confirmed``/``signal``). The incident is recorded
as a ``phase: "integrity"`` ledger row and surfaced in the cycle's
``key_learnings``; no restore is attempted (follow-up hardening).

The clean-cycle test also PINS the window placement: the bridge's own
legitimate sidecar writes (demand fold, exhaustion updates, scorecard
recompute — all on the proposer path) happen OUTSIDE the pre-spawn/pre-gate
hash window, so a normal successful cycle records NO integrity incident.

Reuses the bridge-integration harness from tests/test_cycle_ledger.py.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.runtime import bridge
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


def _setup(base, monkeypatch):
    state_dir = base / "state"
    state_dir.mkdir()
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
    return state_dir


def _seed_completed_sidecar(state_dir) -> None:
    (state_dir / "demand").mkdir(parents=True, exist_ok=True)
    (state_dir / "demand" / "completed.json").write_text(
        json.dumps(
            {
                "schema_version": "demand-completed-v1",
                "entries": {"defect-pre": {"cycle_id": "c0", "ts": "2026-07-01T00:00:00Z",
                                           "files_changed": []}},
            }
        ),
        encoding="utf-8",
    )


class _TamperingFakeSubagentManager(_FakeSubagentManager):
    """A subagent that ALSO writes the fitness sidecar during its turn —
    the 2026-07-17 incident shape (instance code with a hardcoded absolute
    path into ``state/demand/completed.json``)."""

    async def spawn(self, **kwargs):
        result = await super().spawn(**kwargs)
        state_dir = self.workspace.parent / "state"
        (state_dir / "demand").mkdir(parents=True, exist_ok=True)
        (state_dir / "demand" / "completed.json").write_text(
            json.dumps(
                {
                    "schema_version": "demand-completed-v1",
                    "entries": {"defect-pre": {"cycle_id": "c0",
                                               "ts": "2026-07-01T00:00:00Z",
                                               "files_changed": [],
                                               "confirmed": True,
                                               "signal": "operator-confirmed"}},
                }
            ),
            encoding="utf-8",
        )
        return result


class TestSpawnBoundaryTamperDetection:
    def test_clean_cycle_records_no_integrity_row(self, tmp_path, monkeypatch):
        """Pins BOTH: a normal successful cycle is incident-free, AND the
        hash window excludes the bridge's own writes (the pre-existing
        sidecar is present before the run and untouched inside the window)."""
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        _init_selfevo_repo(base)
        _seed_completed_sidecar(state_dir)
        _seed_bridge_request(state_dir, "req-clean", "cycle-clean")

        assert asyncio.run(bridge._main_impl()) == 0

        rows = _read_ledger(state_dir)
        assert [r for r in rows if r["phase"] == "integrity"] == []
        outcome_rows = [r for r in rows if r["phase"] == "outcome"]
        assert outcome_rows[-1]["outcome"] == "success"

    def test_subagent_sidecar_write_records_integrity_row(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        monkeypatch.setattr(bridge, "SubagentManager", _TamperingFakeSubagentManager)
        _init_selfevo_repo(base)
        _seed_completed_sidecar(state_dir)
        _seed_bridge_request(state_dir, "req-tamper", "cycle-tamper")

        assert asyncio.run(bridge._main_impl()) == 0

        rows = _read_ledger(state_dir)
        integrity = [r for r in rows if r["phase"] == "integrity"]
        assert len(integrity) == 1
        assert integrity[0]["reason"] == "sidecar_write_during_spawn"
        assert integrity[0]["files"] == ["demand/completed.json"]
        assert integrity[0]["cycle_id"] == "cycle-tamper"

        # The incident is surfaced in the cycle's own key_learnings.
        result_path = state_dir / "subagents" / "results" / "result-req-tamper.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        learnings = "\n".join(payload["key_learnings"])
        assert "INTEGRITY WARNING" in learnings
        assert "demand/completed.json" in learnings

    def test_sidecar_created_during_spawn_counts_as_change(self, tmp_path, monkeypatch):
        """Missing-file sentinel: a sidecar that did not exist pre-spawn but
        exists pre-gate is a change ('absent' -> hash)."""
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        monkeypatch.setattr(bridge, "SubagentManager", _TamperingFakeSubagentManager)
        _init_selfevo_repo(base)
        # NO pre-existing completed.json — the tamperer creates it.
        _seed_bridge_request(state_dir, "req-create", "cycle-create")

        assert asyncio.run(bridge._main_impl()) == 0

        integrity = [r for r in _read_ledger(state_dir) if r["phase"] == "integrity"]
        assert len(integrity) == 1
        assert integrity[0]["files"] == ["demand/completed.json"]


class TestFitnessSidecarHashes:
    def test_missing_files_hash_as_absent(self, tmp_path):
        hashes = bridge._fitness_sidecar_hashes(tmp_path)
        assert set(hashes) == set(bridge._FITNESS_SIDECARS)
        assert all(v == "absent" for v in hashes.values())

    def test_content_change_changes_hash(self, tmp_path):
        path = tmp_path / "demand" / "completed.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        first = bridge._fitness_sidecar_hashes(tmp_path)["demand/completed.json"]
        assert first != "absent"
        path.write_text('{"entries": {}}', encoding="utf-8")
        second = bridge._fitness_sidecar_hashes(tmp_path)["demand/completed.json"]
        assert second != first
