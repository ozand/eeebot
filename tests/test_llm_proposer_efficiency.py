import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from nanobot.runtime import llm_proposer

def _write_ledger(state_dir: Path, rows: list[dict]):
    d = state_dir / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "cycles.jsonl").open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

class TestLLMProposerEfficiency:
    def test_ac1_dedup_exhausted_skips_before_llm_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llm_proposer, "_dedup_exhaustion_k", lambda: 3)
        state_dir = tmp_path / "state"
        (state_dir / "goals").mkdir(parents=True)
        from tests.test_llm_proposer import _write_goal_text
        _write_goal_text(state_dir, {"priority": []})
        demand_id = "defect-1"
        now = datetime.now(timezone.utc)
        rows = []
        for i in range(3):
            ts = (now - timedelta(hours=i+1)).isoformat()
            rows.append({"phase": "proposer_reject", "reason": "self_dedup", "demand_id": demand_id, "ts": ts})
        _write_ledger(state_dir, rows)
        def _boom(*args, **kwargs): raise AssertionError("LLM should not be called!")
        monkeypatch.setattr(llm_proposer, "propose", _boom)
        monkeypatch.setattr('nanobot.runtime.demand.collect_demand', lambda w, s, emit_split: [{"id": demand_id}])
        monkeypatch.setattr('nanobot.runtime.llm_proposer._select_assigned_demand', lambda s, items: items[:1])
        res = llm_proposer.maybe_propose(state_dir, None)
        assert res is None
        with (state_dir / "ledger" / "cycles.jsonl").open(encoding="utf-8") as f:
            last = json.loads(f.read().splitlines()[-1])
        assert last["phase"] == "proposer_skip"

    def test_ac2_shorter_history_gets_llm_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llm_proposer, "_dedup_exhaustion_k", lambda: 3)
        state_dir = tmp_path / "state"
        (state_dir / "goals").mkdir(parents=True)
        from tests.test_llm_proposer import _write_goal_text
        _write_goal_text(state_dir, {"priority": []})
        demand_id = "defect-2"
        now = datetime.now(timezone.utc)
        rows = []
        for i in range(2):
            ts = (now - timedelta(hours=i+1)).isoformat()
            rows.append({"phase": "proposer_reject", "reason": "self_dedup", "demand_id": demand_id, "ts": ts})
        _write_ledger(state_dir, rows)
        called = []
        def _propose(*args, **kwargs):
            called.append(True)
            return {"title": "Valid", "detail": "Valid", "plan": [], "serves": [demand_id]}
        monkeypatch.setattr(llm_proposer, "propose", _propose)
        monkeypatch.setattr('nanobot.runtime.demand.collect_demand', lambda w, s, emit_split: [{"id": demand_id}])
        monkeypatch.setattr('nanobot.runtime.llm_proposer._select_assigned_demand', lambda s, items: items[:1])
        llm_proposer.maybe_propose(state_dir, None)
        assert called

    def test_ac3_network_failure_records_unavailable(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        (state_dir / "goals").mkdir(parents=True)
        from tests.test_llm_proposer import _write_goal_text
        _write_goal_text(state_dir, {"priority": []})
        demand_id = "defect-3"
        class GatewayTimeout(Exception): pass
        def _boom(*args, **kwargs): raise GatewayTimeout("gateway timeout")
        monkeypatch.setattr(llm_proposer, "propose", _boom)
        monkeypatch.setattr('nanobot.runtime.demand.collect_demand', lambda w, s, emit_split: [{"id": demand_id}])
        monkeypatch.setattr('nanobot.runtime.llm_proposer._select_assigned_demand', lambda s, items: items[:1])
        llm_proposer.maybe_propose(state_dir, None)
        with (state_dir / "ledger" / "cycles.jsonl").open(encoding="utf-8") as f:
            last = json.loads(f.read().splitlines()[-1])
        assert last["phase"] == "proposer_reject"
        assert last["reason"] == "llm_unavailable"

    def test_ac4_scorecard_excludes_skips_and_unavailable(self, tmp_path):
        from nanobot.runtime.scorecard import _loop_section
        state_dir = tmp_path / "state"
        rows = [
            {"phase": "proposer_skip", "reason": "dedup_exhausted", "demand_id": "d-1"},
            {"phase": "proposer_reject", "reason": "llm_unavailable", "demand_id": "d-2"}
        ]
        _write_ledger(state_dir, rows)
        sc = _loop_section(rows)
        assert sc.get("duplicate_failure_skips", 0) == 0
        assert sc.get("self_dedup_rejects", 0) == 0
