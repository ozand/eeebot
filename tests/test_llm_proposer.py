"""Tests for #707: the state-light LLM proposer.

Covers the kill-switch (default OFF), the invocation policy
(``should_propose``), the bounded context builder (``build_context``), the
pre-spawn sizing gate (``validate_sizing``), the C1 request-schema
equality invariant (``write_request`` vs
``nanobot.runtime.cycle_planning._write_subagent_request_artifact``), the
mocked-LLM ``propose`` parsing, and an end-to-end check that a
proposer-written request is picked up by the bridge's real
``find_pending_request`` exactly like a planner-written one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.runtime import bridge, cycle_ledger, llm_proposer
from nanobot.runtime.cycle_planning import _write_subagent_request_artifact
from tests.test_goal_backlog_routing import GOAL_TEXT_JSON, _make_git_repo_with_commit

ENV_VAR = llm_proposer.ENABLED_ENV


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

    def test_priorities_remain_and_not_all_dup_returns_false(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, GOAL_TEXT_JSON and json.loads(GOAL_TEXT_JSON)["text"])
        # No selfevo repo -> filter is a no-op -> priorities remain.
        # Fewer than 3 terminal rows, so the duplicate-streak branch is False too.
        _append_outcome(state_dir, "c1", "success")
        assert llm_proposer.should_propose(state_dir, None) is False

    def test_priorities_empty_returns_true(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "eeebot mission text with no priority-targets section.")
        assert llm_proposer.should_propose(state_dir, None) is True

    def test_last_three_all_skipped_duplicate_returns_true(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        for i in range(3):
            _append_outcome(state_dir, f"c{i}", "skipped-duplicate")
        assert llm_proposer.should_propose(state_dir, None) is True

    def test_last_three_not_all_duplicate_returns_false(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
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
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)
        result = llm_proposer.maybe_propose(state_dir, None)
        assert result == "Implement and commit: Fix a typo in docs/README-ish file"
        assert llm_proposer.should_propose(state_dir, None) is False


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


# ─── validate_sizing ─────────────────────────────────────────────────────────


class TestValidateSizing:
    def _good(self, **overrides):
        proposal = {
            "task_title": "Add a docstring example to scripts/loop_metrics_report.py",
            "rationale": "Improves discoverability of the report script's CLI usage.",
            "target_path": "scripts/loop_metrics_report.py",
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


class TestProposeRetryOnce(object):
    def test_retries_exactly_once_on_rejection(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        calls = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            calls.append(rejection_reason)
            if rejection_reason is None:
                return {"task_title": "", "rationale": "", "target_path": ""}  # invalid
            return {
                "task_title": "Fix a typo in docs/README-ish file",
                "rationale": "Corrects a small doc mistake.",
                "target_path": "docs/foo.md",
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
            return {"task_title": "", "rationale": "", "target_path": ""}

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
                }
            return {
                "task_title": "Add a smoke test for the loop metrics report",
                "rationale": "Closes an unrelated coverage gap.",
                "target_path": "tests/test_loop_metrics_extra.py",
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
                }
            return {
                "task_title": "Document the release checklist",
                "rationale": "Fills a documentation gap.",
                "target_path": "docs/release.md",
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
    def _fixture_request(self, tmp_path) -> dict:
        state_root = tmp_path / "fixture_state"
        state_root.mkdir()
        path = _write_subagent_request_artifact(
            state_root=state_root,
            cycle_id="cycle-fixture",
            goal_id="goal-bootstrap",
            current_plan={
                "current_task_id": "subagent-verify-materialized-improvement",
                "tasks": [],
                "selected_task_title": "Do the thing",
            },
        )
        assert path is not None
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def test_same_keys_and_queued_status(self, tmp_path):
        fixture = self._fixture_request(tmp_path)
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Document the ledger digest helper",
            "rationale": "Improves maintainer onboarding.",
            "target_path": "docs/proposer-notes.md",
        }
        path = llm_proposer.write_request(state_dir, proposal)
        written = json.loads(Path(path).read_text(encoding="utf-8"))

        assert set(written.keys()) == set(fixture.keys())
        assert written["request_status"] == "queued" == fixture["request_status"]

    def test_bridge_find_pending_request_accepts_it(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")

        proposal = {
            "task_title": "Add a helper doc",
            "rationale": "helps operators",
            "target_path": "docs/helper.md",
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
        }
        llm_proposer.write_request(state_dir, proposal)

        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        proposed_rows = [r for r in rows if r.get("phase") == "proposed"]
        assert len(proposed_rows) == 1
        assert proposed_rows[0]["task_title"] == "Add a helper doc"
        assert proposed_rows[0]["target_path"] == "docs/helper.md"
        assert proposed_rows[0]["source_artifact"] == "llm_proposer"


# ─── integration: maybe_propose -> bridge.find_pending_request handoff ─────


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
