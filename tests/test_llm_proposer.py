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


def _append_outcome(state_dir: Path, cycle_id: str, outcome: str) -> None:
    cycle_ledger.append_event(
        state_dir, {"phase": "outcome", "cycle_id": cycle_id, "outcome": outcome}
    )


def _append_skip(state_dir: Path, reason: str = "nothing valuable") -> None:
    cycle_ledger.append_event(state_dir, {"phase": "proposer_skip", "reason": reason})


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
            "## Existing scripts (do not duplicate — extend or skip instead)\n"
        )


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


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


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


def _append_reject(state_dir: Path, reason: str = "self_dedup") -> None:
    cycle_ledger.append_event(state_dir, {"phase": "proposer_reject", "reason": reason})


class TestProposerRejectLedger:
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
        assert rows[0]["reason"] == "error"
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
