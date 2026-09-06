"""Tests for #1374: joining a paid LLM call to the proposer attempt that
bought it.

Before this fix, ``llm_proposer.propose`` re-entered
``llm_telemetry.call_context`` with ``cycle_id=None`` right before recording
its own telemetry, which erased whatever cycle_id the caller had attributed
-- every proposer LLM-call telemetry row and every ``proposer_skip`` /
``proposer_reject`` ledger row carried ``cycle_id: ""`` (or no key at all),
so a paid call could never be joined back to the attempt (request /
``proposed`` row) it produced. The fix: ``llm_telemetry.current_cycle_id()``
reads the ambient context without erasing it, ``maybe_propose`` mints one
``cycle_id`` up front and sets it as the ambient context for the whole
attempt, ``propose`` re-enters the context WITH that id (not ``None``), and
``write_request`` reuses the ambient id instead of always minting a fresh
one.
"""
from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nanobot.observability.llm_telemetry import call_context, current_cycle_id
from nanobot.runtime import cycle_ledger, demand, llm_proposer

ENV_VAR = llm_proposer.ENABLED_ENV
DEMAND_ENV = demand.ENABLED_ENV

CYCLE_ID_RE = re.compile(r"^cycle-[0-9a-f]{12}$")


@pytest.fixture(autouse=True)
def _pre_760_mode(monkeypatch):
    """Same pin as tests/test_llm_proposer.py: exercise the pre-#760
    supply-driven ``should_propose`` policy (kill-switch OFF for demand
    mode) so ``maybe_propose`` fires deterministically off a bare goal_text
    with no priorities section, and reset the once-per-process idle marker
    so tests are order-independent."""
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


def _read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _ledger_rows(state_dir: Path) -> list[dict]:
    ledger_path = state_dir / "ledger" / "cycles.jsonl"
    if not ledger_path.is_file():
        return []
    return _read_jsonl(ledger_path)


