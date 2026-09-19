"""#1662: Structural ledger observability for error card recording outcomes.

Tests that error card generation/push during post-cycle outcome handling emits
a durable cycle_ledger event with phase: 'error_card_recording' and status:
- 'created' on successful creation & push (``card_commit`` names the commit)
- 'not_created' with skip_reason on rejection / failure

Rows are written only on rollback cycles: a cycle with nothing to record
must not read the same as one that recorded nothing.

The card is committed and pushed from an isolated checkout of origin/main
under the state dir, so a cycle branch that already carries executor
commits still ships its card and the live working tree is left exactly as
the cycle left it (no new untracked or modified files).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from unittest.mock import patch

import pytest

from nanobot.runtime import bridge
from tests.test_bridge_executor_llm_error import _LLMBadRequestSubagentManager
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
         patch("nanobot.runtime.bridge._write_structured_error", return_value={"status": "created", "error_id": "ERR-x", "error": None}), \
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
         patch("nanobot.runtime.bridge._write_structured_error", return_value={"status": "created", "error_id": "ERR-x", "error": None}), \
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
         patch("nanobot.runtime.bridge._write_structured_error", return_value={"status": "created", "error_id": "ERR-x", "error": None}), \
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
    """#1710 acceptance: a write that actually raises names the exception
    class and path in `error`, distinct from `already_recorded` below."""
    state_dir = _wire(tmp_path, monkeypatch)
    title = "Test task failure where write_structured_error returns False"
    artifact = tmp_path / "improvements" / "llm-proposed-cycle-wf.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
    _seed_bridge_request(
        state_dir, "req-wf", "cycle-wf", task_title=title, source_artifact=str(artifact),
    )

    with patch("nanobot.runtime.bridge._run_smoke_tests_with_shrink_guard", return_value=(False, "smoke failed")), \
         patch(
             "nanobot.runtime.bridge._write_structured_error",
             return_value={"status": "write_failed", "error_id": "ERR-wf", "error": "PermissionError:/tmp/lessons/errors.yaml"},
         ):
        asyncio.run(bridge._main_impl())

    rows = _read_ledger(state_dir)
    card_events = [r for r in rows if r.get("phase") == "error_card_recording"]
    assert len(card_events) == 1
    ev = card_events[0]
    assert ev["status"] == "not_created"
    assert ev["skip_reason"] == "write_failed"
    assert ev["cycle_id"] == "cycle-wf"
    assert ev["error"] == "PermissionError:/tmp/lessons/errors.yaml"
    assert "card_id" not in ev


def test_error_card_recording_already_recorded_on_retry(tmp_path, monkeypatch):
    """#1710: an executor retry re-running the same cycle_id finds the prior
    attempt's card already on origin/main. This must not read as write_failed
    -- it is proof the retry ran and found the card, not a lost record."""
    state_dir = _wire(tmp_path, monkeypatch)
    title = "Test task failure where the card was already recorded on a prior attempt"
    artifact = tmp_path / "improvements" / "llm-proposed-cycle-ar.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
    _seed_bridge_request(
        state_dir, "req-ar", "cycle-ar", task_title=title, source_artifact=str(artifact),
    )

    with patch("nanobot.runtime.bridge._run_smoke_tests_with_shrink_guard", return_value=(False, "smoke failed")), \
         patch(
             "nanobot.runtime.bridge._write_structured_error",
             return_value={"status": "already_recorded", "error_id": "ERR-20260917-cyclearx", "error": None},
         ):
        asyncio.run(bridge._main_impl())

    rows = _read_ledger(state_dir)
    card_events = [r for r in rows if r.get("phase") == "error_card_recording"]
    assert len(card_events) == 1
    ev = card_events[0]
    assert ev["status"] == "already_recorded"
    assert ev["card_id"] == "ERR-20260917-cyclearx"
    assert ev["cycle_id"] == "cycle-ar"
    assert "skip_reason" not in ev
    assert "error" not in ev



def test_error_card_recording_created_then_already_recorded_across_retries(tmp_path, monkeypatch):
    """#1710 acceptance: an executor-LLM-error retry re-running the same
    cycle_id finds the prior attempt's card already pushed to origin/main.
    Real git flow (no _write_structured_error/_push_main_or_report mocking):
    attempt 1 creates and pushes the card; attempts 2 and 3 must read
    already_recorded -- never write_failed -- and every row carries the
    attempt number that produced it, out of LLM_ERROR_MAX_RETRIES.

    #1765: uses the OUR-OWN-DEFECT fixture (_LLMBadRequestSubagentManager),
    not the connection-error one -- a supplier-side error now classifies as
    'paused-supplier' and writes no card at all (see
    tests/test_paused_supplier_1765.py); this test's own subject, the
    error-card-across-retries mechanism, only ever applied to the 'failed'
    class to begin with.
    """
    state_dir = _wire(tmp_path, monkeypatch, _LLMBadRequestSubagentManager)
    monkeypatch.setattr(bridge, "LLM_ERROR_MAX_RETRIES", 3)
    title = "Retry same cycle three times, card recorded once"
    artifact = tmp_path / "improvements" / "llm-proposed-cycle-retry-card.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
    _seed_bridge_request(
        state_dir, "req-retry-card", "cycle-retry-card", task_title=title, source_artifact=str(artifact),
    )

    for _ in range(3):
        asyncio.run(bridge._main_impl())

    rows = _read_ledger(state_dir)
    card_events = [r for r in rows if r.get("phase") == "error_card_recording"]
    assert len(card_events) == 3
    assert [e["status"] for e in card_events] == ["created", "already_recorded", "already_recorded"]
    assert [e.get("skip_reason") for e in card_events] == [None, None, None]
    assert [e["attempt"] for e in card_events] == ["1/3", "2/3", "3/3"]
    assert {e["cycle_id"] for e in card_events} == {"cycle-retry-card"}
    assert "card_id" not in card_events[0]
    assert card_events[1]["card_id"] == card_events[2]["card_id"]
    assert card_events[1]["card_id"]


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
    assert "skip_reason" not in ev

    # Remote origin/main has the error card, not the unintegrated commit,
    # and the ledger names the very commit that landed.
    subprocess.run(["git", "-C", str(repo), "fetch", "origin"], check=True, capture_output=True)
    out = subprocess.run(["git", "-C", str(repo), "log", "origin/main", "--oneline"], capture_output=True, text=True, check=True).stdout
    assert "chore: record structured error for [cycle-extra]" in out
    assert "cycle commit" not in out
    remote_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "origin/main"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert ev["card_commit"] == remote_head
    shipped = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", f"{remote_head}~1", remote_head],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert shipped == ["lessons/errors.yaml"]

    # The live checkout was not the vehicle: nothing untracked, nothing
    # modified, no leftover isolated worktree or registration.
    porcelain = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert porcelain == "", porcelain
    assert not (state_dir / "error_card_wt").exists()
    worktrees = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"], capture_output=True, text=True, check=True,
    ).stdout
    assert worktrees.count("worktree ") == 1, worktrees
    assert not (repo / "lessons" / "index.md").exists()

    # The next cycle's restore (which fast-forwards local main onto the
    # shipped card) must still find a clean tree — the exact precondition
    # that blocked tests/test_bridge_executor_llm_error.py at d9bd5f61.
    assert bridge._restore_to_main(repo, state_dir) is True
    assert not (repo / "lessons" / "index.md").exists()
    assert (repo / "lessons" / "errors.yaml").exists()


def test_error_card_f6_guard_still_refuses_extra_files_on_isolated_ref(tmp_path, monkeypatch):
    """#678 F6 is preserved: a card commit that carries anything beyond
    lessons/errors.yaml (and archive entries) is refused, and nothing is
    pushed. The extra file is planted by the writer itself, i.e. inside the
    isolated checkout, which is the only place the guard now looks."""
    state_dir = _wire(tmp_path, monkeypatch)
    repo = tmp_path / "eeebot-self-evolving"
    title = "Test task failure where the card commit smuggles a file"
    artifact = tmp_path / "improvements" / "smuggle.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
    _seed_bridge_request(
        state_dir, "req-smuggle", "cycle-smuggle", task_title=title, source_artifact=str(artifact),
    )

    real_writer = bridge._write_structured_error

    def _writer_with_stowaway(repo_root, **kwargs):
        ok = real_writer(repo_root=repo_root, **kwargs)
        (repo_root / "lessons" / "stowaway.py").write_text("print('no')", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_root), "add", "lessons/stowaway.py"], check=True, capture_output=True)
        return ok

    with patch("nanobot.runtime.bridge._run_smoke_tests_with_shrink_guard", return_value=(False, "smoke failed")), \
         patch("nanobot.runtime.bridge._write_structured_error", side_effect=_writer_with_stowaway):
        asyncio.run(bridge._main_impl())

    rows = _read_ledger(state_dir)
    card_events = [r for r in rows if r.get("phase") == "error_card_recording"]
    assert len(card_events) == 1
    ev = card_events[0]
    assert ev["status"] == "not_created"
    assert ev["skip_reason"] == "diff_touched_more_than_errors_yaml"

    subprocess.run(["git", "-C", str(repo), "fetch", "origin"], check=True, capture_output=True)
    out = subprocess.run(["git", "-C", str(repo), "log", "origin/main", "--oneline"], capture_output=True, text=True, check=True).stdout
    assert "record structured error" not in out
    assert not (state_dir / "error_card_wt").exists()
    porcelain = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert porcelain == "", porcelain
