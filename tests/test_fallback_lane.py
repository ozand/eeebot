"""Tests for #1411: the executor-always-runs fallback lane.

Covers the llm_proposer-level pieces: the two-prompt contract (a separate
fallback prompt without the demand-driven prompt's invention prohibition,
the demand-driven prompt itself unchanged), the kill switch, and
``propose_fallback``'s own gates (sizing, self-dedup/futility, write) and
fail-open behavior. Bridge-level wiring (the four trigger routes, at-most-
one-per-cycle, suppression pass-through) is covered separately in
``tests/test_bridge_fallback_lane.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.runtime import llm_proposer


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    (state_dir / "goals").mkdir(parents=True)
    return state_dir


_VALID_PROPOSAL = {
    "task_title": "Document the fallback lane",
    "rationale": "Improves operator understanding of the new lane.",
    "target_path": "docs/fallback-lane-notes.md",
    "serves": "vector 1",
}


@pytest.fixture(autouse=True)
def _parent_proposer_switch_on(monkeypatch):
    """propose_fallback additionally gates on the parent ENABLED_ENV switch
    (SELFEVO_LLM_PROPOSER_ENABLED, default OFF) — every pre-#1411 bridge
    test never touches it, so this lane costs them nothing without
    individually disabling it. The tests in THIS file exercise the lane
    itself, so they need the parent switch on; the parent-gating contract
    itself is pinned separately in TestFallbackLaneKillSwitch below, which
    overrides this back to off where needed."""
    monkeypatch.setenv(llm_proposer.ENABLED_ENV, "1")


# ─── two prompts, not one edited prompt ─────────────────────────────────────


class TestTwoPromptContract:
    def test_demand_driven_prompt_forbids_invention(self):
        assert "MUST NOT invent" in llm_proposer._DEMAND_PROPOSER_SYSTEM_PROMPT

    def test_fallback_prompt_has_no_invention_prohibition(self):
        """propose_fallback uses _PROPOSER_SYSTEM_PROMPT (the pre-#760
        prompt, unmodified) — it must not carry the demand-driven prompt's
        "MUST NOT invent" sentence."""
        assert "MUST NOT invent" not in llm_proposer._PROPOSER_SYSTEM_PROMPT

    def test_fallback_prompt_and_demand_prompt_are_distinct_strings(self):
        assert llm_proposer._PROPOSER_SYSTEM_PROMPT != llm_proposer._DEMAND_PROPOSER_SYSTEM_PROMPT

    def test_propose_fallback_uses_the_no_invention_prompt(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        captured = {}

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, system_prompt=None):
            captured["system_prompt"] = system_prompt
            return dict(_VALID_PROPOSAL)

        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "some context")
        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        monkeypatch.setattr(llm_proposer, "_is_duplicate_proposal", lambda *a, **k: (False, "", ""))

        result = llm_proposer.propose_fallback(state_dir, None)

        assert result is not None
        assert captured["system_prompt"] == llm_proposer._PROPOSER_SYSTEM_PROMPT
        assert captured["system_prompt"] != llm_proposer._DEMAND_PROPOSER_SYSTEM_PROMPT


# ─── kill switch ─────────────────────────────────────────────────────────────


class TestFallbackLaneKillSwitch:
    def test_default_is_on_when_parent_switch_is_on(self, monkeypatch):
        monkeypatch.delenv(llm_proposer.FALLBACK_LANE_ENABLED_ENV, raising=False)
        assert llm_proposer.fallback_lane_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv(llm_proposer.FALLBACK_LANE_ENABLED_ENV, "0")
        assert llm_proposer.fallback_lane_enabled() is False

    def test_off_when_parent_proposer_switch_is_off(self, monkeypatch):
        """The parent SELFEVO_LLM_PROPOSER_ENABLED switch (default OFF)
        gates this lane too — every existing bridge test that never sets it
        must see zero fallback-lane behavior, matching pre-#1411 exactly."""
        monkeypatch.setenv(llm_proposer.ENABLED_ENV, "0")
        monkeypatch.delenv(llm_proposer.FALLBACK_LANE_ENABLED_ENV, raising=False)
        assert llm_proposer.fallback_lane_enabled() is False

    def test_off_short_circuits_before_any_llm_call(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setenv(llm_proposer.FALLBACK_LANE_ENABLED_ENV, "0")

        def _boom(*_a, **_k):
            raise AssertionError("propose() must not be called when the fallback lane is off")

        monkeypatch.setattr(llm_proposer, "propose", _boom)
        monkeypatch.setattr(llm_proposer, "build_context", _boom)

        assert llm_proposer.propose_fallback(state_dir, None) is None


# ─── propose_fallback: success path ──────────────────────────────────────────


class TestProposeFallbackSuccess:
    def test_writes_a_request_labelled_fallback(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "some context")
        monkeypatch.setattr(llm_proposer, "propose", lambda *a, **k: dict(_VALID_PROPOSAL))
        monkeypatch.setattr(llm_proposer, "_is_duplicate_proposal", lambda *a, **k: (False, "", ""))

        result = llm_proposer.propose_fallback(state_dir, None)

        assert result is not None
        request_path, req = result
        assert Path(request_path).is_file()
        assert req["lane"] == "fallback"
        assert req["cycle_id"].startswith("fallback-")
        assert _VALID_PROPOSAL["task_title"] in req["task_title"]
        # Round-trips to the same content find_pending_request would see.
        on_disk = json.loads(Path(request_path).read_text(encoding="utf-8"))
        assert on_disk == req

    def test_cycle_id_is_unique_per_call(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "some context")
        monkeypatch.setattr(llm_proposer, "propose", lambda *a, **k: dict(_VALID_PROPOSAL))
        monkeypatch.setattr(llm_proposer, "_is_duplicate_proposal", lambda *a, **k: (False, "", ""))

        first = llm_proposer.propose_fallback(state_dir, None)
        second = llm_proposer.propose_fallback(state_dir, None)
        assert first[1]["cycle_id"] != second[1]["cycle_id"]

    def test_non_fallback_write_request_call_is_unaffected(self, tmp_path):
        """Regression pin: TestWriteRequestSchemaEquality's canonical key set
        must stay exact for a caller that never passes lane=..."""
        state_dir = _state_dir(tmp_path)
        path = llm_proposer.write_request(state_dir, dict(_VALID_PROPOSAL))
        written = json.loads(Path(path).read_text(encoding="utf-8"))
        assert "lane" not in written


# ─── propose_fallback: rejection / suppression paths (fail-open) ───────────


class TestProposeFallbackFailsOpen:
    def test_context_build_failure_degrades_to_none(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "")

        def _boom(*_a, **_k):
            raise AssertionError("propose() must not be called with no context")

        monkeypatch.setattr(llm_proposer, "propose", _boom)
        assert llm_proposer.propose_fallback(state_dir, None) is None

    def test_no_valuable_task_reply_degrades_to_none(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "some context")
        monkeypatch.setattr(
            llm_proposer, "propose",
            lambda *a, **k: {"no_valuable_task": True, "reason": "nothing to do"},
        )
        requests_before = list((state_dir / "subagents" / "requests").glob("*.json")) \
            if (state_dir / "subagents" / "requests").is_dir() else []
        assert llm_proposer.propose_fallback(state_dir, None) is None
        requests_after = list((state_dir / "subagents" / "requests").glob("*.json")) \
            if (state_dir / "subagents" / "requests").is_dir() else []
        assert requests_after == requests_before

    def test_invalid_sizing_degrades_to_none_no_retry(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        calls = []

        def _fake_propose(*a, **k):
            calls.append(1)
            return {"task_title": "", "rationale": "x", "target_path": "docs/x.md", "serves": "vector 1"}

        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "some context")
        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)

        assert llm_proposer.propose_fallback(state_dir, None) is None
        # Exactly one LLM call — no retry-with-feedback loop (unlike maybe_propose).
        assert len(calls) == 1

    def test_self_dedup_rejection_degrades_to_none(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "some context")
        monkeypatch.setattr(llm_proposer, "propose", lambda *a, **k: dict(_VALID_PROPOSAL))
        monkeypatch.setattr(
            llm_proposer, "_is_duplicate_proposal",
            lambda *a, **k: (True, "matches recent work", "some prior commit subject"),
        )
        assert llm_proposer.propose_fallback(state_dir, None) is None
        assert not (state_dir / "subagents" / "requests").is_dir() or not list(
            (state_dir / "subagents" / "requests").glob("*.json")
        )

    def test_futile_surface_rejection_degrades_to_none(self, tmp_path, monkeypatch):
        """The #1184 futile-surface refusal lives inside _is_duplicate_proposal
        itself — reusing that function gives the fallback lane futility
        suppression for free, without a separate call into
        goal_gap_futility.py (which #1411 is not allowed to touch)."""
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "some context")
        monkeypatch.setattr(llm_proposer, "propose", lambda *a, **k: dict(_VALID_PROPOSAL))
        monkeypatch.setattr(
            llm_proposer, "_is_duplicate_proposal",
            lambda *a, **k: (True, "futile surface", "futile_surface:gap-1"),
        )
        assert llm_proposer.propose_fallback(state_dir, None) is None

    def test_propose_raising_degrades_to_none_never_raises(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "some context")

        def _boom(*_a, **_k):
            raise RuntimeError("network exploded")

        monkeypatch.setattr(llm_proposer, "propose", _boom)
        assert llm_proposer.propose_fallback(state_dir, None) is None

    def test_build_context_raising_degrades_to_none_never_raises(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)

        def _boom(*_a, **_k):
            raise RuntimeError("disk exploded")

        monkeypatch.setattr(llm_proposer, "build_context", _boom)
        assert llm_proposer.propose_fallback(state_dir, None) is None


# ─── cycle_ledger.record_cycle_outcome: additive ``lane`` field ────────────


class TestRecordCycleOutcomeLaneField:
    def test_lane_omitted_by_default(self, tmp_path):
        from nanobot.runtime import cycle_ledger

        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], None)
        rows = [
            json.loads(line)
            for line in (tmp_path / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert "lane" not in rows[0]

    def test_lane_recorded_when_passed(self, tmp_path):
        from nanobot.runtime import cycle_ledger

        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], None, lane="fallback")
        rows = [
            json.loads(line)
            for line in (tmp_path / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert rows[0]["lane"] == "fallback"
