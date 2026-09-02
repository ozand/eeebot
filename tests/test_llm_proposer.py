"""Tests for #707: the state-light LLM proposer.

Covers the kill-switch (default OFF), the invocation policy
(``should_propose``), the bounded context builder (``build_context``), the
pre-spawn sizing gate (``validate_sizing``), the C1 request-schema
invariant (``write_request`` emits the canonical ``subagent-request-v1``
shape the bridge consumes; #747 deleted the deterministic planner, leaving
the proposer as the sole request writer), the mocked-LLM ``propose``
parsing, and an end-to-end check that a proposer-written request is picked
up by the bridge's real ``find_pending_request``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import bridge, cycle_ledger, demand, llm_proposer
from tests.test_goal_backlog_routing import GOAL_TEXT_JSON, _make_git_repo_with_commit

ENV_VAR = llm_proposer.ENABLED_ENV
DEMAND_ENV = demand.ENABLED_ENV


@pytest.fixture(autouse=True)
def _pre_760_mode(monkeypatch):
    """#760: the tests in this module (written for #707-#762) pin the exact
    pre-#760 supply-driven behavior, which now lives behind
    ``SELFEVO_DEMAND_DRIVEN_ENABLED=0`` — the kill-switch-OFF contract this
    fixture is the regression suite for. Demand-driven-mode tests (see
    ``TestDemandDrivenMode`` below and ``tests/test_demand.py``) re-enable
    the switch inside their own bodies. Also resets the once-per-process
    idle-heartbeat marker so tests are order-independent."""
    monkeypatch.setenv(DEMAND_ENV, "0")
    monkeypatch.setattr(llm_proposer, "_idle_recorded_this_process", False)


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    (state_dir / "goals").mkdir(parents=True)
    return state_dir


def _write_goal_text(state_dir: Path, text: str) -> None:
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"text": text}), encoding="utf-8"
    )


def _append_proposed(state_dir: Path, cycle_id: str, task_title: str) -> None:
    cycle_ledger.append_event(
        state_dir,
        {
            "phase": "proposed",
            "cycle_id": cycle_id,
            "task_title": task_title,
            "source_artifact": "llm_proposer",
        },
    )


def _append_outcome(state_dir: Path, cycle_id: str, outcome: str, **extra) -> None:
    cycle_ledger.append_event(
        state_dir, {"phase": "outcome", "cycle_id": cycle_id, "outcome": outcome, **extra}
    )


def _write_usage_sidecar(state_dir: Path, entries: dict) -> None:
    usage_dir = state_dir / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    (usage_dir / "last_used.json").write_text(
        json.dumps({"schema_version": "usage-evidence-v1", "entries": entries}),
        encoding="utf-8",
    )


def _append_skip(state_dir: Path, reason: str = "nothing valuable") -> None:
    cycle_ledger.append_event(state_dir, {"phase": "proposer_skip", "reason": reason})


def _append_gate(state_dir: Path, cycle_id: str, allowed: bool, reason: str = "") -> None:
    cycle_ledger.append_event(
        state_dir,
        {"phase": "gate", "cycle_id": cycle_id, "allowed": allowed, "reason": reason},
    )


# ─── kill-switch ───────────────────────────────────────────────────────────


class TestKillSwitch:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert llm_proposer._enabled() is False

    def test_should_propose_false_when_off_even_with_favorable_conditions(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priorities section here at all")
        assert llm_proposer.should_propose(state_dir, None) is False

    def test_maybe_propose_noop_when_off(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priorities section here at all")
        assert llm_proposer.maybe_propose(state_dir, None) is None
        assert not (state_dir / "subagents" / "requests").exists()


# ─── should_propose branches (flag ON) ─────────────────────────────────────


class TestShouldPropose:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")

    def test_missing_state_dir_returns_false(self, tmp_path):
        assert llm_proposer.should_propose(tmp_path / "does-not-exist", None) is False

    def test_queued_planner_request_no_longer_blocks(self, tmp_path):
        """#707 canary fix: a stale PLANNER-written request queued does NOT
        block the proposer — a queue full of planner duplicates is itself
        the novelty-exhaustion signal should_propose exists to catch."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section")
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "request-x.json").write_text(
            json.dumps({"request_status": "queued", "request_id": "cycle-abc123"}),
            encoding="utf-8",
        )
        assert llm_proposer.should_propose(state_dir, None) is True

    def test_queued_proposer_request_blocks_anti_stacking(self, tmp_path):
        """A queued request the proposer itself already wrote DOES block a
        second proposal (anti-stacking guard)."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section")
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "request-y.json").write_text(
            json.dumps(
                {"request_status": "queued", "request_id": "llm-proposer-cycle-abc123"}
            ),
            encoding="utf-8",
        )
        assert llm_proposer.should_propose(state_dir, None) is False

    def test_queued_proposer_request_by_source_artifact_blocks(self, tmp_path):
        """The anti-stacking guard also matches on source_artifact filename
        (llm-proposed-*) alone, for a request missing the request_id marker."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section")
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "request-z.json").write_text(
            json.dumps(
                {
                    "request_status": "queued",
                    "source_artifact": "/some/path/llm-proposed-cycle-abc123.json",
                }
            ),
            encoding="utf-8",
        )
        assert llm_proposer.should_propose(state_dir, None) is False

    def test_priorities_remain_no_dup_streak_but_empty_queue_returns_true(self, tmp_path):
        """#745: changed expectation. Previously (no queue-empty clause) this
        returned False — priorities remain and there's no dup streak. Now the
        requests dir has never even been created (nothing queued at all), so
        the new queue-empty clause (spec R28) fires regardless of the
        priorities/dup-streak state — this IS the fresh-priorities-deadlock
        case the fix targets."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, GOAL_TEXT_JSON and json.loads(GOAL_TEXT_JSON)["text"])
        # No selfevo repo -> filter is a no-op -> priorities remain.
        # Fewer than 3 terminal rows, so the duplicate-streak branch is False too.
        _append_outcome(state_dir, "c1", "success")
        assert llm_proposer.should_propose(state_dir, None) is True

    def test_priorities_remain_no_dup_streak_but_queue_nonempty_returns_false(self, tmp_path):
        """Unchanged behavior: an unhandled non-proposer (e.g. planner)
        request is still queued, so the queue is NOT empty — falls through
        to the unchanged priorities/dup-streak fallback clauses, both False."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, GOAL_TEXT_JSON and json.loads(GOAL_TEXT_JSON)["text"])
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "request-planner.json").write_text(
            json.dumps({"request_status": "queued", "request_id": "cycle-planner-1"}),
            encoding="utf-8",
        )
        _append_outcome(state_dir, "c1", "success")
        assert llm_proposer.should_propose(state_dir, None) is False

    def test_priorities_empty_returns_true(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "eeebot mission text with no priority-targets section.")
        assert llm_proposer.should_propose(state_dir, None) is True

    def test_last_three_all_skipped_duplicate_returns_true(self, tmp_path):
        """#745: an unhandled non-proposer request is kept queued so the
        queue-empty clause does NOT fire — this isolates the dup-streak
        fallback clause the test name is actually about."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "request-planner.json").write_text(
            json.dumps({"request_status": "queued", "request_id": "cycle-planner-1"}),
            encoding="utf-8",
        )
        for i in range(3):
            _append_outcome(state_dir, f"c{i}", "skipped-duplicate")
        assert llm_proposer.should_propose(state_dir, None) is True

    def test_last_three_not_all_duplicate_returns_false(self, tmp_path):
        """#745: an unhandled non-proposer request is kept queued so the
        queue-empty clause does NOT fire — otherwise an empty queue would
        make this True regardless of the dup-streak state, defeating the
        point of this test (isolating the dup-streak fallback clause)."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "request-planner.json").write_text(
            json.dumps({"request_status": "queued", "request_id": "cycle-planner-1"}),
            encoding="utf-8",
        )
        _append_outcome(state_dir, "c1", "skipped-duplicate")
        _append_outcome(state_dir, "c2", "skipped-duplicate")
        _append_outcome(state_dir, "c3", "success")
        assert llm_proposer.should_propose(state_dir, None) is False

    def test_priorities_done_via_git_log_returns_true(self, tmp_path):
        """When a selfevo repo is supplied and its git log shows every listed
        priority already done, the #712 filter empties "Current priority
        targets:" -> should_propose fires even without a duplicate streak."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        repo = _make_git_repo_with_commit(
            tmp_path,
            "feat: write scripts/cycle_logger.py — confirmed done for cycle-999",
            "feat: write scripts/smoke_test_loop.py — confirmed done for cycle-1000",
            create_files=("scripts/cycle_logger.py", "scripts/smoke_test_loop.py"),
        )
        assert llm_proposer.should_propose(state_dir, repo) is True

    def test_stale_planner_request_plus_dup_streak_fires_then_self_blocks(
        self, tmp_path, monkeypatch
    ):
        """End-to-end #707 canary scenario: a stale PLANNER request sits
        queued (never consumed) while the last 3 terminal outcomes are all
        skipped-duplicate. should_propose must be True despite the queued
        request. After one successful maybe_propose call writes its own
        (proposer) request, a second should_propose call is False — the
        anti-stacking guard now sees ITS OWN queued request."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        for i in range(3):
            _append_outcome(state_dir, f"c{i}", "skipped-duplicate")
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "request-stale.json").write_text(
            json.dumps({"request_status": "queued", "request_id": "cycle-stale123"}),
            encoding="utf-8",
        )

        assert llm_proposer.should_propose(state_dir, None) is True

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            return {
                "task_title": "Fix a typo in docs/README-ish file",
                "rationale": "Corrects a small doc mistake.",
                "target_path": "docs/foo.md",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)
        assert result == "Implement and commit: Fix a typo in docs/README-ish file"
        assert llm_proposer.should_propose(state_dir, None) is False


# ─── #745: marker-based handledness (archiver-paced cadence bug) ──────────
# ─── and the queue-empty clause (fresh-priorities deadlock)             ───


def _write_handled_marker(state_dir: Path, request_id: str) -> None:
    """Writes the marker the SAME way the bridge does (bridge.py:1231-1232,
    1276): ``handled_<safe_rid>.txt`` under ``<state_dir>/subagent_bridge``,
    with ``safe_rid = request_id.replace('/', '_')[:120]``."""
    bridge_state_dir = state_dir / "subagent_bridge"
    bridge_state_dir.mkdir(parents=True, exist_ok=True)
    safe_rid = request_id.replace("/", "_")[:120]
    (bridge_state_dir / f"handled_{safe_rid}.txt").write_text("marker", encoding="utf-8")