def _load_report_module():
    """Import scripts/llm_calls_report.py as a module, same pattern as
    tests/test_llm_calls_report.py."""
    script_path = Path(__file__).parent.parent / "scripts" / "llm_calls_report.py"
    spec = importlib.util.spec_from_file_location("llm_calls_report", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_loop_metrics_module():
    """Import scripts/loop_metrics_report.py as a module, same pattern as
    tests/test_loop_metrics_report.py."""
    script_path = Path(__file__).parent.parent / "scripts" / "loop_metrics_report.py"
    spec = importlib.util.spec_from_file_location("loop_metrics_report", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─── fake OpenAI client, same shape as tests/test_llm_proposer.py's ────────
# ─── TestProposeMockedClient fixtures, reused here for test 2            ───


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


def _patch_openai_client(monkeypatch, content):
    def _factory(*args, **kwargs):
        return _FakeClient(content=content)

    import openai

    monkeypatch.setattr(openai, "OpenAI", _factory)
    monkeypatch.setenv("LITELLM_BASE_URL", "http://fake-gateway.local")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-fake")


# ─── 1. current_cycle_id() ambient-context contract ────────────────────────


class TestCurrentCycleId:
    def test_empty_outside_any_context(self):
        assert current_cycle_id() == ""

    def test_returns_the_id_inside_call_context(self):
        with call_context("cycle-x", "bridge"):
            assert current_cycle_id() == "cycle-x"

    def test_empty_again_after_context_exits(self):
        with call_context("cycle-x", "bridge"):
            pass
        assert current_cycle_id() == ""


# ─── 2. propose() stamps telemetry with the ambient cycle_id ───────────────


class TestProposeTelemetryCycleId:
    def test_propose_inside_context_stamps_its_cycle_id(self, monkeypatch, tmp_path):
        _patch_openai_client(monkeypatch, json.dumps({"task_title": "x"}))
        monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

        with call_context("cycle-t1", "proposer"):
            result = llm_proposer.propose("some context")

        assert result == {"task_title": "x"}
        rows = _read_jsonl(tmp_path / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl")
        assert rows[-1]["cycle_id"] == "cycle-t1"
        assert rows[-1]["component"] == "proposer"

    def test_propose_with_no_ambient_context_stamps_empty_string(self, monkeypatch, tmp_path):
        """Old form still valid: a direct ``propose()`` call outside any
        context (e.g. the prompt-preference test in test_llm_proposer.py)
        must not fabricate a cycle_id -- it stays ``""``."""
        _patch_openai_client(monkeypatch, json.dumps({"task_title": "x"}))
        monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

        assert current_cycle_id() == ""
        result = llm_proposer.propose("some context")

        assert result == {"task_title": "x"}
        rows = _read_jsonl(tmp_path / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl")
        assert rows[-1]["cycle_id"] == ""
        assert rows[-1]["component"] == "proposer"


# ─── 3. end-to-end join via maybe_propose (+ #9 regression pin) ────────────


class TestEndToEndJoin:
    def test_maybe_propose_join(self, monkeypatch, tmp_path):
        """Regression pin (fails on ``main``): before #1374, ``write_request``
        always minted its OWN fresh ``cycle_id`` (``f"cycle-{uuid...}"``)
        independent of any ambient context, and ``propose`` called
        ``call_context(None, "proposer")`` which erased the caller's
        context to ``""``. So on ``main``, assertion (c) below --
        the fake ``propose``'s captured ``current_cycle_id()`` equals the
        request/ledger cycle_id -- fails: the fake would have captured
        ``""`` while the request/ledger row carry a DIFFERENT, independently
        minted id. This is exactly the join the issue is about: a paid call
        could not be traced back to the attempt it produced.
        """
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        captured_cycle_ids: list[str] = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            from nanobot.observability.llm_telemetry import current_cycle_id as _ccid
            captured_cycle_ids.append(_ccid())
            return {
                "task_title": "Add a smoke test for the loop metrics report",
                "rationale": "Closes a coverage gap surfaced by the ledger digest.",
                "target_path": "tests/test_loop_metrics_extra.py",
                "serves": "priority 1",
            }

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)

        result = llm_proposer.maybe_propose(state_dir, None)
        assert result is not None

        # (a) exactly one 'proposed' row with a non-empty cycle_id of the
        # expected shape.
        proposed_rows = [r for r in _ledger_rows(state_dir) if r.get("phase") == "proposed"]
        assert len(proposed_rows) == 1
        cycle_id = proposed_rows[0].get("cycle_id", "")
        assert CYCLE_ID_RE.match(cycle_id), cycle_id

        # (b) the request JSON carries the same cycle_id.
        req_files = list((state_dir / "subagents" / "requests").glob("request-*.json"))
        assert len(req_files) == 1
        req_data = json.loads(req_files[0].read_text(encoding="utf-8"))
        assert req_data["cycle_id"] == cycle_id

        # (c) the fake propose captured current_cycle_id() at call time and
        # it equals the same id -- this is the telemetry key a real
        # propose() would have stamped on its record_llm_call row.
        assert captured_cycle_ids == [cycle_id]

        # Ambient context is reset once maybe_propose returns.
        assert current_cycle_id() == ""


# ─── 4. skip path join ──────────────────────────────────────────────────────


class TestSkipPathJoin:
    def test_noop_skip_row_carries_the_attempt_cycle_id(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        captured_cycle_ids: list[str] = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            from nanobot.observability.llm_telemetry import current_cycle_id as _ccid
            captured_cycle_ids.append(_ccid())
            # _is_noop_reply's accepted shape (llm_proposer.py): a dict with
            # a truthy 'no_valuable_task'.
            return {"no_valuable_task": True, "reason": "nothing worth doing this cycle"}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)

        result = llm_proposer.maybe_propose(state_dir, None)
        assert result is None

        skip_rows = [r for r in _ledger_rows(state_dir) if r.get("phase") == "proposer_skip"]
        assert len(skip_rows) == 1
        cycle_id = skip_rows[0].get("cycle_id", "")
        assert cycle_id
        assert captured_cycle_ids == [cycle_id]


# ─── 5. reject path join ────────────────────────────────────────────────────


class TestRejectPathJoin:
    def test_sizing_rejected_row_carries_the_attempt_cycle_id(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        captured_cycle_ids: list[str] = []

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            from nanobot.observability.llm_telemetry import current_cycle_id as _ccid
            captured_cycle_ids.append(_ccid())
            # validate_sizing fails: task_title is empty.
            return {"task_title": "", "rationale": "x", "target_path": "tests/test_x.py"}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)

        result = llm_proposer.maybe_propose(state_dir, None)
        assert result is None

        reject_rows = [r for r in _ledger_rows(state_dir) if r.get("phase") == "proposer_reject"]
        assert len(reject_rows) == 1
        assert reject_rows[0]["reason"] == "sizing_rejected"
        cycle_id = reject_rows[0].get("cycle_id", "")
        assert cycle_id
        # every retry inside the one attempt shares the same ambient id.
        assert captured_cycle_ids
        assert set(captured_cycle_ids) == {cycle_id}

    def test_propose_raising_still_carries_cycle_id(self, monkeypatch, tmp_path):
        """A raising ``propose`` still produces a joinable reject row.

        Discrepancy vs. the brief for this suite: the brief described this
        as ``reason == "error"``. In the ACTUAL control flow every call to
        ``propose()`` inside ``maybe_propose`` goes through a local
        ``_call_propose`` wrapper that catches ``Exception`` itself and
        converts it to ``_last_propose_failure`` -- so the raise never
        reaches ``maybe_propose``'s own outer ``except Exception as exc: ...
        reason "error"`` safety net. The pre-existing
        ``TestSelfDedup...test_catch_all_error_records_reject`` in
        tests/test_llm_proposer.py already pins the true reason for a
        raising ``propose``: ``"llm_unavailable"``. This test asserts that
        TRUE behavior and additionally proves the #1374 join still holds
        for it: the reject row still carries the attempt's cycle_id, equal
        to what the raising ``propose`` observed via ``current_cycle_id()``
        before raising. See ``test_genuine_error_path_still_carries_cycle_id``
        below for a real ``reason == "error"`` case (triggered by
        ``build_context`` raising, which is NOT propose-wrapped).
        """
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        captured_cycle_ids: list[str] = []

        def _raising_propose(context, *, rejection_reason=None, timeout=120.0):
            from nanobot.observability.llm_telemetry import current_cycle_id as _ccid
            captured_cycle_ids.append(_ccid())
            raise RuntimeError("proposer exploded")

        monkeypatch.setattr(llm_proposer, "propose", _raising_propose)

        result = llm_proposer.maybe_propose(state_dir, None)
        assert result is None

        reject_rows = [r for r in _ledger_rows(state_dir) if r.get("phase") == "proposer_reject"]
        assert len(reject_rows) == 1
        assert reject_rows[0]["reason"] == "llm_unavailable"
        cycle_id = reject_rows[0].get("cycle_id", "")
        assert cycle_id
        assert set(captured_cycle_ids) == {cycle_id}

    def test_genuine_error_path_still_carries_cycle_id(self, monkeypatch, tmp_path):
        """A real ``reason == "error"`` row: raise from ``build_context``,
        the one call inside ``maybe_propose``'s try body that is NOT
        wrapped by the local ``_call_propose`` helper, so the exception
        escapes to ``maybe_propose``'s own catch-all
        ``except Exception as exc: _record_proposer_reject(state_dir,
        "error", ...)``. The ambient cycle_id (minted and set before the
        try body even starts) is still present on this row, and the
        ``finally: reset_call_context(...)`` still fires afterwards."""
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _boom(*args, **kwargs):
            raise RuntimeError("context build exploded")

        monkeypatch.setattr(llm_proposer, "build_context", _boom)

        result = llm_proposer.maybe_propose(state_dir, None)
        assert result is None

        reject_rows = [r for r in _ledger_rows(state_dir) if r.get("phase") == "proposer_reject"]
        assert len(reject_rows) == 1
        assert reject_rows[0]["reason"] == "error"
        cycle_id = reject_rows[0].get("cycle_id", "")
        assert CYCLE_ID_RE.match(cycle_id), cycle_id
        assert current_cycle_id() == ""


# ─── 6. two attempts mint two different cycle ids ──────────────────────────


class TestTwoAttemptsDoNotShareAKey:
    def test_two_consecutive_maybe_propose_calls_mint_different_ids(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_VAR, "1")
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, "no priority section, so should_propose is True")

        def _fake_propose(context, *, rejection_reason=None, timeout=120.0):
            return {"no_valuable_task": True, "reason": "skip"}

        monkeypatch.setattr(llm_proposer, "propose", _fake_propose)

        assert llm_proposer.maybe_propose(state_dir, None) is None
        assert llm_proposer.maybe_propose(state_dir, None) is None

        skip_rows = [r for r in _ledger_rows(state_dir) if r.get("phase") == "proposer_skip"]
        assert len(skip_rows) == 2
        ids = {r.get("cycle_id", "") for r in skip_rows}
        assert len(ids) == 2
        assert "" not in ids


# ─── 7. write_request direct-call behaviour ────────────────────────────────


class TestWriteRequestDirectCall:
    """#1374 contract update (post-review): ``write_request`` no longer reads
    the ambient context at all -- it takes an explicit keyword-only
    ``cycle_id`` (``maybe_propose`` passes its own minted id explicitly, see
    the docstring at the call site: "Explicit, not ambient: inside the
    bridge process the ambient context is the EXECUTING cycle, and reusing
    that id here would collide with the running cycle's files and rows.").
    A direct caller with no ``cycle_id=`` still gets a freshly minted one,
    regardless of any ambient ``call_context`` -- including one that LOOKS
    like it should apply.
    """

    def test_no_cycle_id_kwarg_mints_a_fresh_id(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Direct call task",
            "rationale": "test",
            "target_path": "tests/test_direct.py",
            "serves": "priority 1",
        }
        path = llm_proposer.write_request(state_dir, proposal, None)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert CYCLE_ID_RE.match(data["cycle_id"]), data["cycle_id"]

    def test_explicit_cycle_id_kwarg_is_used_verbatim(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Fixed id task",
            "rationale": "test",
            "target_path": "tests/test_fixed.py",
            "serves": "priority 1",
        }
        path = llm_proposer.write_request(state_dir, proposal, None, cycle_id="cycle-fixed")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["cycle_id"] == "cycle-fixed"

    def test_ambient_context_is_never_consulted(self, tmp_path):
        """Even inside an ambient ``call_context``, a direct caller that
        does not pass ``cycle_id=`` must NOT pick up the ambient id -- it
        still mints its own fresh one. This is the regression the explicit-
        parameter redesign guards against: a call made from inside the
        bridge's own executing-cycle context must never silently borrow
        that (unrelated) cycle_id."""
        state_dir = _state_dir(tmp_path)
        proposal = {
            "task_title": "Ambient should not leak",
            "rationale": "test",
            "target_path": "tests/test_ambient.py",
            "serves": "priority 1",
        }
        with call_context("cycle-ambient", "bridge"):
            path = llm_proposer.write_request(state_dir, proposal, None)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["cycle_id"] != "cycle-ambient"
        assert CYCLE_ID_RE.match(data["cycle_id"]), data["cycle_id"]


# ─── 8. old-form (pre-#1374) tolerance ──────────────────────────────────────


class TestOldFormTolerance:
    def test_ledger_reader_helpers_tolerate_rows_without_cycle_id(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        # Pre-#1374 shape: proposer_skip/proposer_reject with NO cycle_id key
        # at all, plus a proposed row that DOES carry one (#1374's own new
        # shape can coexist with old rows in the same ledger).
        cycle_ledger.append_event(state_dir, {"phase": "proposer_skip", "reason": "nothing valuable"})
        cycle_ledger.append_event(state_dir, {"phase": "proposer_reject", "reason": "self_dedup"})
        cycle_ledger.append_event(
            state_dir,
            {
                "phase": "proposed",
                "cycle_id": "cycle-abc123def456",
                "task_title": "x",
                "target_path": "y",
            },
        )

        streak = llm_proposer._consecutive_noop_streak(state_dir)
        assert isinstance(streak, int) and streak >= 0

        dedup_rejects = llm_proposer._consecutive_self_dedup_rejects(state_dir)
        assert isinstance(dedup_rejects, int) and dedup_rejects >= 0

        cooled, status = llm_proposer._recent_duplicate_failure_cooling(state_dir)
        assert isinstance(cooled, set)
        assert isinstance(status, str)

    def test_llm_calls_report_counts_empty_cycle_id_rows_without_assigning_a_cycle(self, tmp_path):
        report = _load_report_module()
        calls_dir = tmp_path / "llm_calls"
        calls_dir.mkdir(parents=True)
        (calls_dir / "2026-09-01.jsonl").write_text(
            "\n".join(
                json.dumps(rec)
                for rec in [
                    # Pre-#1374 shape: cycle_id present but empty.
                    {"model": "m1", "duration_ms": 10.0, "total_tokens": 5, "cycle_id": ""},
                    {"model": "m1", "duration_ms": 20.0, "total_tokens": 7, "cycle_id": "cycle-real000001"},
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        records = report.load_records(calls_dir)
        assert len(records) == 2

        summary = report.aggregate(records)
        # Both rows are counted in the totals/per-model buckets...
        assert summary["totals"]["calls"] == 2
        assert summary["per_model"]["m1"]["count"] == 2
        # ...but the empty-cycle_id row is never assigned a per-cycle bucket
        # (aggregate()'s ``if cycle_id:`` guard) -- it must not crash, and it
        # must not be attributed to any cycle.
        assert "" not in summary["per_cycle_duration_ms"]
        assert summary["per_cycle_duration_ms"] == {"cycle-real000001": 20.0}


# ─── contract update (post-review): current_cycle_id(component=...) filter ──
# ─── and the two ledger writers that must respect it                      ──


class TestComponentFilteredCurrentCycleId:
    def test_mismatched_component_reads_as_empty(self):
        with call_context("cycle-p", "proposer"):
            assert current_cycle_id("bridge") == ""
            assert current_cycle_id("proposer") == "cycle-p"
            assert current_cycle_id() == "cycle-p"

    def test_bridge_context_does_not_leak_into_proposer_writers(self, tmp_path):
        """A ledger writer that asks specifically for the PROPOSER's
        attempt id (``current_cycle_id("proposer")``) must not pick up an
        unrelated ambient ``"bridge"`` context -- such a row must carry NO
        ``cycle_id`` key at all (never a wrong one)."""
        state_dir = _state_dir(tmp_path)
        with call_context("cycle-exec", "bridge"):
            llm_proposer._record_noop_skip(state_dir, "x")
            llm_proposer._record_proposer_reject(state_dir, "sizing_rejected")

        rows = _ledger_rows(state_dir)
        skip_row = next(r for r in rows if r.get("phase") == "proposer_skip")
        reject_row = next(r for r in rows if r.get("phase") == "proposer_reject")
        assert "cycle_id" not in skip_row
        assert "cycle_id" not in reject_row

    def test_proposer_context_reaches_the_same_writers(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        with call_context("cycle-p", "proposer"):
            llm_proposer._record_noop_skip(state_dir, "x")
            llm_proposer._record_proposer_reject(state_dir, "sizing_rejected")

        rows = _ledger_rows(state_dir)
        skip_row = next(r for r in rows if r.get("phase") == "proposer_skip")
        reject_row = next(r for r in rows if r.get("phase") == "proposer_reject")
        assert skip_row.get("cycle_id") == "cycle-p"
        assert reject_row.get("cycle_id") == "cycle-p"


# ─── reader: scripts/loop_metrics_report.py::group_by_cycle ─────────────────


class TestGroupByCycleIgnoresAttemptOnlyPhases:
    def test_skip_reject_and_cooling_rows_open_no_bucket(self):
        mod = _load_loop_metrics_module()
        rows = [
            {"phase": "proposer_skip", "cycle_id": "cycle-s", "ts": "2026-09-01T00:00:00Z"},
            {"phase": "proposer_reject", "cycle_id": "cycle-r", "ts": "2026-09-01T00:00:01Z"},
            {"phase": "demand_cooling", "cycle_id": "cycle-c", "ts": "2026-09-01T00:00:02Z"},
            {"phase": "started", "cycle_id": "cycle-x", "ts": "2026-09-01T00:00:03Z"},
            {"phase": "outcome", "cycle_id": "cycle-x", "outcome": "success", "ts": "2026-09-01T00:00:04Z"},
            {"phase": "proposed", "cycle_id": "cycle-p2", "ts": "2026-09-01T00:00:05Z"},
        ]
        cycles = mod.group_by_cycle(rows)
        assert set(cycles.keys()) == {"cycle-x", "cycle-p2"}


# ─── reader: nanobot/runtime/action_index.py's build_action_index ──────────


class TestActionIndexExcludesProposerRecords:
    def test_proposer_prompt_record_with_higher_seq_does_not_win(self, tmp_path):
        """A proposer prompt record for the same cycle_id as an executor
        (bridge) record, with a HIGHER seq, must not win the cycle's action
        set -- proposer records hold no tool calls of the executor's own and
        their ``seq`` restarts in a different process (#1374)."""
        from nanobot.runtime.action_index import build_action_index

        cycle = "cycle-mix1"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        (tmp_path / "ledger").mkdir(parents=True)
        (tmp_path / "ledger" / "cycles.jsonl").write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {"phase": "proposed", "cycle_id": cycle, "task_title": "Mixed cycle"},
                    {"phase": "outcome", "cycle_id": cycle, "outcome": "success", "ts": f"{today}T01:00:00Z"},
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        # A real workspace-relative target so the detail pass resolves it
        # (mirrors tests/test_action_index.py's fixture convention).
        target = tmp_path / "scripts" / "bridge_target.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")

        prompts_dir = tmp_path / "llm_calls" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / f"{today}.jsonl").write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {
                        "cycle_id": cycle,
                        "component": "bridge",
                        "seq": 1,
                        "messages": [{"role": "assistant", "tool_calls": [
                            {"function": {"name": "edit_file", "arguments": {"path": "scripts/bridge_target.py"}}},
                        ]}],
                    },
                    {
                        "cycle_id": cycle,
                        "component": "proposer",
                        "seq": 2,
                        "messages": [{"role": "assistant", "tool_calls": [
                            {"function": {"name": "edit_file", "arguments": {"path": "scripts/should_not_win.py"}}},
                        ]}],
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        summary = build_action_index(tmp_path, prompts_dir)
        assert summary["cycles"] == 1

        index_path = tmp_path / "action_index" / f"{today}.jsonl"
        rows = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(rows) == 1
        assert rows[0]["cycle_id"] == cycle
        assert rows[0]["actions"]
        assert rows[0]["actions"] == ["edit:scripts/*.py"]
        assert rows[0]["actions_detail"] == ["edit:scripts/bridge_target.py"]


# ─── llm_telemetry._PROMPT_SEQ keyed by (cycle_id, component) ──────────────


class TestPromptSeqKeyedByComponent:
    def test_independent_sequences_per_component(self, monkeypatch, tmp_path):
        from nanobot.observability.llm_telemetry import record_llm_prompt

        monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
        monkeypatch.delenv("LLM_CAPTURE_PROMPTS", raising=False)  # default ON

        with call_context("cycle-q", "proposer"):
            record_llm_prompt(
                messages=[{"role": "user", "content": "proposer prompt"}],
                content="proposer reply", reasoning_content=None,
                finish_reason="stop", model="m",
                prompt_tokens=1, completion_tokens=1,
            )
        with call_context("cycle-q", "bridge"):
            record_llm_prompt(
                messages=[{"role": "user", "content": "bridge prompt"}],
                content="bridge reply", reasoning_content=None,
                finish_reason="stop", model="m",
                prompt_tokens=1, completion_tokens=1,
            )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = _read_jsonl(tmp_path / "prompts" / f"{today}.jsonl")
        proposer_rows = [r for r in rows if r["cycle_id"] == "cycle-q" and r["component"] == "proposer"]
        bridge_rows = [r for r in rows if r["cycle_id"] == "cycle-q" and r["component"] == "bridge"]
        assert len(proposer_rows) == 1 and proposer_rows[0]["seq"] == 1
        assert len(bridge_rows) == 1 and bridge_rows[0]["seq"] == 1
