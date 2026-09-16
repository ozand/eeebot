"""#1662: Structural ledger observability for error card recording outcomes.

Tests that error card generation/push during post-cycle outcome handling emits
a durable cycle_ledger event with phase: 'error_card_recording' and status:
- 'created' on successful creation & push
- 'not_created' with skip_reason on rejection / failure
- 'no_attempt' when no rollback reason is present
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from unittest.mock import patch

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


def _wire(tmp_path, monkeypatch, manager_cls=_FakeSubagentManager):
    base = tmp_path
    state_dir = base / "state"
    state_dir.mkdir(exist_ok=True)
    _init_selfevo_repo(base)
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    monkeypatch.setattr(bridge, "SubagentManager", manager_cls)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
    return state_dir


def test_error_card_recording_created(tmp_path, monkeypatch):
    state_dir = _wire(tmp_path, monkeypatch)
    title = "Test task failure leading to error card created"
    artifact = tmp_path / "improvements" / "llm-proposed-cycle-fail.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
    _seed_bridge_request(
        state_dir, "req-fail", "cycle-fail", task_title=title, source_artifact=str(artifact),
    )

    with patch("nanobot.runtime.bridge._run_smoke_tests_with_shrink_guard", return_value=(False, "smoke failed")), \
         patch("nanobot.runtime.bridge._write_structured_error", return_value=True), \
         patch("nanobot.runtime.bridge._diff_against_remote_touches_only", return_value=True), \
         patch("nanobot.runtime.bridge._push_main_or_report", return_value=True):
        asyncio.run(bridge._main_impl())

    rows = _read_ledger(state_dir)
    card_events = [r for r in rows if r.get("phase") == "error_card_recording"]
    assert len(card_events) == 1
    ev = card_events[0]
    assert ev["status"] == "created"
    assert ev["cycle_id"] == "cycle-fail"
    assert ev.get("rollback_reason") is not None


def test_error_card_recording_not_created_diff_mismatch(tmp_path, monkeypatch):
    state_dir = _wire(tmp_path, monkeypatch)
    title = "Test task failure where diff touches more than errors.yaml"
    artifact = tmp_path / "improvements" / "llm-proposed-cycle-diff.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
    _seed_bridge_request(
        state_dir, "req-diff", "cycle-diff", task_title=title, source_artifact=str(artifact),
    )

    with patch("nanobot.runtime.bridge._run_smoke_tests_with_shrink_guard", return_value=(False, "smoke failed")), \
         patch("nanobot.runtime.bridge._write_structured_error", return_value=True), \
         patch("nanobot.runtime.bridge._diff_against_remote_touches_only", return_value=False):
        asyncio.run(bridge._main_impl())

    rows = _read_ledger(state_dir)
    card_events = [r for r in rows if r.get("phase") == "error_card_recording"]
    assert len(card_events) == 1
    ev = card_events[0]
    assert ev["status"] == "not_created"
    assert ev["skip_reason"] == "diff_touched_more_than_errors_yaml"
    assert ev["cycle_id"] == "cycle-diff"


def test_error_card_recording_not_created_push_rejected(tmp_path, monkeypatch):
    state_dir = _wire(tmp_path, monkeypatch)
    title = "Test task failure where push is rejected"
    artifact = tmp_path / "improvements" / "llm-proposed-cycle-push.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
    _seed_bridge_request(
        state_dir, "req-push", "cycle-push", task_title=title, source_artifact=str(artifact),
    )

    with patch("nanobot.runtime.bridge._run_smoke_tests_with_shrink_guard", return_value=(False, "smoke failed")), \
         patch("nanobot.runtime.bridge._write_structured_error", return_value=True), \
         patch("nanobot.runtime.bridge._diff_against_remote_touches_only", return_value=True), \
         patch("nanobot.runtime.bridge._push_main_or_report", return_value=False):
        asyncio.run(bridge._main_impl())

    rows = _read_ledger(state_dir)
    card_events = [r for r in rows if r.get("phase") == "error_card_recording"]
    assert len(card_events) == 1
    ev = card_events[0]
    assert ev["status"] == "not_created"
    assert ev["skip_reason"] == "push_rejected"
    assert ev["cycle_id"] == "cycle-push"


def test_error_card_recording_not_created_write_failed(tmp_path, monkeypatch):
    state_dir = _wire(tmp_path, monkeypatch)
    title = "Test task failure where write_structured_error returns False"
    artifact = tmp_path / "improvements" / "llm-proposed-cycle-wf.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
    _seed_bridge_request(
        state_dir, "req-wf", "cycle-wf", task_title=title, source_artifact=str(artifact),
    )

    with patch("nanobot.runtime.bridge._run_smoke_tests_with_shrink_guard", return_value=(False, "smoke failed")), \
         patch("nanobot.runtime.bridge._write_structured_error", return_value=False):
        asyncio.run(bridge._main_impl())

    rows = _read_ledger(state_dir)
    card_events = [r for r in rows if r.get("phase") == "error_card_recording"]
    assert len(card_events) == 1
    ev = card_events[0]
    assert ev["status"] == "not_created"
    assert ev["skip_reason"] == "write_failed"
    assert ev["cycle_id"] == "cycle-wf"



def test_error_card_recording_created_with_unintegrated_cycle_commits(tmp_path, monkeypatch):
    state_dir = _wire(tmp_path, monkeypatch)
    repo = tmp_path / "eeebot-self-evolving"
    title = "Test task failure with extra cycle commits"
    artifact = tmp_path / "improvements" / "extra.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
    _seed_bridge_request(
        state_dir, "req-extra", "cycle-extra", task_title=title, source_artifact=str(artifact),
    )

    # Fake subagent adds an unintegrated commit to the cycle branch
    class _UnintegratedSubagentManager(_FakeSubagentManager):
        def _run_subagent(self, task_id, prompt):
            # Write and commit an unrelated file on the cycle branch
            unrelated = repo / "unrelated.py"
            unrelated.write_text("print('cycle extra')", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "unrelated.py"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "cycle commit"], check=True, capture_output=True)
            return super()._run_subagent(task_id, prompt)

    monkeypatch.setattr(bridge, "SubagentManager", _UnintegratedSubagentManager)

    with patch("nanobot.runtime.bridge._run_smoke_tests_with_shrink_guard", return_value=(False, "smoke failed")):
        asyncio.run(bridge._main_impl())

    rows = _read_ledger(state_dir)
    card_events = [r for r in rows if r.get("phase") == "error_card_recording"]
    assert len(card_events) == 1, f"Expected 1 card event, got {card_events}"
    ev = card_events[0]
    assert ev["status"] == "created"
    assert "error_id" in ev

    # Remote origin/main has the error card, not the unintegrated commit
    subprocess.run(["git", "-C", str(repo), "fetch", "origin"], check=True, capture_output=True)
    out = subprocess.run(["git", "-C", str(repo), "log", "origin/main", "--oneline"], capture_output=True, text=True, check=True).stdout
    assert "chore: record structured error for [cycle-extra]" in out
    assert "cycle commit" not in out