class TestHandledMarker:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")

    def test_handled_marker_present_no_longer_blocks_anti_stacking(self, tmp_path):
        """#745 core fix: a proposer request the bridge already executed
        (handled marker written) no longer counts as 'queued' for the
        anti-stacking guard, even though its own request_status field still
        says 'queued' (that field is never rewritten) — this was the
        archiver-paced cadence bug."""
        state_dir = _state_dir(tmp_path)
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        request_id = "llm-proposer-cycle-abc123"
        (req_dir / "request-handled.json").write_text(
            json.dumps({"request_status": "queued", "request_id": request_id}),
            encoding="utf-8",
        )
        _write_handled_marker(state_dir, request_id)
        assert llm_proposer._has_queued_proposer_request(state_dir) is False

    def test_no_marker_still_blocks_anti_stacking(self, tmp_path):
        """Companion negative case: same request, no marker written -> still
        blocks (anti-stacking preserved)."""
        state_dir = _state_dir(tmp_path)
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        request_id = "llm-proposer-cycle-abc123"
        (req_dir / "request-unhandled.json").write_text(
            json.dumps({"request_status": "queued", "request_id": request_id}),
            encoding="utf-8",
        )
        assert llm_proposer._has_queued_proposer_request(state_dir) is True

    def test_queue_effectively_empty_true_when_only_request_is_handled(self, tmp_path):
        """A non-proposer (e.g. planner) request queued but handled-marker'd,
        and nothing else in the dir -> the queue counts as effectively
        empty."""
        state_dir = _state_dir(tmp_path)
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        request_id = "cycle-planner-handled-1"
        (req_dir / "request-planner-handled.json").write_text(
            json.dumps({"request_status": "queued", "request_id": request_id}),
            encoding="utf-8",
        )
        _write_handled_marker(state_dir, request_id)
        assert llm_proposer._queue_effectively_empty(state_dir) is True

    def test_queue_effectively_empty_false_when_unhandled_request_present(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "request-planner.json").write_text(
            json.dumps({"request_status": "queued", "request_id": "cycle-planner-2"}),
            encoding="utf-8",
        )
        assert llm_proposer._queue_effectively_empty(state_dir) is False

    def test_queue_effectively_empty_true_when_dir_missing(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert llm_proposer._queue_effectively_empty(state_dir) is True

    def test_should_propose_true_when_only_queued_request_is_handled(self, tmp_path):
        """End-to-end: a handled-marker'd proposer request sitting in the
        queue with a stale 'queued' request_status must NOT block a new
        proposal, and with the queue thus effectively empty, should_propose
        fires even though priorities remain and there's no dup streak."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        request_id = "llm-proposer-cycle-done1"
        (req_dir / "request-done.json").write_text(
            json.dumps({"request_status": "queued", "request_id": request_id}),
            encoding="utf-8",
        )
        _write_handled_marker(state_dir, request_id)
        _append_outcome(state_dir, "c1", "success")
        assert llm_proposer.should_propose(state_dir, None) is True


# ─── build_context ──────────────────────────────────────────────────────────


def test_digest_ledger_joins_proposed_title_and_target():
    rows = [
        {"phase": "proposed", "cycle_id": "c1", "task_title": "Improve proposer context", "target_path": "nanobot/runtime/llm_proposer.py"},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "branch": "selfevo/cycle-c1"},
    ]
    assert llm_proposer._digest_ledger(rows) == [
        "success: Improve proposer context [nanobot/runtime/llm_proposer.py]"
    ]


def test_digest_ledger_falls_back_to_branch_without_proposal():
    rows = [{"phase": "outcome", "cycle_id": "legacy", "outcome": "failed", "branch": "selfevo/cycle-legacy"}]
    assert llm_proposer._digest_ledger(rows) == ["failed: selfevo/cycle-legacy"]


def test_digest_ledger_bounds_joined_title_line():
    rows = [
        {"phase": "proposed", "cycle_id": "c1", "task_title": "x" * 300, "target_path": "scripts/x.py"},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "partial"},
    ]
    assert len(llm_proposer._digest_ledger(rows)[0]) <= 160


class TestBuildContext:
    def test_bounded_and_includes_goal_and_digest(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        goal_text = json.loads(GOAL_TEXT_JSON)["text"]
        _write_goal_text(state_dir, goal_text)
        _append_outcome(state_dir, "c1", "success")
        _append_outcome(state_dir, "c2", "skipped-duplicate")

        context = llm_proposer.build_context(state_dir, None)
        assert len(context) <= llm_proposer._MAX_CONTEXT_CHARS
        assert "Priority 5" in context
        assert "success" in context
        assert "skipped-duplicate" in context
        assert "Mutable surface rule" in context

    def test_hard_cap_enforced_with_huge_ledger(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "x" * 5000)
        for i in range(200):
            _append_outcome(state_dir, f"c{i}", "failed")
        context = llm_proposer.build_context(state_dir, None)
        assert len(context) <= llm_proposer._MAX_CONTEXT_CHARS

    def test_surface_rule_survives_truncation_with_oversized_context(self, tmp_path):
        """#825 review FIX 2: surface_rule (the mutable-surface path
        constraint) must never be truncated away. An oversized goal text
        PLUS a burst of #716 failed-title rows (each with a fairly long
        title, to push the joined context well past _MAX_CONTEXT_CHARS)
        must still leave the surface rule intact in the returned context —
        losing it would let the model target an out-of-surface path
        uncorrected, which gate-blocks and adds yet another failed title
        (a compounding loop)."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "x" * 6000)
        for i in range(llm_proposer._RECENT_FAILED_WINDOW_CYCLES):
            _append_proposed(
                state_dir, f"c{i}",
                f"Failing task with a fairly long descriptive title number {i}",
            )
            _append_outcome(state_dir, f"c{i}", "failed")

        context = llm_proposer.build_context(state_dir, None)
        assert "Mutable surface rule" in context
        assert "no other path is acceptable." in context
        assert context.endswith("no other path is acceptable.")
        assert len(context) <= llm_proposer._MAX_CONTEXT_CHARS

    def test_guardrails_survive_when_goal_forces_truncation(self, tmp_path):
        """#826: a goal large enough to force the goal+outcomes blob to be
        truncated must still leave the guardrail signals — the #716
        recently-failed section AND the recently-proposed section AND the
        surface_rule — intact in the returned context. Previously the goal
        was the first part of a single hard-cut blob, so it truncated the
        trailing guardrails away and the do-not-retry hints never reached
        the model."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "g" * (llm_proposer._MAX_CONTEXT_CHARS + 2000))
        for i in range(llm_proposer._RECENT_FAILED_WINDOW_CYCLES):
            _append_proposed(
                state_dir, f"c{i}",
                f"Failing task with a fairly long descriptive title number {i}",
            )
            _append_outcome(state_dir, f"c{i}", "failed")

        context = llm_proposer.build_context(state_dir, None)
        # goal was truncated (context capped) but guardrails survived
        assert len(context) <= llm_proposer._MAX_CONTEXT_CHARS
        assert "Recently attempted but NOT integrated" in context  # #716 failed section
        assert "Recently proposed" in context                      # dupes section
        assert "no other path is acceptable." in context           # surface_rule
        assert "Failing task with a fairly long descriptive title number" in context

    def test_missing_ledger_is_fail_open(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some goal text")
        context = llm_proposer.build_context(state_dir, None)
        assert "no ledger history yet" in context

    def test_recent_proposed_titles_appear_under_rejected_block(self, tmp_path):
        """#707 canary fix: proposer-written 'proposed' ledger rows (which
        DO carry task_title, unlike 'outcome' rows) surface under a clearly
        labeled do-not-repeat block, so the model sees its own recent
        (rejected-as-duplicate) proposals."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        _append_proposed(state_dir, "c1", "Implement lightweight memory usage tracker")
        _append_proposed(state_dir, "c2", "Implement lightweight resource usage monitor")

        context = llm_proposer.build_context(state_dir, None)
        assert "Recently proposed" in context
        assert "do NOT propose these themes again" in context
        assert "Implement lightweight memory usage tracker" in context
        assert "Implement lightweight resource usage monitor" in context
        assert len(context) <= llm_proposer._MAX_CONTEXT_CHARS

    def test_no_proposed_rows_shows_none_yet(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        context = llm_proposer.build_context(state_dir, None)
        assert "(none yet)" in context

    def test_recent_failed_title_appears_under_not_integrated_section(self, tmp_path):
        """#716: a title from a recent, non-integrated attempt (failed
        outcome) surfaces under its own section, distinct from the
        'Recently proposed' duplicates block, so the model sees it tried
        and failed at this theme even though no commit for it ever landed
        in git log."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        _append_proposed(state_dir, "c1", "Add a memory-leak detector script")
        _append_outcome(state_dir, "c1", "failed")

        context = llm_proposer.build_context(state_dir, None)
        assert "Recently attempted but NOT integrated" in context
        assert "do NOT re-propose the same approach" in context
        assert "Add a memory-leak detector script" in context
        assert len(context) <= llm_proposer._MAX_CONTEXT_CHARS

    def test_no_recent_failures_omits_new_section_entirely(self, tmp_path):
        """#716 acceptance: with no recent non-integrated attempts,
        build_context's output is byte-identical to pre-#716 — the new
        section is omitted entirely rather than shown empty (unlike the
        'Recently proposed' block, which always shows a '(none yet)'
        placeholder)."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        _append_proposed(state_dir, "c1", "Add a memory-leak detector script")
        _append_outcome(state_dir, "c1", "success")

        context = llm_proposer.build_context(state_dir, None)
        assert "Recently attempted but NOT integrated" not in context

    def test_absent_gracefully_when_no_selfevo_repo(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        context = llm_proposer.build_context(state_dir, None)
        assert "Existing scripts" not in context

    def test_includes_inventory_section_from_system_map_file(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        repo = tmp_path / "selfevo_repo"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "SYSTEM_MAP.md").write_text(
            "# SYSTEM MAP\n\n## Inventory\n\n"
            "- scripts/track_memory.py — Track memory usage over time.\n\n"
            "## Near-duplicate candidates\n\n(none detected)\n",
            encoding="utf-8",
        )

        context = llm_proposer.build_context(state_dir, repo)

        assert "Existing scripts (do not duplicate" in context
        assert "scripts/track_memory.py — Track memory usage over time." in context

    def test_foreign_format_map_falls_back_to_direct_generation(self, tmp_path):
        """#749 follow-up: a foreign-generated SYSTEM_MAP.md (e.g. the
        instance's own scripts/generate_system_map.py) has no ``## Inventory``
        section our parser recognizes — the inventory context must still be
        populated by generating directly from the repo, not silently dropped.
        """
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        repo = tmp_path / "selfevo_repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "deploy_release.py").write_text(
            '"""Deploys the latest release."""\n', encoding="utf-8"
        )
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "SYSTEM_MAP.md").write_text(
            "# System Map\n\n"
            "It is automatically generated by `scripts/generate_system_map.py`.\n\n"
            "## Scripts by Theme\n\n### Deployment\n\n- deploy_release.py — ships releases\n",
            encoding="utf-8",
        )

        context = llm_proposer.build_context(state_dir, repo)

        assert "Existing scripts (do not duplicate" in context
        assert "scripts/deploy_release.py — Deploys the latest release." in context

    def test_includes_inventory_section_generated_directly_without_map_file(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        repo = tmp_path / "selfevo_repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "deploy_release.py").write_text(
            '"""Deploys the latest release."""\n', encoding="utf-8"
        )

        context = llm_proposer.build_context(state_dir, repo)

        assert "Existing scripts (do not duplicate" in context
        assert "scripts/deploy_release.py — Deploys the latest release." in context

    def test_absent_gracefully_when_repo_has_no_scripts(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        repo = tmp_path / "selfevo_repo"
        repo.mkdir()
        context = llm_proposer.build_context(state_dir, repo)
        assert "Existing scripts" not in context

    def test_inventory_bounded_at_cap_when_over_max_entries(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        repo = tmp_path / "selfevo_repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        for i in range(llm_proposer._MAX_INVENTORY_ENTRIES + 10):
            (scripts_dir / f"script_{i:03d}.py").write_text(
                f'"""Script number {i}."""\n', encoding="utf-8"
            )

        context = llm_proposer.build_context(state_dir, repo)

        assert "Existing scripts (do not duplicate" in context
        assert f"{llm_proposer._MAX_INVENTORY_ENTRIES + 10} scripts total" in context
        assert "most recently modified" in context

    def test_inventory_section_capped_by_max_inventory_chars(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        repo = tmp_path / "selfevo_repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        for i in range(200):
            (scripts_dir / f"script_{i:03d}.py").write_text(
                '"""' + ("x" * 200) + '"""\n', encoding="utf-8"
            )

        context = llm_proposer.build_context(state_dir, repo)

        inventory_idx = context.index("## Existing scripts")
        inventory_section = context[inventory_idx:]
        assert len(inventory_section) <= llm_proposer._MAX_INVENTORY_CHARS + len(
            "## Existing scripts (do not duplicate — reuse or extend "
            "one of these instead of writing a new file)\n"
        )


# ─── #840: relevance-ranked existing-scripts inventory ─────────────────────


class TestInventoryRelevanceRanking:
    def test_relevant_old_script_survives_cap_with_query(self, tmp_path):
        """A script relevant to the current demand/query, but with the
        OLDEST mtime, would be dropped by the pre-#840 mtime-only cap. With
        state_dir + a matching query, existence_index.related_scripts ranks
        it first so it survives."""
        state_dir = _state_dir(tmp_path)
        repo = tmp_path / "selfevo_repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)

        old_path = scripts_dir / "special_widget_analyzer.py"
        old_path.write_text('"""Analyzes special widget metrics."""\n', encoding="utf-8")
        old_time = 1_000_000_000
        os.utime(old_path, (old_time, old_time))

        for i in range(llm_proposer._MAX_INVENTORY_ENTRIES + 10):
            p = scripts_dir / f"script_{i:03d}.py"
            p.write_text(f'"""Script number {i}."""\n', encoding="utf-8")
            newer_time = old_time + 1000 + i
            os.utime(p, (newer_time, newer_time))

        # Baseline (no query/state_dir): mtime-only ordering drops the old script.
        baseline = llm_proposer._system_map_inventory_section(repo)
        assert "special_widget_analyzer.py" not in baseline

        # With a relevance query matching the old script, it survives the cap.
        ranked = llm_proposer._system_map_inventory_section(
            repo, state_dir=state_dir, query="special widget metrics",
        )
        assert "special_widget_analyzer.py" in ranked

    def test_empty_query_is_byte_identical_to_no_arg_call(self, tmp_path):
        """Regression pin (#840): query="" (the default) must produce EXACTLY
        the same output as the pre-#840 no-keyword-arg call."""
        state_dir = _state_dir(tmp_path)
        repo = tmp_path / "selfevo_repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        for i in range(llm_proposer._MAX_INVENTORY_ENTRIES + 10):
            (scripts_dir / f"script_{i:03d}.py").write_text(
                f'"""Script number {i}."""\n', encoding="utf-8"
            )

        default_call = llm_proposer._system_map_inventory_section(repo)
        explicit_empty_query = llm_proposer._system_map_inventory_section(
            repo, state_dir=state_dir, query="",
        )

        assert explicit_empty_query == default_call
        assert default_call != ""


# ─── #862: usage-evidence utility annotations on the inventory ─────────────


class TestInventoryUtilityAnnotations:
    def _repo_with_one_script(self, tmp_path: Path) -> Path:
        repo = tmp_path / "selfevo_repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "helper.py").write_text('"""Helper script."""\n', encoding="utf-8")
        return repo

    def test_last_used_entry_shows_signal_and_days_ago(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = self._repo_with_one_script(tmp_path)
        last_used = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace(
            "+00:00", "Z"
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/helper.py": {"last_used": last_used, "last_touched": None, "signal": "pycache"}},
        )

        section = llm_proposer._system_map_inventory_section(repo, state_dir=state_dir)

        line = next(ln for ln in section.splitlines() if "scripts/helper.py" in ln)
        assert line.endswith("[unverified 2d ago]")
        assert "[used:pycache" not in section

    def test_last_touched_only_entry_shows_edited_never_used(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = self._repo_with_one_script(tmp_path)
        last_touched = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace(
            "+00:00", "Z"
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/helper.py": {"last_used": None, "last_touched": last_touched, "signal": None}},
        )

        section = llm_proposer._system_map_inventory_section(repo, state_dir=state_dir)

        line = next(ln for ln in section.splitlines() if "scripts/helper.py" in ln)
        assert line.endswith("[edited 5d ago, never used]")

    def test_entry_absent_from_sidecar_shows_no_usage_evidence(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = self._repo_with_one_script(tmp_path)
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        # Sidecar has data, but not for scripts/helper.py — the annotation
        # step must still be active (sidecar non-empty) and mark this one
        # entry as having no evidence of its own.
        _write_usage_sidecar(
            state_dir,
            {"scripts/other.py": {"last_used": now_iso, "last_touched": None, "signal": "pycache"}},
        )

        section = llm_proposer._system_map_inventory_section(repo, state_dir=state_dir)

        line = next(ln for ln in section.splitlines() if "scripts/helper.py" in ln)
        assert line.endswith("[no usage evidence]")

    def test_missing_sidecar_is_byte_identical_and_has_no_steering_line(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = self._repo_with_one_script(tmp_path)

        with_state_dir = llm_proposer._system_map_inventory_section(repo, state_dir=state_dir)
        without_state_dir = llm_proposer._system_map_inventory_section(repo)

        assert with_state_dir == without_state_dir
        assert llm_proposer._INVENTORY_STEERING_LINE not in with_state_dir
        assert "[used:" not in with_state_dir
        assert "[no usage evidence]" not in with_state_dir
        assert "[edited " not in with_state_dir

    def test_steering_line_present_only_when_sidecar_has_data(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = self._repo_with_one_script(tmp_path)

        no_sidecar = llm_proposer._system_map_inventory_section(repo, state_dir=state_dir)
        assert llm_proposer._INVENTORY_STEERING_LINE not in no_sidecar

        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_usage_sidecar(
            state_dir,
            {"scripts/helper.py": {"last_used": now_iso, "last_touched": None, "signal": "pycache"}},
        )
        with_sidecar = llm_proposer._system_map_inventory_section(repo, state_dir=state_dir)
        assert llm_proposer._INVENTORY_STEERING_LINE in with_sidecar
        assert "Prefer EXTENDING a verified-used tool" in with_sidecar

    def test_malformed_timestamp_treated_as_absent_no_crash(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = self._repo_with_one_script(tmp_path)
        _write_usage_sidecar(
            state_dir,
            {
                "scripts/helper.py": {
                    "last_used": "not-a-timestamp",
                    "last_touched": "also-not-a-timestamp",
                    "signal": "pycache",
                }
            },
        )

        section = llm_proposer._system_map_inventory_section(repo, state_dir=state_dir)

        line = next(ln for ln in section.splitlines() if "scripts/helper.py" in ln)
        assert line.endswith("[no usage evidence]")


# ─── #716: _recent_failed_titles (recent non-integrated attempts) ──────────


class TestRecentFailedTitles:
    def test_failed_outcome_joined_to_title_via_cycle_id(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _append_proposed(state_dir, "c1", "Add a memory-leak detector script")
        _append_outcome(state_dir, "c1", "failed")

        rows = llm_proposer._load_ledger_rows(state_dir)
        assert llm_proposer._recent_failed_titles(rows) == ["Add a memory-leak detector script"]

    @pytest.mark.parametrize("outcome", ["failed", "partial", "timeout"])
    def test_non_integrated_outcomes_are_included(self, tmp_path, outcome):
        state_dir = _state_dir(tmp_path)
        _append_proposed(state_dir, "c1", "Add a disk-usage sweeper script")
        _append_outcome(state_dir, "c1", outcome)

        rows = llm_proposer._load_ledger_rows(state_dir)
        assert llm_proposer._recent_failed_titles(rows) == ["Add a disk-usage sweeper script"]

    @pytest.mark.parametrize("outcome", ["success", "promotion_candidate", "skipped-duplicate"])
    def test_integrated_or_deduped_outcomes_are_excluded(self, tmp_path, outcome):
        state_dir = _state_dir(tmp_path)
        _append_proposed(state_dir, "c1", "Add a disk-usage sweeper script")
        _append_outcome(state_dir, "c1", outcome)

        rows = llm_proposer._load_ledger_rows(state_dir)
        assert llm_proposer._recent_failed_titles(rows) == []

    @pytest.mark.parametrize(
        "reason", ["mutation_surface_violation", "blocked_file_present", "gate_failed"]
    )
    def test_blocked_gate_rows_are_included(self, tmp_path, reason):
        state_dir = _state_dir(tmp_path)
        _append_proposed(state_dir, "c1", "Add a runtime slice profiler")
        _append_gate(state_dir, "c1", allowed=False, reason=reason)

        rows = llm_proposer._load_ledger_rows(state_dir)
        assert llm_proposer._recent_failed_titles(rows) == ["Add a runtime slice profiler"]

    def test_allowed_gate_row_is_excluded(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _append_proposed(state_dir, "c1", "Add a runtime slice profiler")
        _append_gate(state_dir, "c1", allowed=True, reason="smoke_passed")

        rows = llm_proposer._load_ledger_rows(state_dir)
        assert llm_proposer._recent_failed_titles(rows) == []

    def test_old_failure_outside_window_does_not_block(self, tmp_path):
        """#716 recency policy: a failure that has scrolled outside the
        recent window ages out — a retry of the same title is not
        permanently blocked."""
        state_dir = _state_dir(tmp_path)
        _append_proposed(state_dir, "c-old", "Add a memory-leak detector script")
        _append_outcome(state_dir, "c-old", "failed")
        # Push c-old's outcome row out of the recent window with filler
        # terminal rows (unrelated cycles, no matching 'proposed' title).
        for i in range(llm_proposer._RECENT_FAILED_WINDOW_CYCLES):
            _append_outcome(state_dir, f"filler-{i}", "success")

        rows = llm_proposer._load_ledger_rows(state_dir)
        assert llm_proposer._recent_failed_titles(rows) == []

    def test_dedup_and_cap(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        for i in range(llm_proposer._RECENT_FAILED_TITLES_N + 5):
            _append_proposed(state_dir, f"c{i}", "Add a memory-leak detector script")
            _append_outcome(state_dir, f"c{i}", "failed")

        rows = llm_proposer._load_ledger_rows(state_dir)
        titles = llm_proposer._recent_failed_titles(rows)
        assert titles == ["Add a memory-leak detector script"]

    def test_no_ledger_rows_is_fail_open_empty(self, tmp_path):
        assert llm_proposer._recent_failed_titles([]) == []

    def test_cap_keeps_newest_not_oldest(self, tmp_path):
        """#825 review FIX 1: failed_cycle_ids is built oldest-to-newest
        (ledger append order); with more than _RECENT_FAILED_TITLES_N
        distinct failures in the window, capping must keep the NEWEST
        ones (the recent churn #716 needs visible) — not silently retain
        the oldest N and drop the very failures that motivated this
        feature."""
        state_dir = _state_dir(tmp_path)
        total = llm_proposer._RECENT_FAILED_WINDOW_CYCLES
        for i in range(total):
            _append_proposed(state_dir, f"c{i}", f"Failing task {i}")
            _append_outcome(state_dir, f"c{i}", "failed")

        rows = llm_proposer._load_ledger_rows(state_dir)
        titles = llm_proposer._recent_failed_titles(rows)

        n = llm_proposer._RECENT_FAILED_TITLES_N
        assert len(titles) == n
        # Newest n are kept, returned oldest-first (matching
        # _recent_proposed_titles' "most-recent-last" convention).
        expected = [f"Failing task {i}" for i in range(total - n, total)]
        assert titles == expected
        assert "Failing task 0" not in titles


# ─── validate_sizing ─────────────────────────────────────────────────────────


class TestValidateSizing:
    def _good(self, **overrides):
        proposal = {
            "task_title": "Add a docstring example to scripts/loop_metrics_report.py",
            "rationale": "Improves discoverability of the report script's CLI usage.",
            "target_path": "scripts/loop_metrics_report.py",
            "serves": "priority 3",
        }
        proposal.update(overrides)
        return proposal

    def test_accepts_well_formed_proposal(self):
        ok, reason = llm_proposer.validate_sizing(self._good())
        assert ok is True
        assert reason == ""

    def test_rejects_none(self):
        ok, reason = llm_proposer.validate_sizing(None)
        assert ok is False
        assert reason

    def test_rejects_empty_title(self):
        ok, reason = llm_proposer.validate_sizing(self._good(task_title=""))
        assert ok is False
        assert "task_title" in reason

    def test_rejects_long_title(self):
        ok, reason = llm_proposer.validate_sizing(self._good(task_title="x" * 200))
        assert ok is False
        assert "120" in reason

    def test_rejects_missing_rationale(self):
        ok, reason = llm_proposer.validate_sizing(self._good(rationale=""))
        assert ok is False
        assert "rationale" in reason

    def test_rejects_out_of_surface_path(self):
        ok, reason = llm_proposer.validate_sizing(self._good(target_path="nanobot/runtime/bridge.py"))
        assert ok is False
        assert "outside allowed surfaces" in reason

    def test_rejects_immutable_goals_charter(self):
        ok, reason = llm_proposer.validate_sizing(self._good(target_path="goals.md"))
        assert ok is False
        assert "immutable" in reason

    def test_rejects_operator_owned_agents_and_accepts_skill_surface(self):
        ok, reason = llm_proposer.validate_sizing(self._good(target_path="AGENTS.md"))
        assert ok is False
        assert reason == "operator_owned_path"
        ok, reason = llm_proposer.validate_sizing(self._good(target_path="skills/review/SKILL.md"))
        assert ok is True, reason

    def test_nested_agents_is_not_root_exact_allowance(self):
        ok, reason = llm_proposer.validate_sizing(self._good(target_path="other/AGENTS.md"))
        assert ok is False
        assert "outside allowed surfaces" in reason

    # ── #823: runtime-slice tier awareness (#812) ──────────────────────────────
    _SLICE_ENV = "SELFEVO_RUNTIME_SLICE"
    _SLICE_MOD = "nanobot/runtime/existence_index.py"

    def test_runtime_slice_target_rejected_when_env_empty(self, monkeypatch):
        # feature off (default) → a runtime target is rejected, unchanged
        # behaviour (#823). #876: rung 0 (existence_index.py) reaches the
        # effective slice ONLY via the env allow-list, never via the ladder
        # on its own — so it is rejected here too, exactly like before #876.
        monkeypatch.delenv(self._SLICE_ENV, raising=False)
        ok, reason = llm_proposer.validate_sizing(self._good(target_path=self._SLICE_MOD))
        assert ok is False
        assert "outside allowed surfaces" in reason

    def test_runtime_slice_earned_ladder_rung_accepted_when_rung0_promotion_active(self, tmp_path, monkeypatch):
        # #876: with rung 0 (existence_index.py) genuinely ACTIVE in
        # PROMOTED_TREE's manifest, rung 1 (demand.py) is earned — accepted
        # as a target even though the operator never listed it explicitly.
        import hashlib
        import json as _json

        flat = "nanobot__runtime__existence_index.py"
        data = b"X = 1\n"
        (tmp_path / flat).write_bytes(data)
        (tmp_path / "manifest.json").write_text(
            _json.dumps({
                "nanobot/runtime/existence_index.py": {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "status": "active",
                },
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr("nanobot.runtime.promoted_overlay._boundary_ok", lambda *_: True)
        monkeypatch.setenv("PROMOTED_TREE", str(tmp_path))
        monkeypatch.setenv(self._SLICE_ENV, self._SLICE_MOD)
        ok, reason = llm_proposer.validate_sizing(
            self._good(target_path="nanobot/runtime/demand.py", serves="optimization demand")
        )
        assert ok is True
        assert reason == ""

    def test_runtime_slice_target_accepted_when_enabled(self, monkeypatch):
        monkeypatch.setenv(self._SLICE_ENV, self._SLICE_MOD)
        ok, reason = llm_proposer.validate_sizing(
            self._good(target_path=self._SLICE_MOD, serves="optimization existence_index")
        )
        assert ok is True
        assert reason == ""

    def test_runtime_slice_deny_path_rejected_even_if_listed(self, monkeypatch):
        monkeypatch.setenv(self._SLICE_ENV, f"nanobot/runtime/bridge.py,{self._SLICE_MOD}")
        ok, reason = llm_proposer.validate_sizing(
            self._good(target_path="nanobot/runtime/bridge.py", serves="optimization x")
        )
        assert ok is False

    def test_runtime_module_not_in_slice_rejected(self, monkeypatch):
        # only existence_index opted in → a different runtime module stays rejected
        monkeypatch.setenv(self._SLICE_ENV, self._SLICE_MOD)
        ok, reason = llm_proposer.validate_sizing(
            self._good(target_path="nanobot/runtime/scorecard.py", serves="optimization x")
        )
        assert ok is False
        assert "outside allowed surfaces" in reason

    def test_script_surface_unaffected_by_slice_env(self, monkeypatch):
        monkeypatch.setenv(self._SLICE_ENV, self._SLICE_MOD)
        ok, reason = llm_proposer.validate_sizing(self._good())  # scripts/ target
        assert ok is True

    def test_rejects_multiple_paths_as_list(self):
        ok, reason = llm_proposer.validate_sizing(
            self._good(target_path=["scripts/a.py", "scripts/b.py"])
        )
        assert ok is False
        assert "one path" in reason

    def test_rejects_multiple_paths_as_string(self):
        ok, reason = llm_proposer.validate_sizing(
            self._good(target_path="scripts/a.py, scripts/b.py")
        )
        assert ok is False
        assert "one path" in reason


# ─── #751: 'serves' goal-alignment field validation ────────────────────────


class TestValidateServes:
    def _good(self, **overrides):
        proposal = {
            "task_title": "Add a docstring example to scripts/loop_metrics_report.py",
            "rationale": "Improves discoverability of the report script's CLI usage.",
            "target_path": "scripts/loop_metrics_report.py",
            "serves": "priority 3",
        }
        proposal.update(overrides)
        return proposal

    def test_accepts_priority_form(self):
        ok, reason = llm_proposer.validate_sizing(self._good(serves="priority 11"))
        assert ok is True, reason

    def test_accepts_vector_1_with_justification(self):
        ok, reason = llm_proposer.validate_sizing(
            self._good(serves="vector 1: reduces cycle disk writes")
        )
        assert ok is True, reason

    def test_accepts_vector_2_alone(self):
        ok, reason = llm_proposer.validate_sizing(self._good(serves="vector 2"))
        assert ok is True, reason

    def test_accepts_hypothesis_form(self):
        ok, reason = llm_proposer.validate_sizing(self._good(serves="hypothesis h3"))
        assert ok is True, reason

    def test_accepts_case_insensitively(self):
        ok, reason = llm_proposer.validate_sizing(self._good(serves="PRIORITY 2"))
        assert ok is True, reason

    def test_rejects_missing_serves(self):
        ok, reason = llm_proposer.validate_sizing(self._good(serves=""))
        assert ok is False
        assert "serves" in reason

    def test_rejects_serves_key_absent_entirely(self):
        proposal = self._good()
        del proposal["serves"]
        ok, reason = llm_proposer.validate_sizing(proposal)
        assert ok is False
        assert "serves" in reason

    def test_rejects_wrong_prefix(self):
        ok, reason = llm_proposer.validate_sizing(
            self._good(serves="because it seemed useful")
        )
        assert ok is False
        assert "serves" in reason

    def test_rejects_serves_over_160_chars(self):
        ok, reason = llm_proposer.validate_sizing(self._good(serves="priority 1 " + "x" * 160))
        assert ok is False
        assert "160" in reason

    def test_rejects_serves_as_list(self):
        ok, reason = llm_proposer.validate_sizing(self._good(serves=["priority 1"]))
        assert ok is False
        assert "serves" in reason


class TestProposeRetryOnce(object):
    def test_retries_exactly_once_on_rejection(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        calls = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            calls.append(rejection_reason)
            if rejection_reason is None:
                return {"task_title": "", "rationale": "", "target_path": "", "serves": ""}  # invalid
            return {
                "task_title": "Fix a typo in docs/README-ish file",
                "rationale": "Corrects a small doc mistake.",
                "target_path": "docs/foo.md",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)
        assert result == "Implement and commit: Fix a typo in docs/README-ish file"
        assert len(calls) == 2
        assert calls[0] is None
        assert calls[1] is not None

    def test_gives_up_after_one_retry(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        calls = []

        def _always_bad(context, *, rejection_reason=None, timeout=120.0):
            calls.append(rejection_reason)
            return {"task_title": "", "rationale": "", "target_path": "", "serves": ""}

        monkeypatch.setattr(llm_proposer, "propose", _always_bad)
        result = llm_proposer.maybe_propose(state_dir, None)
        assert result is None
        assert len(calls) == 2
        assert not (state_dir / "subagents" / "requests").exists()


# ─── #707 canary: pre-write self-dedup + retry-with-feedback ──────────────


class TestSelfDedup:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")

    def test_duplicate_via_git_log_retries_then_writes_novel_title(self, tmp_path, monkeypatch):
        """A proposal whose title matches an already-committed piece of work
        (the #575 heuristic, via the temp git repo) is rejected pre-write;
        the retry prompt carries explicit duplicate feedback, and a novel
        retry title is written."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        repo = _make_git_repo_with_commit(
            tmp_path,
            "feat: implement lightweight memory and cpu profiling script for eeepc host",
        )

        calls = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            calls.append(rejection_reason)
            if rejection_reason is None:
                return {
                    "task_title": "Implement lightweight memory and CPU profiling script",
                    "rationale": "Tracks resource usage.",
                    "target_path": "scripts/profile.py",
                    "serves": "priority 2",
                }
            return {
                "task_title": "Add a smoke test for the loop metrics report",
                "rationale": "Closes an unrelated coverage gap.",
                "target_path": "tests/test_loop_metrics_extra.py",
                "serves": "priority 2",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, repo)

        assert result is not None
        assert "loop metrics report" in result
        assert len(calls) == 2
        assert calls[0] is None
        assert "duplicates already-done" in calls[1]
        assert "DIFFERENT area" in calls[1]

        req_dir = state_dir / "subagents" / "requests"
        written = [json.loads(p.read_text(encoding="utf-8")) for p in req_dir.glob("*.json")]
        assert len(written) == 1
        assert "loop metrics report" in written[0]["task_title"]
        assert result == written[0]["task_title"]

    def test_duplicate_via_recent_proposed_titles_rejected(self, tmp_path, monkeypatch):
        """A title never committed to git (no selfevo repo at all here) but
        matching a recent 'proposed' ledger row is still caught — this is
        the case the canary hit: 6 clones in a row, each correctly bridge-
        skipped as a duplicate, so no commit for any of them ever landed."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        _append_proposed(state_dir, "c-old", "Implement lightweight memory usage monitor for eeepc")

        calls = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            calls.append(rejection_reason)
            if rejection_reason is None:
                return {
                    "task_title": "Implement lightweight memory usage tracker",
                    "rationale": "Tracks memory usage.",
                    "target_path": "scripts/mem.py",
                    "serves": "priority 4",
                }
            return {
                "task_title": "Document the release checklist",
                "rationale": "Fills a documentation gap.",
                "target_path": "docs/release.md",
                "serves": "priority 4",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)

        assert result is not None
        assert "release checklist" in result
        assert len(calls) == 2
        assert calls[1] is not None

    def test_both_duplicate_gives_up_without_writing(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        repo = _make_git_repo_with_commit(
            tmp_path,
            "feat: implement lightweight memory and cpu profiling script for eeepc host",
        )

        calls = []

        def _always_clone(context, *, rejection_reason=None, timeout=120.0):
            calls.append(rejection_reason)
            return {
                "task_title": "Implement lightweight memory and CPU profiling script",
                "rationale": "Tracks resource usage.",
                "target_path": "scripts/profile.py",
                "serves": "priority 2",
            }

        monkeypatch.setattr(llm_proposer, "propose", _always_clone)
        result = llm_proposer.maybe_propose(state_dir, repo)

        assert result is None
        assert len(calls) <= 3
        assert len(calls) == 2  # sizing passes both times; only the dedup retry is spent
        assert not (state_dir / "subagents" / "requests").exists()

    def test_duplicate_via_recent_failed_title_rejected(self, tmp_path, monkeypatch):
        """#716: a title never committed to git and never itself
        re-proposed, but matching a recent attempt that FAILED (never
        integrated), is still caught pre-write — this is the #716 loop-
        health defect: the proposer kept re-proposing themes it had already
        tried and failed at, because neither git log nor its own recent-
        proposed-titles list carries a failed attempt's title."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        _append_proposed(state_dir, "c-old", "Implement lightweight memory usage monitor for eeepc")
        _append_outcome(state_dir, "c-old", "failed")

        calls = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            calls.append(rejection_reason)
            if rejection_reason is None:
                return {
                    "task_title": "Implement lightweight memory usage tracker",
                    "rationale": "Tracks memory usage.",
                    "target_path": "scripts/mem.py",
                    "serves": "priority 4",
                }
            return {
                "task_title": "Document the release checklist",
                "rationale": "Fills a documentation gap.",
                "target_path": "docs/release.md",
                "serves": "priority 4",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)

        assert result is not None
        assert "release checklist" in result
        assert len(calls) == 2
        assert calls[1] is not None
        assert "recently-failed" in calls[1]

    def test_genuinely_new_work_is_not_flagged_as_duplicate(self, tmp_path, monkeypatch):
        """Sanity check for #716: a proposal unrelated to any recent failure
        (or duplicate) passes through untouched on the first try."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        _append_proposed(state_dir, "c-old", "Add a memory-leak detector script")
        _append_outcome(state_dir, "c-old", "failed")

        calls = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            calls.append(rejection_reason)
            return {
                "task_title": "Document the deployment runbook",
                "rationale": "Fills a documentation gap.",
                "target_path": "docs/deploy.md",
                "serves": "priority 4",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)

        assert result is not None
        assert "deployment runbook" in result
        assert len(calls) == 1
        assert calls[0] is None

    def test_old_failure_outside_window_does_not_block_retry(self, tmp_path, monkeypatch):
        """#716 policy: the recency window means an old (aged-out) failure
        does not permanently ban a retry of the same theme. Filler cycles
        are BOTH 'proposed' and 'outcome' rows so the old title also ages
        out of the (unrelated, pre-existing) recent-proposed-titles window
        — otherwise that separate mechanism would still catch it and the
        test would not isolate the new #716 behavior."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        _append_proposed(state_dir, "c-old", "Implement lightweight memory usage monitor for eeepc")
        _append_outcome(state_dir, "c-old", "failed")
        for i in range(llm_proposer._RECENT_FAILED_WINDOW_CYCLES):
            _append_proposed(state_dir, f"filler-{i}", f"Unrelated filler task {i}")
            _append_outcome(state_dir, f"filler-{i}", "success")

        calls = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            calls.append(rejection_reason)
            return {
                "task_title": "Implement lightweight memory usage tracker",
                "rationale": "Tracks memory usage.",
                "target_path": "scripts/mem.py",
                "serves": "priority 4",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)

        assert result is not None
        assert "memory usage tracker" in result
        assert len(calls) == 1
        assert calls[0] is None


# ─── prompt: prefer numbered "Current priority targets" ────────────────────


class TestPromptPrioritiesPreference:
    def test_system_prompt_prefers_numbered_priorities(self, monkeypatch):
        captured: dict = {}

        class _CapturingCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _FakeResponse(
                    json.dumps(
                        {
                            "task_title": "x",
                            "rationale": "y",
                            "target_path": "docs/z.md",
                        }
                    )
                )

        class _CapturingChat:
            def __init__(self):
                self.completions = _CapturingCompletions()

        class _CapturingClient:
            def __init__(self, *args, **kwargs):
                self.chat = _CapturingChat()

        import openai

        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: _CapturingClient())
        monkeypatch.setenv("LITELLM_BASE_URL", "http://fake-gateway.local")
        monkeypatch.setenv("LITELLM_API_KEY", "sk-fake")

        llm_proposer.propose("some context")

        messages = captured["messages"]
        system_msg = next(m["content"] for m in messages if m["role"] == "system")
        assert "Current priority targets" in system_msg
        assert "VERBATIM" in system_msg


# ─── propose() — mocked OpenAI client, no network ──────────────────────────


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)
        self.finish_reason = "stop"


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.model = "an/test-proposer"
        self.usage = type("Usage", (), {
            "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
        })()


class _FakeCompletions:
    def __init__(self, content):
        self._content = content

    def create(self, **kwargs):
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content):
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, *args, content="{}", **kwargs):
        self.chat = _FakeChat(content)


class TestProposeMockedClient:
    def _patch_client(self, monkeypatch, content):
        def _factory(*args, **kwargs):
            return _FakeClient(content=content)

        import openai

        monkeypatch.setattr(openai, "OpenAI", _factory)
        monkeypatch.setenv("LITELLM_BASE_URL", "http://fake-gateway.local")
        monkeypatch.setenv("LITELLM_API_KEY", "sk-fake")

    def test_happy_path_json(self, monkeypatch):
        self._patch_client(
            monkeypatch,
            json.dumps({
                "task_title": "Add a test",
                "rationale": "closes a gap",
                "target_path": "tests/test_x.py",
            }),
        )
        result = llm_proposer.propose("some context")
        assert result == {
            "task_title": "Add a test",
            "rationale": "closes a gap",
            "target_path": "tests/test_x.py",
        }

    def test_proposer_records_call_and_prompt_telemetry(self, monkeypatch, tmp_path):
        self._patch_client(monkeypatch, json.dumps({"task_title": "x"}))
        monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
        assert llm_proposer.propose("some context") == {"task_title": "x"}
        main_rows = [json.loads(line) for line in (tmp_path / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl").read_text().splitlines()]
        assert main_rows[-1]["component"] == "proposer"
        assert main_rows[-1]["model"] == "an/test-proposer"
        assert main_rows[-1]["prompt_tokens"] == 11
        prompt_path = tmp_path / "prompts" / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
        prompt = json.loads(prompt_path.read_text().splitlines()[-1])
        assert prompt["component"] == "proposer"
        assert prompt["messages"][1]["content"] == "some context"

    def test_proposer_logging_failure_does_not_break_result(self, monkeypatch):
        self._patch_client(monkeypatch, json.dumps({"task_title": "x"}))
        monkeypatch.setattr(llm_proposer, "record_llm_call", lambda **_: (_ for _ in ()).throw(RuntimeError("telemetry")))
        assert llm_proposer.propose("some context") == {"task_title": "x"}

    def test_fenced_json(self, monkeypatch):
        payload = {
            "task_title": "Add a test",
            "rationale": "closes a gap",
            "target_path": "tests/test_x.py",
        }
        self._patch_client(monkeypatch, f"Sure!\n```json\n{json.dumps(payload)}\n```\n")
        result = llm_proposer.propose("some context")
        assert result == payload

    def test_garbage_reply_returns_none(self, monkeypatch):
        self._patch_client(monkeypatch, "not json at all, sorry")
        assert llm_proposer.propose("some context") is None

    def test_missing_gateway_env_returns_none(self, monkeypatch):
        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)
        assert llm_proposer.propose("some context") is None

    def test_network_failure_sets_unavailable_status(self, monkeypatch):
        self._patch_client(monkeypatch, "unused")
        class _FailingCompletions:
            def create(self, **kwargs):
                raise TimeoutError("gateway timeout")
        class _FailingClient:
            def __init__(self, *args, **kwargs):
                self.chat = type("Chat", (), {"completions": _FailingCompletions()})()
        import openai
        monkeypatch.setattr(openai, "OpenAI", _FailingClient)
        assert llm_proposer.propose("some context") is None
        assert llm_proposer._last_propose_failure.startswith("TimeoutError")


# ─── write_request — C1 schema equality ────────────────────────────────────


class TestWriteRequestSchemaEquality:
    # Canonical ``subagent-request-v1`` key set the subagent bridge consumes.
    # #747 deleted the deterministic planner's request-minting lane, so the
    # proposer's ``write_request`` is the sole writer of these requests; this
    # frozen key set is the schema contract the bridge relies on.
    _CANONICAL_REQUEST_KEYS = frozenset({
        "schema_version",
        "cycle_id",
        "goal_id",
        "task_id",
        "semantic_task_id",
        "request_id",
        "verification_task_id",
        "verification_role",
        "task_title",
        "task",
        "recommended_next_action",
        "request_status",
        "profile",
        "budget",
        "source_artifact",
        "feedback_decision",
        "lessons_context",
    })

    def test_same_keys_and_queued_status(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Document the ledger digest helper",
            "rationale": "Improves maintainer onboarding.",
            "target_path": "docs/proposer-notes.md",
            "serves": "priority 1",
        }
        path = llm_proposer.write_request(state_dir, proposal)
        written = json.loads(Path(path).read_text(encoding="utf-8"))

        # #751: 'serves' is recorded in the ledger row only (see
        # test_ledger_proposed_row_appended), deliberately NOT added to the
        # written request payload, so this C1 schema invariant is
        # unaffected by the new field.
        assert set(written.keys()) == self._CANONICAL_REQUEST_KEYS
        assert written["request_status"] == "queued"

    def test_bridge_find_pending_request_accepts_it(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")

        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
        }
        written_path = llm_proposer.write_request(state_dir, proposal)

        found_path, found_req = bridge.find_pending_request()
        assert found_path is not None
        assert str(found_path) == written_path
        assert found_req.get("request_status") == "queued"

    def test_ledger_proposed_row_appended(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 7",
        }
        llm_proposer.write_request(state_dir, proposal)

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        proposed_rows = [r for r in rows if r.get("phase") == "proposed"]
        assert len(proposed_rows) == 1
        assert proposed_rows[0]["task_title"] == "Add a helper doc"
        assert proposed_rows[0]["target_path"] == "docs/helper.md"
        assert proposed_rows[0]["serves"] == "priority 7"
        assert proposed_rows[0]["source_artifact"] == "llm_proposer"

    def test_ledger_row_serves_defaults_to_empty_string_when_absent(self, tmp_path):
        """Old-shaped callers (or a proposal dict missing 'serves' entirely)
        must not crash write_request; the row simply carries an empty
        string, which loop_metrics_report classifies as 'missing'."""
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
        }
        llm_proposer.write_request(state_dir, proposal)

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        proposed_rows = [r for r in rows if r.get("phase") == "proposed"]
        assert proposed_rows[0]["serves"] == ""

    def test_escalated_proposal_records_marker_and_telemetry(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setenv("SELFEVO_ESCALATION_MODEL", "an/frontier-model")
        demand_id = "priority-escalate123"
        for cycle_id in ("c-old-1", "c-old-2"):
            cycle_ledger.append_event(state_dir, {
                "phase": "proposed", "cycle_id": cycle_id, "demand_id": demand_id,
            })
            cycle_ledger.append_event(state_dir, {
                "phase": "outcome", "cycle_id": cycle_id, "outcome": "completed_no_commit",
            })
        proposal = {
            "task_title": "Escalate this demand",
            "rationale": "repeated no-op",
            "target_path": "scripts/escalate.py",
            "serves": f"demand {demand_id}",
        }
        llm_proposer.write_request(state_dir, proposal)
        rows = [json.loads(line) for line in (state_dir / "ledger" / "cycles.jsonl").read_text().splitlines()]
        row = [r for r in rows if r.get("phase") == "proposed" and r.get("request_id", "").startswith("llm-proposer-")][-1]
        assert row["escalated_model"] == "an/frontier-model"
        marker = demand._escalation_marker(state_dir, demand_id)
        assert marker and marker["cycle_id"] == row["cycle_id"]


# ─── #1118: optional, frozen expected_outcome claim ────────────────────────


class TestExpectedOutcomeClaim:
    """Optional, write-once claim on the proposer artifact (issue #1118,
    Part B) — modeled on #878's hypothesis-lane claim shape. Never required;
    an artifact without it remains valid (backward compatible)."""

    def _artifact_payload(self, state_dir, path):
        written = json.loads(Path(path).read_text(encoding="utf-8"))
        artifact_path = written["source_artifact"]
        return json.loads(Path(artifact_path).read_text(encoding="utf-8"))

    def test_absent_when_proposal_has_no_expected_outcome(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
        }
        path = llm_proposer.write_request(state_dir, proposal)
        artifact = self._artifact_payload(state_dir, path)
        assert "expected_outcome" not in artifact

    def test_claim_and_check_recorded_verbatim_when_valid(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
            "expected_outcome": {
                "claim": "running the new script exits 0",
                "check": {"kind": "script_exit_zero", "path": "scripts/helper.py"},
            },
        }
        path = llm_proposer.write_request(state_dir, proposal)
        artifact = self._artifact_payload(state_dir, path)
        assert artifact["expected_outcome"] == {
            "claim": "running the new script exits 0",
            "check": {"kind": "script_exit_zero", "path": "scripts/helper.py"},
        }

    def test_free_text_check_kind_is_accepted(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
            "expected_outcome": {"claim": "onboarding docs read more clearly", "check": {"kind": "free_text"}},
        }
        path = llm_proposer.write_request(state_dir, proposal)
        artifact = self._artifact_payload(state_dir, path)
        assert artifact["expected_outcome"]["claim"] == "onboarding docs read more clearly"
        assert artifact["expected_outcome"]["check"]["kind"] == "free_text"

    def test_claim_without_check_is_accepted(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
            "expected_outcome": {"claim": "the doc exists and is non-empty"},
        }
        path = llm_proposer.write_request(state_dir, proposal)
        artifact = self._artifact_payload(state_dir, path)
        assert artifact["expected_outcome"] == {"claim": "the doc exists and is non-empty"}

    def test_empty_claim_is_dropped_entirely(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
            "expected_outcome": {"claim": "   "},
        }
        path = llm_proposer.write_request(state_dir, proposal)
        artifact = self._artifact_payload(state_dir, path)
        assert "expected_outcome" not in artifact

    def test_malformed_expected_outcome_is_dropped_not_fatal(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
            "expected_outcome": "not a dict",
        }
        path = llm_proposer.write_request(state_dir, proposal)
        artifact = self._artifact_payload(state_dir, path)
        assert "expected_outcome" not in artifact

    def test_invalid_check_kind_is_dropped_but_claim_kept(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
            "expected_outcome": {"claim": "something falsifiable", "check": {"kind": "not_a_real_kind"}},
        }
        path = llm_proposer.write_request(state_dir, proposal)
        artifact = self._artifact_payload(state_dir, path)
        assert artifact["expected_outcome"] == {"claim": "something falsifiable"}

    def test_claim_is_capped_in_length(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
            "expected_outcome": {"claim": "x" * 5000},
        }
        path = llm_proposer.write_request(state_dir, proposal)
        artifact = self._artifact_payload(state_dir, path)
        assert len(artifact["expected_outcome"]["claim"]) <= 300

    def test_ledger_proposed_row_carries_claim_text(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
            "expected_outcome": {"claim": "the new doc exists on disk"},
        }
        llm_proposer.write_request(state_dir, proposal)

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        proposed_rows = [r for r in rows if r.get("phase") == "proposed"]
        assert proposed_rows[0]["expected_outcome_claim"] == "the new doc exists on disk"

    def test_ledger_proposed_row_omits_claim_key_when_absent(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
        }
        llm_proposer.write_request(state_dir, proposal)

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        proposed_rows = [r for r in rows if r.get("phase") == "proposed"]
        assert "expected_outcome_claim" not in proposed_rows[0]

    def test_canonical_request_keys_unaffected(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
            "serves": "priority 1",
            "expected_outcome": {"claim": "the new doc exists on disk"},
        }
        path = llm_proposer.write_request(state_dir, proposal)
        written = json.loads(Path(path).read_text(encoding="utf-8"))
        assert "expected_outcome" not in written

    def test_proposer_prompts_mention_expected_outcome_as_optional(self):
        for prompt in (llm_proposer._PROPOSER_SYSTEM_PROMPT, llm_proposer._DEMAND_PROPOSER_SYSTEM_PROMPT):
            assert "expected_outcome" in prompt
            assert "Optionally" in prompt or "optional" in prompt.lower()


# ─── write_request — #912 lessons_context wiring ───────────────────────────


class TestWriteRequestLessonsContext:
    """#912: write_request fills the request's lessons_context field (via
    build_lessons_context) instead of the pre-#912 hardcoded {}, and
    annotates the ledger 'proposed' row with which cards were injected."""

    def test_no_selfevo_repo_writes_empty_lessons_context(self, tmp_path):
        """Legacy call shape (no selfevo_repo arg, e.g. existing callers/
        tests) must keep writing '{}' — identical to pre-#912 behavior."""
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
        }
        path = llm_proposer.write_request(state_dir, proposal)
        written = json.loads(Path(path).read_text(encoding="utf-8"))

        assert written["lessons_context"] == {}

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        proposed_rows = [r for r in rows if r.get("phase") == "proposed"]
        assert "lessons_context" not in proposed_rows[0]

    def test_matching_selfevo_repo_populates_request_and_ledger(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        state_dir = _state_dir(tmp_path)
        repo = tmp_path / "instance_repo"
        errors_path = repo / "lessons" / "errors.yaml"
        errors_path.parent.mkdir(parents=True)
        errors_path.write_text(
            "- id: ERR-AUTO-timeout-guard\n"
            "  category: timeout\n"
            "  title: Subagent timeout guard misconfigured\n"
            "  root_cause: Timeout value read from stale config default.\n"
            "  prevention: Always read timeout from live config.\n",
            encoding="utf-8",
        )
        proposal = {
            "task_title": "Fix subagent timeout guard misconfiguration",
            "rationale": "closes a recorded pitfall",
            "target_path": "nanobot/runtime/bridge.py",
        }

        path = llm_proposer.write_request(state_dir, proposal, repo)
        written = json.loads(Path(path).read_text(encoding="utf-8"))

        assert written["lessons_context"]["relevant_error"]["id"] == "ERR-AUTO-timeout-guard"

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        proposed_rows = [r for r in rows if r.get("phase") == "proposed"]
        assert proposed_rows[0]["lessons_context"] == ["error:ERR-AUTO-timeout-guard"]

    def test_no_match_leaves_ledger_row_without_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        state_dir = _state_dir(tmp_path)
        repo = tmp_path / "instance_repo"  # lessons/ dir doesn't exist at all
        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
        }

        path = llm_proposer.write_request(state_dir, proposal, repo)
        written = json.loads(Path(path).read_text(encoding="utf-8"))
        assert written["lessons_context"] == {}

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        proposed_rows = [r for r in rows if r.get("phase") == "proposed"]
        assert "lessons_context" not in proposed_rows[0]


# ─── integration: maybe_propose -> bridge.find_pending_request handoff ─────


# ─── #749: maybe_propose keeps SYSTEM_MAP.md fresh every call ─────────────


class TestSystemMapWiring:
    def test_maybe_propose_updates_system_map_even_when_killswitch_off(self, tmp_path, monkeypatch):
        """The bridge calls maybe_propose() unconditionally every cycle
        regardless of SELFEVO_LLM_PROPOSER_ENABLED; the system-map refresh
        must run on that same unconditional path, not only when the
        proposer itself is enabled."""
        monkeypatch.delenv(ENV_VAR, raising=False)
        state_dir = _state_dir(tmp_path)
        repo = tmp_path / "selfevo_repo"
        repo.mkdir()
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

        assert not llm_proposer._enabled()
        result = llm_proposer.maybe_propose(state_dir, repo)

        assert result is None  # kill switch off, no proposal
        assert (repo / "docs" / "SYSTEM_MAP.md").is_file()

    def test_maybe_propose_survives_system_map_failure(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        state_dir = _state_dir(tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        from nanobot.runtime import system_map

        monkeypatch.setattr(system_map, "update_system_map", _boom)
        # Should not raise even though selfevo_repo is a nonsense path.
        assert llm_proposer.maybe_propose(state_dir, tmp_path / "nope") is None

    def test_maybe_propose_skips_system_map_when_no_repo_given(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        state_dir = _state_dir(tmp_path)
        # No selfevo_repo at all — must not raise, must not attempt update.
        assert llm_proposer.maybe_propose(state_dir, None) is None


class TestIntegrationHandoff:
    def test_maybe_propose_writes_request_bridge_then_finds(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")

        # Novelty-exhaustion condition: last 3 terminal rows all skipped-duplicate.
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        for i in range(3):
            _append_outcome(state_dir, f"c{i}", "skipped-duplicate")

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            return {
                "task_title": "Add a smoke test for the loop metrics report",
                "rationale": "Closes a coverage gap surfaced by the ledger digest.",
                "target_path": "tests/test_loop_metrics_extra.py",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)

        result = llm_proposer.maybe_propose(state_dir, None)
        assert result is not None
        assert result.endswith("loop metrics report")

        found_path, found_req = bridge.find_pending_request()
        assert found_path is not None
        assert found_req.get("task_title", "").endswith("loop metrics report")
        assert found_req.get("request_status") == "queued"
        assert found_req.get("task_title") == result


# ─── #741: bridge must log maybe_propose's own return value, not a stale ───
# ─── post-write find_pending_request lookup                              ───


class TestJournalLineUsesOwnReturnValue:
    def test_maybe_propose_return_value_contract(self, tmp_path, monkeypatch):
        """The new contract: ``maybe_propose`` returns the exact
        ``task_title`` string it just persisted (never a stale/unrelated
        one) when it writes a request, and ``None`` — never ``False`` — when
        it does not. The return value stays truthy iff a request was
        written, so existing ``if maybe_propose(...):`` call sites keep
        working unchanged."""
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            return {
                "task_title": "Add a docstring to the ledger digest helper",
                "rationale": "Improves maintainer onboarding.",
                "target_path": "docs/proposer-notes.md",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)
        assert isinstance(result, str)
        assert result == "Implement and commit: Add a docstring to the ledger digest helper"
        assert bool(result) is True

        # A second call is blocked by the anti-stacking guard (a proposer
        # request is now queued) -> must return exactly None, not False.
        result2 = llm_proposer.maybe_propose(state_dir, None)
        assert result2 is None

    def test_bridge_after_skip_logs_own_title_not_stale_queue_tail(self, tmp_path, monkeypatch, capsys):
        """#741 regression: seed an OLDER stale request that stays queued
        (unhandled) after the bulk-skip loop's cap ends the run — exactly
        the ``test_cap_enforced_remainder_stays_queued`` scenario — then
        drive ``_maybe_propose_after_skip`` directly with a mocked LLM reply.
        Before the fix, the bridge re-derived the logged title via a
        post-write ``find_pending_request()`` call, which is oldest-first
        and so returned the STALE request's title (mtime-sorted ahead of the
        proposer's brand-new file). The fix logs ``maybe_propose``'s own
        return value instead.
        """
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")

        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        # An older, still-queued (never handled) stale request — sorted first
        # by find_pending_request's oldest-first (mtime) ordering.
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        stale_path = req_dir / "request-stale.json"
        stale_path.write_text(
            json.dumps(
                {
                    "request_status": "queued",
                    "request_id": "cycle-stale-old",
                    "task_title": "STALE OLDEST TITLE — should never be logged",
                }
            ),
            encoding="utf-8",
        )

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            return {
                "task_title": "Add a smoke test for the loop metrics report",
                "rationale": "Closes a coverage gap.",
                "target_path": "tests/test_loop_metrics_extra.py",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)

        bridge._maybe_propose_after_skip(None)

        captured = capsys.readouterr()
        assert "llm-proposer: queued" in captured.out
        assert "STALE OLDEST TITLE" not in captured.out
        assert "loop metrics report" in captured.out

        # Confirm the stale request is indeed still the oldest queued
        # candidate find_pending_request would return — proving this test
        # actually exercises the bug's precondition, not a no-op.
        found_path, found_req = bridge.find_pending_request()
        assert found_path == stale_path
        assert found_req.get("task_title") == "STALE OLDEST TITLE — should never be logged"


# ─── #751: honest no-op (no_valuable_task) ─────────────────────────────────


class TestHonestNoOp:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")

    def test_no_valuable_task_reply_skips_without_minting_request(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            return {"no_valuable_task": True, "reason": "everything worthwhile is already queued"}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)

        assert result is None
        assert not (state_dir / "subagents" / "requests").exists()

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        skip_rows = [r for r in rows if r.get("phase") == "proposer_skip"]
        assert len(skip_rows) == 1
        assert skip_rows[0]["reason"] == "everything worthwhile is already queued"
        # Must never be a 'proposed' row (would pollute title-based dedup /
        # the #751 goal-alignment counts in loop_metrics_report.py).
        assert not [r for r in rows if r.get("phase") == "proposed"]

    def test_string_true_noop_reply_is_honored(self, tmp_path, monkeypatch):
        """#760 roll-out fix (live 2026-07-15 18:29Z): the weak host model
        emits no_valuable_task as the STRING "true"; that reply fell through
        to validate_sizing and burned a retry call on 'task_title is empty'.
        Truthy string/int forms must be honored as an honest no-op."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        calls = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, **kw):
            calls.append(1)
            return {"no_valuable_task": "true", "reason": "nothing addressable"}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        assert llm_proposer.maybe_propose(state_dir, None) is None
        assert len(calls) == 1  # no burned sizing-retry call

        rows = [json.loads(line) for line in (state_dir / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        assert [r for r in rows if r.get("phase") == "proposer_skip"]
        assert not [r for r in rows if r.get("phase") == "proposer_reject"]

    def test_sizing_reject_detail_includes_reply_snippet(self, tmp_path, monkeypatch):
        """#760 roll-out fix: 'task_title is empty' alone was undiagnosable —
        the reject row's detail must show what the model actually sent."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, **kw):
            return {"no_valuable_task": "maybe", "task_title": ""}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        assert llm_proposer.maybe_propose(state_dir, None) is None

        rows = [json.loads(line) for line in (state_dir / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        rejects = [r for r in rows if r.get("phase") == "proposer_reject"]
        assert rejects and rejects[0]["reason"] == "sizing_rejected"
        assert "reply=" in (rejects[0].get("detail") or "")
        assert "no_valuable_task" in (rejects[0].get("detail") or "")

    def test_noop_reply_on_dedup_retry_is_honored(self, tmp_path, monkeypatch):
        """#760 follow-up, fired live 2026-07-15 20:42-21:02Z: a model told
        'your proposal duplicates X' honestly answered no_valuable_task, but
        the dedup-retry path lacked the no-op check — three honest refusals
        were recorded as sizing_rejected instead of proposer_skip."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        replies = [
            {"task_title": "Create duplicate thing", "rationale": "r",
             "target_path": "scripts/dup_thing.py", "serves": "vector 1: x"},
            {"no_valuable_task": True, "reason": "the only candidate is a duplicate"},
        ]

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, **kw):
            return replies.pop(0)

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        monkeypatch.setattr(
            llm_proposer, "_is_duplicate_proposal",
            lambda *_a, **_k: (True, "duplicates existing work", "scripts/existing.py"),
        )

        assert llm_proposer.maybe_propose(state_dir, None) is None

        rows = [json.loads(line) for line in (state_dir / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        skips = [r for r in rows if r.get("phase") == "proposer_skip"]
        assert len(skips) == 1
        assert skips[0]["reason"] == "the only candidate is a duplicate"
        assert not [r for r in rows if r.get("phase") == "proposer_reject"]

    def test_reason_defaults_when_absent(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            return {"no_valuable_task": True}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        assert llm_proposer.maybe_propose(state_dir, None) is None

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        skip_rows = [r for r in rows if r.get("phase") == "proposer_skip"]
        assert len(skip_rows) == 1
        assert skip_rows[0]["reason"]  # non-empty placeholder, never blank

    def test_consecutive_cap_forces_normal_mode_on_fourth_call(self, tmp_path, monkeypatch):
        """#751 kill-switch bound: after _MAX_CONSECUTIVE_NOOP_SKIPS (3)
        trailing skips, the 4th call is forced into normal proposal mode —
        the context carries the forced-proposal note, and even if the model
        still tries no_valuable_task on its first reply, that reply is
        ignored (treated as an ordinary schema violation: missing
        task_title) rather than honored."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        for i in range(llm_proposer._MAX_CONSECUTIVE_NOOP_SKIPS):
            _append_skip(state_dir, reason=f"skip {i}")

        assert llm_proposer._consecutive_noop_streak(state_dir) == llm_proposer._MAX_CONSECUTIVE_NOOP_SKIPS

        captured_contexts = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            captured_contexts.append(context)
            if rejection_reason is None:
                return {"no_valuable_task": True, "reason": "still nothing, honestly"}
            return {
                "task_title": "Least-wasteful available option",
                "rationale": "Forced proposal after the no-op cap.",
                "target_path": "docs/forced.md",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)

        assert result == "Implement and commit: Least-wasteful available option"
        assert len(captured_contexts) == 2
        assert llm_proposer._FORCE_PROPOSAL_NOTE in captured_contexts[0]

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        # No additional proposer_skip row was recorded — the forced call
        # produced a real proposal instead of another honored skip.
        assert len([r for r in rows if r.get("phase") == "proposer_skip"]) == llm_proposer._MAX_CONSECUTIVE_NOOP_SKIPS

    def test_streak_counts_only_trailing_skips_after_last_proposal(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _append_proposed(state_dir, "c1", "Some earlier proposal")
        _append_skip(state_dir, "skip 1")
        _append_skip(state_dir, "skip 2")
        assert llm_proposer._consecutive_noop_streak(state_dir) == 2

        _append_proposed(state_dir, "c2", "A newer proposal resets the streak")
        assert llm_proposer._consecutive_noop_streak(state_dir) == 0


# ─── #751: hypothesis-backlog context section ──────────────────────────────


class TestBuildContextHypotheses:
    def test_includes_hypothesis_section_from_backlog_json(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        backlog_dir = state_dir / "hypotheses"
        backlog_dir.mkdir(parents=True)
        (backlog_dir / "backlog.json").write_text(
            json.dumps(
                {
                    "entries": [
                        {"hypothesis_id": "hypothesis-h1", "task_title": "Investigate flaky test X"},
                        {"hypothesis_id": "hypothesis-h2", "task_title": "Reduce cycle disk writes"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        context = llm_proposer.build_context(state_dir, None)

        assert "## Hypothesis backlog (candidate value sources)" in context
        assert "- [hypothesis-h1] Investigate flaky test X" in context
        assert "- [hypothesis-h2] Reduce cycle disk writes" in context

    def test_corrupt_backlog_file_omits_section(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        backlog_dir = state_dir / "hypotheses"
        backlog_dir.mkdir(parents=True)
        (backlog_dir / "backlog.json").write_text("not valid json {{{", encoding="utf-8")

        context = llm_proposer.build_context(state_dir, None)
        assert "Hypothesis backlog" not in context

    def test_absent_when_no_hypothesis_files(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        context = llm_proposer.build_context(state_dir, None)
        assert "Hypothesis backlog" not in context


# ─── #762: proposer_reject ledger rows for silent maybe_propose exits ──────


def _ledger_rows(state_dir: Path) -> list[dict]:
    ledger_path = state_dir / "ledger" / "cycles.jsonl"
    if not ledger_path.is_file():
        return []
    return [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _reject_rows(state_dir: Path) -> list[dict]:
    return [r for r in _ledger_rows(state_dir) if r.get("phase") == "proposer_reject"]


def _append_reject(state_dir: Path, reason: str = "self_dedup", **extra) -> None:
    cycle_ledger.append_event(state_dir, {"phase": "proposer_reject", "reason": reason, **extra})


class TestProposerRejectLedger:
    def test_dedup_exhaustion_skips_before_llm_call(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        demand_id = "defect-exhausted"
        for _ in range(llm_proposer._DEFAULT_DEDUP_EXHAUSTION_K):
            _append_reject(state_dir, "self_dedup", demand_id=demand_id)
        item = {"id": demand_id, "kind": "defect", "summary": "same issue", "evidence": "e"}
        monkeypatch.setenv(demand.ENABLED_ENV, "1")
        monkeypatch.setattr(llm_proposer, "should_propose", lambda *_: True)
        monkeypatch.setattr(demand, "collect_demand", lambda *a, **k: [item])
        monkeypatch.setattr(llm_proposer, "_select_assigned_demand", lambda *a, **k: [item])
        monkeypatch.setattr(llm_proposer, "propose", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called")))
        assert llm_proposer.maybe_propose(state_dir, None) is None
        skips = [r for r in _ledger_rows(state_dir) if r.get("phase") == "proposer_skip"]
        assert skips[-1]["reason"] == f"dedup_exhausted: demand {demand_id}"

    def _run_with_dedup_history(self, state_dir, monkeypatch, demand_id, rejects):
        """Drive maybe_propose past the pre-call skip and report whether the LLM ran.

        Mirrors test_dedup_exhaustion_skips_before_llm_call, except `propose`
        records the call instead of failing it, so the two directions of the
        #998 skip are exercised by the same setup.
        """
        for kwargs in rejects:
            _append_reject(state_dir, "self_dedup", demand_id=demand_id, **kwargs)
        item = {"id": demand_id, "kind": "defect", "summary": "same issue", "evidence": "e"}
        called = []
        monkeypatch.setenv(demand.ENABLED_ENV, "1")
        monkeypatch.setattr(llm_proposer, "should_propose", lambda *_: True)
        monkeypatch.setattr(demand, "collect_demand", lambda *a, **k: [item])
        monkeypatch.setattr(llm_proposer, "_select_assigned_demand", lambda *a, **k: [item])
        monkeypatch.setattr(llm_proposer, "propose", lambda *a, **k: called.append(True))
        llm_proposer.maybe_propose(state_dir, None)
        skipped = [
            r
            for r in _ledger_rows(state_dir)
            if r.get("phase") == "proposer_skip"
            and str(r.get("reason") or "").startswith("dedup_exhausted")
        ]
        return bool(called), skipped

    def test_shorter_dedup_history_still_gets_its_llm_call(self, tmp_path, monkeypatch):
        """#998 AC2: below K self_dedup rejections, the LLM call must still happen.

        The skip is an efficiency measure, not a mute button. Testing only the
        positive direction would let a too-eager predicate (or a later
        off-by-one on K) silently stop the proposer from ever calling the model
        while every existing test stayed green.
        """
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        rejects = [{}] * (llm_proposer._DEFAULT_DEDUP_EXHAUSTION_K - 1)
        assert rejects, "K must be >= 2 for a 'shorter history' case to exist"
        called, skipped = self._run_with_dedup_history(
            state_dir, monkeypatch, "defect-shorter", rejects
        )
        assert called, "LLM was skipped on a history shorter than K"
        assert not skipped

    def test_expired_dedup_history_still_gets_its_llm_call(self, tmp_path, monkeypatch):
        """#998 AC2: K rejections older than D days no longer exhaust the item.

        `_dedup_exhausted` stops counting at the first row older than the
        window, so a demand item the model gave up on weeks ago becomes
        eligible again instead of being skipped forever.
        """
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        stale = datetime.now(timezone.utc) - timedelta(
            days=llm_proposer._DEFAULT_DEDUP_EXHAUSTION_DAYS + 1
        )
        rejects = [
            {"ts": stale.isoformat().replace("+00:00", "Z")}
            for _ in range(llm_proposer._DEFAULT_DEDUP_EXHAUSTION_K)
        ]
        called, skipped = self._run_with_dedup_history(
            state_dir, monkeypatch, "defect-expired", rejects
        )
        assert called, "LLM was skipped on a dedup history outside the D-day window"
        assert not skipped

    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")

    def test_empty_context_records_reject(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "")

        assert llm_proposer.maybe_propose(state_dir, None) is None

        rows = _reject_rows(state_dir)
        assert len(rows) == 1
        assert rows[0]["reason"] == "empty_context"
        # Never a 'proposed'/'proposer_skip' row — a reject must not pollute
        # title-based dedup or the goal-alignment/no-op counts.
        assert not [r for r in _ledger_rows(state_dir) if r.get("phase") in ("proposed", "proposer_skip")]

    def test_double_sizing_failure_records_reject_with_title(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _always_bad(context, *, rejection_reason=None, timeout=120.0):
            return {
                "task_title": "A task that is always mis-sized",
                "rationale": "",
                "target_path": "scripts/bad.py",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _always_bad)
        monkeypatch.setattr(llm_proposer, "validate_sizing", lambda p: (False, "rationale is empty"))

        assert llm_proposer.maybe_propose(state_dir, None) is None

        rows = _reject_rows(state_dir)
        assert len(rows) == 1
        assert rows[0]["reason"] == "sizing_rejected"
        assert rows[0]["task_title"] == "A task that is always mis-sized"
        assert rows[0]["target_path"] == "scripts/bad.py"
        assert "rationale is empty" in rows[0]["detail"]

    def test_double_self_dedup_records_reject_with_matched_against(self, tmp_path, monkeypatch):
        """The live-saturation case: both dedup checks flag the proposal.
        The reject row must carry the rejected title AND what it matched."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")
        _append_proposed(state_dir, "c-old", "Implement lightweight memory usage monitor for eeepc")

        def _always_clone(context, *, rejection_reason=None, timeout=120.0):
            return {
                "task_title": "Implement lightweight memory usage tracker",
                "rationale": "Tracks memory usage.",
                "target_path": "scripts/mem.py",
                "serves": "priority 4",
            }

        monkeypatch.setattr(llm_proposer, "propose", _always_clone)
        assert llm_proposer.maybe_propose(state_dir, None) is None
        assert not (state_dir / "subagents" / "requests").exists()

        rows = _reject_rows(state_dir)
        assert len(rows) == 1
        assert rows[0]["reason"] == "self_dedup"
        assert rows[0]["task_title"] == "Implement lightweight memory usage tracker"
        assert rows[0]["target_path"] == "scripts/mem.py"
        # matched_against records what the heuristic actually matched (the
        # prior proposed title), not an echo of the proposal's own title.
        assert rows[0]["matched_against"] == "Implement lightweight memory usage monitor for eeepc"

    def test_self_dedup_via_forced_tuple_records_matched_against(self, tmp_path, monkeypatch):
        """Drive the dedup exit deterministically by monkeypatching
        _is_duplicate_proposal itself."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            return {
                "task_title": "Some perfectly sized task",
                "rationale": "Does something useful.",
                "target_path": "scripts/useful.py",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        monkeypatch.setattr(
            llm_proposer,
            "_is_duplicate_proposal",
            lambda *a, **k: (True, "duplicates already-done work", "feat: the matched historical line"),
        )

        assert llm_proposer.maybe_propose(state_dir, None) is None

        rows = _reject_rows(state_dir)
        assert len(rows) == 1
        assert rows[0]["reason"] == "self_dedup"
        assert rows[0]["matched_against"] == "feat: the matched historical line"
        assert rows[0]["task_title"] == "Some perfectly sized task"

    def test_catch_all_error_records_reject(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _boom(context, *, rejection_reason=None, timeout=120.0):
            raise RuntimeError("proposer exploded")

        monkeypatch.setattr(llm_proposer, "propose", _boom)

        assert llm_proposer.maybe_propose(state_dir, None) is None

        rows = _reject_rows(state_dir)
        assert len(rows) == 1
        assert rows[0]["reason"] == "llm_unavailable"
        assert "RuntimeError" in rows[0]["detail"]
        assert "proposer exploded" in rows[0]["detail"]

    def test_ledger_write_failure_does_not_break_maybe_propose(self, tmp_path, monkeypatch):
        """Fail-open: a raising ledger writer must not escape maybe_propose —
        on the empty-context path AND from inside the final except block."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _raise(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(llm_proposer, "append_event", _raise)

        # empty_context path
        monkeypatch.setattr(llm_proposer, "build_context", lambda *a, **k: "")
        assert llm_proposer.maybe_propose(state_dir, None) is None

        # error (catch-all) path — recording happens inside the except block
        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(llm_proposer, "build_context", _boom)
        assert llm_proposer.maybe_propose(state_dir, None) is None

    def test_successful_proposal_records_no_reject_row(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            return {
                "task_title": "Add a smoke test for the loop metrics report",
                "rationale": "Closes a coverage gap.",
                "target_path": "tests/test_loop_metrics_extra.py",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        assert llm_proposer.maybe_propose(state_dir, None) is not None
        assert _reject_rows(state_dir) == []


class TestConsecutiveSelfDedupRejects:
    def test_zero_on_empty_ledger(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert llm_proposer._consecutive_self_dedup_rejects(state_dir) == 0

    def test_counts_trailing_run(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _append_proposed(state_dir, "c1", "Some earlier proposal")
        _append_reject(state_dir)
        _append_reject(state_dir)
        _append_reject(state_dir)
        assert llm_proposer._consecutive_self_dedup_rejects(state_dir) == 3

    def test_resets_on_intervening_proposed_row(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _append_reject(state_dir)
        _append_reject(state_dir)
        _append_proposed(state_dir, "c2", "A newer proposal resets the streak")
        assert llm_proposer._consecutive_self_dedup_rejects(state_dir) == 0

    def test_resets_on_intervening_proposer_skip_row(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _append_reject(state_dir)
        _append_skip(state_dir, "nothing valuable")
        assert llm_proposer._consecutive_self_dedup_rejects(state_dir) == 0
        _append_reject(state_dir)
        assert llm_proposer._consecutive_self_dedup_rejects(state_dir) == 1

    def test_non_self_dedup_reject_reasons_do_not_count(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _append_reject(state_dir, reason="self_dedup")
        _append_reject(state_dir, reason="error")
        assert llm_proposer._consecutive_self_dedup_rejects(state_dir) == 0

    def test_unrelated_phases_are_ignored_not_resetting(self, tmp_path):
        """Bridge-owned phases (outcome/dedup/...) between rejects don't
        reset the streak — only the proposer's own decision rows do, same
        filtering as _consecutive_noop_streak."""
        state_dir = _state_dir(tmp_path)
        _append_reject(state_dir)
        _append_outcome(state_dir, "c1", "success")
        _append_reject(state_dir)
        assert llm_proposer._consecutive_self_dedup_rejects(state_dir) == 2


# ─── #760: demand-driven mode ───────────────────────────────────────────────


def _idle_rows(state_dir: Path) -> list[dict]:
    return [r for r in _ledger_rows(state_dir) if r.get("phase") == "idle"]


class TestDemandDrivenMode:
    """#760: with SELFEVO_DEMAND_DRIVEN_ENABLED on (the default), the
    proposer works only when demand exists; empty demand means ZERO LLM
    calls and one idle heartbeat ledger row per bridge cycle."""

    @pytest.fixture(autouse=True)
    def _demand_on(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        monkeypatch.setenv(DEMAND_ENV, "1")
        monkeypatch.setattr(llm_proposer, "_idle_recorded_this_process", False)

    def test_empty_demand_should_propose_false_and_records_idle(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "mission text with no priority-targets section")

        assert llm_proposer.should_propose(state_dir, None) is False

        rows = _idle_rows(state_dir)
        assert len(rows) == 1
        assert rows[0]["reason"] == "no_demand"
        assert "cycle_id" not in rows[0]

    def test_idle_row_at_most_once_per_bridge_cycle(self, tmp_path):
        """One bridge cycle == one process invocation; a second
        should_propose call in the same process must not write a second
        idle row."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "mission text with no priority-targets section")

        assert llm_proposer.should_propose(state_dir, None) is False
        assert llm_proposer.should_propose(state_dir, None) is False
        assert len(_idle_rows(state_dir)) == 1

    def test_maybe_propose_makes_zero_llm_calls_when_no_demand(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "mission text with no priority-targets section")

        def _must_not_be_called(*a, **k):
            raise AssertionError("LLM was called on an idle (no-demand) cycle")

        monkeypatch.setattr(llm_proposer, "propose", _must_not_be_called)
        assert llm_proposer.maybe_propose(state_dir, None) is None
        assert len(_idle_rows(state_dir)) == 1
        assert not (state_dir / "subagents" / "requests").exists()

    def test_r30_fresh_seeded_priority_wakes_loop_and_proposes(self, tmp_path, monkeypatch):
        """R30 regression, verbatim scenario: an operator seeds fresh
        goal_text priorities (nothing done yet, nothing queued) — the loop
        must wake and propose. Under #760 the priorities surface as demand
        kind 'priority'."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])

        items = demand.collect_demand(state_dir, None)
        priority_items = [i for i in items if i["kind"] == "priority"]
        assert len(priority_items) == 2  # Priority 5 and Priority 6, both fresh
        assert llm_proposer.should_propose(state_dir, None) is True

        demand_id = priority_items[0]["id"]

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, **kwargs):
            assert "## Demand" in context
            return {
                "task_title": "Write scripts/cycle_logger.py with append_cycle_summary helper",
                "rationale": "Implements the operator-seeded priority 5 target.",
                "target_path": "scripts/cycle_logger.py",
                "serves": f"demand {demand_id}",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)

        assert result == (
            "Implement and commit: Write scripts/cycle_logger.py with append_cycle_summary helper"
        )
        assert _idle_rows(state_dir) == []
        proposed = [r for r in _ledger_rows(state_dir) if r.get("phase") == "proposed"]
        assert len(proposed) == 1
        assert proposed[0]["demand_id"] == demand_id
        assert proposed[0]["serves"] == f"demand {demand_id}"

    def test_demand_mode_uses_demand_system_prompt(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])

        captured: dict = {}

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0, **kwargs):
            captured.update(kwargs)
            return {
                "task_title": "Write scripts/cycle_logger.py helper",
                "rationale": "Implements priority 5.",
                "target_path": "scripts/cycle_logger.py",
                "serves": "demand priority-abc123def456",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        assert llm_proposer.maybe_propose(state_dir, None) is not None
        assert captured.get("system_prompt") == llm_proposer._DEMAND_PROPOSER_SYSTEM_PROMPT

    def test_self_dedup_reject_row_carries_demand_id(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        _append_proposed(state_dir, "c-old", "Implement lightweight memory usage monitor for eeepc")

        def _always_clone(context, *, rejection_reason=None, timeout=120.0, **kwargs):
            return {
                "task_title": "Implement lightweight memory usage tracker",
                "rationale": "Tracks memory usage.",
                "target_path": "scripts/mem.py",
                "serves": "demand defect-1a2b3c4d5e6f",
            }

        monkeypatch.setattr(llm_proposer, "propose", _always_clone)
        assert llm_proposer.maybe_propose(state_dir, None) is None

        rows = _reject_rows(state_dir)
        assert len(rows) == 1
        assert rows[0]["reason"] == "self_dedup"
        assert rows[0]["demand_id"] == "defect-1a2b3c4d5e6f"

    def test_anti_stacking_guard_still_blocks_before_demand_gate(self, tmp_path):
        """The existing gates are kept: a queued unhandled proposer request
        blocks regardless of demand, and no idle row is written (empty
        demand is not the reason for the refusal)."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "request-y.json").write_text(
            json.dumps({"request_status": "queued", "request_id": "llm-proposer-cycle-abc123"}),
            encoding="utf-8",
        )
        assert llm_proposer.should_propose(state_dir, None) is False
        assert _idle_rows(state_dir) == []

    def test_killswitch_off_restores_pre_760_behavior(self, tmp_path, monkeypatch):
        """SELFEVO_DEMAND_DRIVEN_ENABLED=0 restores the supply-driven policy
        wholesale: an empty queue fires should_propose even with zero demand,
        and no idle row is ever written."""
        monkeypatch.setenv(DEMAND_ENV, "0")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "mission text with no priority-targets section")

        assert llm_proposer.should_propose(state_dir, None) is True
        assert _idle_rows(state_dir) == []


class TestDemandSection:
    def test_demand_section_present_with_kind_summary_and_evidence(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        items = [
            {
                "kind": "defect",
                "id": "defect-0011aabbccdd",
                "summary": "script fails to compile: scripts/broken.py",
                "evidence": "SyntaxError: invalid syntax (line 3)",
                "affected_path": "scripts/broken.py",
            }
        ]
        context = llm_proposer.build_context(state_dir, None, demand_items=items)
        assert "## Demand" in context
        assert "[defect-0011aabbccdd] (defect) script fails to compile: scripts/broken.py" in context
        assert 'evidence: "SyntaxError: invalid syntax (line 3)"' in context
        assert "Select ONE demand item" in context
        assert "no_valuable_task" in context

    def test_demand_section_bounded(self):
        items = [
            {
                "kind": "defect",
                "id": f"defect-{i:012d}",
                "summary": "x" * 150,
                "evidence": "y" * 200,
                "affected_path": "",
            }
            for i in range(200)
        ]
        section = llm_proposer._demand_section(items)
        assert len(section) <= llm_proposer._MAX_DEMAND_CHARS

    def test_no_demand_items_no_section(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "some real goal text")
        context = llm_proposer.build_context(state_dir, None, demand_items=[])
        assert "## Demand" not in context


class TestServesDemandForm:
    def _good(self, **overrides):
        proposal = {
            "task_title": "Fix the broken script",
            "rationale": "Resolves a compile defect.",
            "target_path": "scripts/broken.py",
            "serves": "demand defect-1a2b3c4d5e6f",
        }
        proposal.update(overrides)
        return proposal

    def test_accepts_demand_form(self):
        ok, reason = llm_proposer.validate_sizing(self._good())
        assert ok is True, reason

    def test_legacy_forms_still_accepted_for_one_release(self):
        for serves in ("priority 3", "vector 1: reduces disk writes", "vector 2", "hypothesis h3"):
            ok, reason = llm_proposer.validate_sizing(self._good(serves=serves))
            assert ok is True, (serves, reason)

    def test_demand_id_extraction(self):
        assert llm_proposer._demand_id_from_serves("demand defect-1a2b3c4d5e6f") == "defect-1a2b3c4d5e6f"
        assert llm_proposer._demand_id_from_serves("DEMAND priority-aabb") == "priority-aabb"
        assert llm_proposer._demand_id_from_serves("priority 3") == ""
        assert llm_proposer._demand_id_from_serves("") == ""
        assert llm_proposer._demand_id_from_serves(None) == ""


# ─── #832: operator-controlled bridge reasoning effort ─────────────────────


class TestBridgeReasoningEffort:
    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("SUBAGENT_BRIDGE_REASONING_EFFORT", raising=False)
        assert llm_proposer.bridge_reasoning_effort() is None

    @pytest.mark.parametrize("val", ["low", "medium", "high"])
    def test_valid_tiers(self, monkeypatch, val):
        monkeypatch.setenv("SUBAGENT_BRIDGE_REASONING_EFFORT", val)
        assert llm_proposer.bridge_reasoning_effort() == val

    def test_case_and_whitespace_normalized(self, monkeypatch):
        monkeypatch.setenv("SUBAGENT_BRIDGE_REASONING_EFFORT", "  HIGH ")
        assert llm_proposer.bridge_reasoning_effort() == "high"

    @pytest.mark.parametrize("val", ["", "bogus", "extreme", "1"])
    def test_invalid_returns_none(self, monkeypatch, val):
        monkeypatch.setenv("SUBAGENT_BRIDGE_REASONING_EFFORT", val)
        assert llm_proposer.bridge_reasoning_effort() is None


class TestProposeReasoningPassthrough:
    def _install(self, monkeypatch):
        captured: dict = {}

        class _Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _FakeResponse(json.dumps({"task_title": "x", "target_path": "docs/z.md"}))

        class _Client:
            def __init__(self, *a, **kw):
                self.chat = type("_C", (), {"completions": _Completions()})()

        import openai

        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: _Client())
        monkeypatch.setenv("LITELLM_BASE_URL", "http://fake-gateway.local")
        monkeypatch.setenv("LITELLM_API_KEY", "sk-fake")
        return captured

    def test_high_effort_forwarded(self, monkeypatch):
        captured = self._install(monkeypatch)
        monkeypatch.setenv("SUBAGENT_BRIDGE_REASONING_EFFORT", "high")
        llm_proposer.propose("ctx")
        assert captured.get("reasoning_effort") == "high"

    def test_unset_sends_no_param(self, monkeypatch):
        captured = self._install(monkeypatch)
        monkeypatch.delenv("SUBAGENT_BRIDGE_REASONING_EFFORT", raising=False)
        llm_proposer.propose("ctx")
        assert "reasoning_effort" not in captured


# ─── #834: permanent (history-wide) novelty guard ──────────────────────────


def _init_instance_repo(tmp_path, *, subject, script_rel, old=True):
    """Minimal git repo with ONE commit (dated >14 days ago when old=True) that
    creates `script_rel`, so the 14-day recency git_log misses it but the
    full-history _all_built_subjects catches it."""
    import subprocess as _sp
    repo = tmp_path / "inst"
    repo.mkdir()
    env = dict(__import__("os").environ)
    if old:
        env["GIT_AUTHOR_DATE"] = "2025-01-01T00:00:00"
        env["GIT_COMMITTER_DATE"] = "2025-01-01T00:00:00"
    env.setdefault("GIT_AUTHOR_NAME", "t"); env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    env.setdefault("GIT_COMMITTER_NAME", "t"); env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
    def g(*a):
        _sp.run(["git", "-C", str(repo), *a], capture_output=True, env=env, check=False)
    g("init", "-b", "main")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / script_rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / script_rel).write_text("# tool\n")
    g("add", "-A")
    _sp.run(["git", "-C", str(repo), "commit", "-m", subject],
            capture_output=True, env=env, check=False)
    return repo


class TestPermanentNoveltyGuard:
    def test_new_file_duplicating_old_built_artifact_rejected(self, tmp_path):
        repo = _init_instance_repo(
            tmp_path,
            subject="create firewall log analyzer summary script",
            script_rel="scripts/firewall_log_analyzer.py",
        )
        state = tmp_path / "state"; state.mkdir()
        proposal = {
            "task_title": "create firewall log analyzer summary script",
            "target_path": "scripts/firewall_log_analyzer_v2.py",  # NEW file
        }
        dup, feedback, matched = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is True
        assert "ALREADY EXISTS" in feedback
        assert "firewall" in matched.lower()

    def test_edit_of_existing_file_not_blocked(self, tmp_path):
        repo = _init_instance_repo(
            tmp_path,
            subject="create firewall log analyzer summary script",
            script_rel="scripts/firewall_log_analyzer.py",
        )
        state = tmp_path / "state"; state.mkdir()
        proposal = {
            "task_title": "create firewall log analyzer summary script",
            "target_path": "scripts/firewall_log_analyzer.py",  # EXISTS -> edit
        }
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False  # iteration on an existing artifact is never blocked

    def test_novel_new_file_passes(self, tmp_path):
        repo = _init_instance_repo(
            tmp_path,
            subject="create firewall log analyzer summary script",
            script_rel="scripts/firewall_log_analyzer.py",
        )
        state = tmp_path / "state"; state.mkdir()
        proposal = {
            "task_title": "add dashboard latency histogram exporter",  # unrelated
            "target_path": "scripts/latency_histogram_exporter.py",
        }
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, repo, proposal)
        assert dup is False

    def test_fail_open_without_repo(self, tmp_path):
        state = tmp_path / "state"; state.mkdir()
        proposal = {"task_title": "create firewall log analyzer summary script",
                    "target_path": "scripts/x.py"}
        dup, _, _ = llm_proposer._is_duplicate_proposal(state, None, proposal)
        assert dup is False

    def test_helpers_direct(self, tmp_path):
        repo = _init_instance_repo(
            tmp_path, subject="build token usage reporter", script_rel="scripts/token_usage_reporter.py")
        assert "token usage reporter" in llm_proposer._all_built_subjects(repo).lower()
        assert llm_proposer._all_built_subjects(None) == ""
        assert llm_proposer._proposal_creates_new_file(repo, {"target_path": "scripts/new.py"}) is True
        assert llm_proposer._proposal_creates_new_file(repo, {"target_path": "scripts/token_usage_reporter.py"}) is False
        assert llm_proposer._proposal_creates_new_file(repo, {"target_path": ""}) is False
        assert llm_proposer._proposal_creates_new_file(None, {"target_path": "scripts/x.py"}) is False
        # #834 path-traversal containment: absolute / escaping targets are not
        # treated as repo new-files (never probed outside the repo).
        assert llm_proposer._proposal_creates_new_file(repo, {"target_path": "../../etc/passwd"}) is False
        assert llm_proposer._proposal_creates_new_file(repo, {"target_path": "/etc/passwd"}) is False


# ─── #878: refuted-hypothesis permanent novelty guard ──────────────────────


def _write_lifecycle(state_dir, entries: dict) -> None:
    d = state_dir / "hypotheses"
    d.mkdir(parents=True, exist_ok=True)
    (d / "lifecycle.json").write_text(
        json.dumps({"schema_version": "hypothesis-lifecycle-v1", "entries": entries}),
        encoding="utf-8",
    )


class TestRefutedHypothesisGuard:
    def test_refuted_hypothesis_titles_reads_lifecycle(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {"status": "answered", "verdict": "refuted", "title": "Cache the widget index"},
            "hypothesis-h2": {"status": "answered", "verdict": "supported", "title": "Batch the writer"},
            "hypothesis-h3": {"status": "active", "title": "Untouched idea"},
        })
        titles = llm_proposer._refuted_hypothesis_titles(state_dir)
        assert titles == ["Cache the widget index"]

    def test_refuted_hypothesis_titles_bounded_and_newest_first(self, tmp_path):
        """#878 opus-review Y2 fix: the refuted block-list must be bounded
        (mirrors hypothesis_backlog.SUPPORTED_TOP_N), newest verdict first —
        an unbounded list would keep growing the false-positive surface
        over a long RSI run."""
        from nanobot.runtime import hypothesis_backlog

        state_dir = tmp_path / "state"
        n = hypothesis_backlog.SUPPORTED_TOP_N
        entries = {
            f"hypothesis-h{i}": {
                "status": "answered",
                "verdict": "refuted",
                "verdict_at": f"2026-08-{i:02d}T00:00:00Z",
                "title": f"Refuted idea {i}",
            }
            for i in range(1, n + 3)  # strictly more entries than the cap
        }
        _write_lifecycle(state_dir, entries)
        titles = llm_proposer._refuted_hypothesis_titles(state_dir)
        assert len(titles) == n
        # Newest (highest day number) first.
        assert titles[0] == f"Refuted idea {n + 2}"

    def test_refuted_hypothesis_titles_fail_open(self, tmp_path):
        state_dir = tmp_path / "state"
        assert llm_proposer._refuted_hypothesis_titles(state_dir) == []
        d = state_dir / "hypotheses"
        d.mkdir(parents=True)
        (d / "lifecycle.json").write_text("not json {{{", encoding="utf-8")
        assert llm_proposer._refuted_hypothesis_titles(state_dir) == []

    def test_refuted_title_blocks_reproposal_permanently(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {
                "status": "answered",
                "verdict": "refuted",
                "title": "cache the widget lookup index for faster reads",
            },
        })
        proposal = {
            "task_title": "cache the widget lookup index for faster reads",
            "target_path": "nanobot/runtime/existence_index.py",
        }
        dup, feedback, matched = llm_proposer._is_duplicate_proposal(state_dir, None, proposal)
        assert dup is True
        assert "REFUTED" in feedback
        assert matched.startswith("refuted-hypothesis:")

    def test_supported_title_is_not_blocked(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {
                "status": "answered",
                "verdict": "supported",
                "title": "cache the widget lookup index for faster reads",
            },
        })
        proposal = {
            "task_title": "cache the widget lookup index for faster reads",
            "target_path": "nanobot/runtime/existence_index.py",
        }
        dup, _, _ = llm_proposer._is_duplicate_proposal(state_dir, None, proposal)
        assert dup is False

    def test_inconclusive_title_is_not_blocked(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {
                "status": "answered",
                "verdict": "inconclusive",
                "title": "cache the widget lookup index for faster reads",
            },
        })
        proposal = {
            "task_title": "cache the widget lookup index for faster reads",
            "target_path": "nanobot/runtime/existence_index.py",
        }
        dup, _, _ = llm_proposer._is_duplicate_proposal(state_dir, None, proposal)
        assert dup is False

    def test_refuted_guard_applies_without_new_file_creation(self, tmp_path, monkeypatch):
        """Unlike the #834 guard, the refuted-hypothesis guard must fire even
        when the proposal edits an EXISTING file (a hypothesis experiment
        need not create a new file)."""
        state_dir = tmp_path / "state"
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {
                "status": "answered",
                "verdict": "refuted",
                "title": "batch the ledger writer flush calls",
            },
        })
        # No repo passed at all — _proposal_creates_new_file would fail-open
        # to False anyway, but this also proves the guard doesn't depend on
        # the #834 code path running first.
        proposal = {
            "task_title": "batch the ledger writer flush calls",
            "target_path": "nanobot/runtime/existence_index.py",
        }
        dup, feedback, matched = llm_proposer._is_duplicate_proposal(state_dir, None, proposal)
        assert dup is True
        assert matched.startswith("refuted-hypothesis:")
