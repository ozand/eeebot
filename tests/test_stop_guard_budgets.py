"""Direct stop-guard budget-exhaustion tests for the bridge/repair loop (#709).

Follow-up from #703 (safety-shell freeze): invariant **S7** ("Stop-guard
time/iteration budgets" — ``docs/specs/subagent-bridge/spec.md``) was flagged
as the *weakest-covered* of the eight frozen invariants in
``docs/changes/703-safety-shell-invariants/test-coverage-map.md``: existing
coverage was indirect (structural kwargs-validity + smoke-gate-level repair
tests), and nothing directly drove either budget to exhaustion and asserted
the bounded, fail-safe outcome. This file closes that gap with direct tests:

1. The repair loop's revision cap (``SUBAGENT_BRIDGE_MAX_REVISIONS`` /
   ``stop_guards.REVISION_CAP_DEFAULT``) — exhausted against a real temp git
   repo, asserting an exact call count (no unbounded iteration) and a
   fail-safe (never-integrate) gate decision.
2. ``stop_guards.revision_outcome``'s cap-reached terminal state (the other
   terminal states already have direct unit coverage in
   ``tests/test_stop_guards.py``; this file only adds what that suite does
   not: the cap-reached path wired to a real exhausted loop).
3. ``SUBAGENT_BRIDGE_MAX_REVISIONS`` env-override parsing (valid/invalid),
   exercised against the REAL source text of ``bridge.py`` (not a hand-copied
   mirror that could silently drift).
4. Clean state after cap-reached: ``_restore_to_main`` puts the shared
   checkout back on ``main`` with ``origin/main`` untouched, and the cycle
   branch is preserved for forensics.
5. The subagent turn budget (``config.agents.defaults.max_tool_iterations``)
   actually bounds a REAL ``SubagentManager`` turn (as opposed to only being
   stored as an attribute — see
   ``test_subagent_manager_honors_configured_max_iterations`` in
   ``tests/test_subagent_manager.py``, which checks the attribute only).

No runtime/gate code is changed here.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime import bridge, stop_guards

_BRIDGE_PATH = Path(bridge.__file__)


# ─── git fixture helpers (same shape as tests/test_bridge_cycle_branch.py,
# tests/test_bridge_locking.py, tests/test_bounded_gate.py — this repo's
# established per-file duplication pattern for the bare-origin + working-clone
# harness rather than a shared import) ─────────────────────────────────────────


def _git(repo: Path) -> list[str]:
    return ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(_git(repo) + list(args), capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare 'origin' and a clone with one commit on main. Returns (origin, work)."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
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


def _commit_file(work: Path, name: str, content: str, message: str) -> None:
    (work / name).write_text(content)
    _run(work, "add", name)
    _run(work, "commit", "-m", message)


def _origin_main_sha(origin: Path) -> str:
    return _run(origin, "rev-parse", "main").stdout.strip()


def _local_main_sha(work: Path) -> str:
    return _run(work, "rev-parse", "main").stdout.strip()


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    """Same rationale as test_bridge_cycle_branch.py: the real core-smoke set
    names paths in THIS repo that don't exist in the synthetic origin/work
    repos built here — point it at the one file the fixture actually creates.
    """
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


# ─── 1/2/4: repair-loop revision-cap exhaustion (mirrors main()'s repair
# while-loop shape, bridge.py ~1633-1705: "while not smoke_passed and
# repairs < cap: repairs += 1; ...; re-run smoke") ─────────────────────────────


class TestRevisionCapExhaustion:
    def test_repair_loop_stops_exactly_at_cap_and_never_integrates(self, tmp_path, monkeypatch):
        """No repair subagent is spawned here (this test targets the STOP
        mechanism, not the repair itself) — the suite is left broken every
        retry, so the loop must exhaust the cap, record an exact attempt
        count, and never integrate.
        """
        origin, work = _init_repo(tmp_path)
        main_sha_before = _origin_main_sha(origin)
        setup = bridge._setup_cycle_branch(work, "budget-exhaust")
        assert setup["ok"]
        # Break the suite on the cycle branch — stays broken every retry
        # (no repair is applied), so every gate re-run observes the same FAIL.
        _commit_file(
            work, "tests/test_smoke.py",
            "def test_ok():\n    assert False\n",
            "feat: breaking change",
        )
        baseline = bridge._count_tests_at_ref(work, "origin/main")

        real_run_smoke = bridge._run_smoke_tests
        calls = {"n": 0}

        def _counting_run_smoke(repo_root, changed_files=None, timeout=300):
            calls["n"] += 1
            return real_run_smoke(repo_root, changed_files=changed_files, timeout=timeout)

        monkeypatch.setattr(bridge, "_run_smoke_tests", _counting_run_smoke)

        cap = 3
        repairs = 0
        smoke_passed, smoke_output = bridge._run_smoke_tests_with_shrink_guard(
            work, baseline, changed_files=["tests/test_smoke.py"],
        )
        while not smoke_passed and repairs < cap:
            repairs += 1
            # (a real repair turn would spawn a subagent + commit here; the
            # STOP mechanism under test doesn't depend on what a repair turn
            # does — only on the loop honoring the cap.)
            smoke_passed, smoke_output = bridge._run_smoke_tests_with_shrink_guard(
                work, baseline, changed_files=["tests/test_smoke.py"],
            )

        # Loop stopped exactly at the cap — not one attempt short, not
        # one over (no unbounded iteration).
        assert repairs == cap
        assert smoke_passed is False
        assert calls["n"] == cap + 1  # 1 initial run + 1 per repair attempt

        record = stop_guards.revision_outcome(
            revisions=repairs, smoke_passed=smoke_passed, cap=cap,
            last_smoke_output=smoke_output,
        )
        assert record["outcome"] == "blocked"
        assert record["capped"] is True
        assert record["count"] == repairs  # the recorded `revisions` field (#709 ask)
        assert record["max"] == cap

        # Fail-safe: never integrate on red. Mirrors the `else: _rollback_reason
        # = 'gate_failed'` branch in main() — _restore_to_main puts the shared
        # checkout back on main, origin/main is untouched, and the cycle branch
        # survives for forensics.
        restored = bridge._restore_to_main(work)
        assert restored is True
        assert _origin_main_sha(origin) == main_sha_before
        assert _local_main_sha(work) == main_sha_before
        current_branch = _run(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert current_branch == "main"
        branches = _run(work, "branch", "--list", setup["branch"]).stdout
        assert setup["branch"] in branches
        log = _run(work, "log", setup["branch"], "--oneline").stdout
        assert "breaking change" in log

    def test_repair_loop_stops_short_of_cap_once_smoke_passes(self, tmp_path, monkeypatch):
        """Sanity counterpart: if a repair turn actually fixes the suite before
        the cap is reached, the loop must stop immediately (not burn the
        remaining budget) and record `capped=False`.
        """
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "budget-recovers")
        assert setup["ok"]
        _commit_file(
            work, "tests/test_smoke.py",
            "def test_ok():\n    assert False\n",
            "feat: breaking change",
        )
        baseline = bridge._count_tests_at_ref(work, "origin/main")

        cap = 3
        repairs = 0
        smoke_passed, smoke_output = bridge._run_smoke_tests_with_shrink_guard(
            work, baseline, changed_files=["tests/test_smoke.py"],
        )
        while not smoke_passed and repairs < cap:
            repairs += 1
            if repairs == 1:
                # Simulate a repair turn that actually fixes the suite on its
                # first attempt.
                _commit_file(
                    work, "tests/test_smoke.py",
                    "def test_ok():\n    assert True\n",
                    "fix: repair turn 1",
                )
            smoke_passed, smoke_output = bridge._run_smoke_tests_with_shrink_guard(
                work, baseline, changed_files=["tests/test_smoke.py"],
            )

        assert repairs == 1
        assert smoke_passed is True

        record = stop_guards.revision_outcome(
            revisions=repairs, smoke_passed=smoke_passed, cap=cap,
        )
        assert record["outcome"] == "passed"
        assert record["capped"] is False


# ─── 3: SUBAGENT_BRIDGE_MAX_REVISIONS env-override parsing ────────────────────
#
# This block is inline in main() (not a standalone function), so — same
# technique as tests/test_repair_loop.py's _extract_fn — pull the exact
# source text out of bridge.py by anchor and exec it, rather than hand-copying
# the parsing logic into the test (which could silently drift from the real
# code).


def _extract_max_repair_attempts_snippet() -> str:
    source = _BRIDGE_PATH.read_text()
    start_anchor = "    try:\n        _max_repair_attempts = int("
    end_anchor = "_max_repair_attempts = max(0, _max_repair_attempts)"
    start = source.index(start_anchor)
    end = source.index(end_anchor, start) + len(end_anchor)
    snippet = source[start:end]
    assert "SUBAGENT_BRIDGE_MAX_REVISIONS" in snippet
    assert "REVISION_CAP_DEFAULT" in snippet
    return textwrap.dedent(snippet)


class _FakeOs:
    """Stand-in for the `os` module used by the extracted snippet — lets the
    test control `os.environ.get(...)` without mutating real process env."""

    def __init__(self, environ: dict[str, str]):
        self.environ = environ


def _run_max_repair_attempts_snippet(env: dict[str, str]) -> int:
    from nanobot.runtime.stop_guards import REVISION_CAP_DEFAULT

    ns: dict = {"os": _FakeOs(env), "REVISION_CAP_DEFAULT": REVISION_CAP_DEFAULT}
    exec(_extract_max_repair_attempts_snippet(), ns)
    return ns["_max_repair_attempts"]


class TestMaxRevisionsEnvOverride:
    def test_unset_env_falls_back_to_default(self):
        assert _run_max_repair_attempts_snippet({}) == stop_guards.REVISION_CAP_DEFAULT

    def test_valid_override_is_honored(self):
        assert _run_max_repair_attempts_snippet({"SUBAGENT_BRIDGE_MAX_REVISIONS": "7"}) == 7

    def test_invalid_value_falls_back_to_default_not_a_crash(self):
        assert (
            _run_max_repair_attempts_snippet({"SUBAGENT_BRIDGE_MAX_REVISIONS": "not-a-number"})
            == stop_guards.REVISION_CAP_DEFAULT
        )

    def test_negative_override_is_clamped_to_zero(self):
        """The snippet's own `max(0, ...)` clamp — a cap of 0 means "fail on
        the first smoke run, no repair turns at all" rather than a negative
        (nonsensical) cap.
        """
        assert _run_max_repair_attempts_snippet({"SUBAGENT_BRIDGE_MAX_REVISIONS": "-5"}) == 0

    def test_zero_cap_means_no_repair_turns(self, tmp_path, monkeypatch):
        """End-to-end tie-in: cap=0 (e.g. from the clamp above) means the
        repair while-loop body never executes even once.
        """
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "zero-cap")
        assert setup["ok"]
        _commit_file(
            work, "tests/test_smoke.py",
            "def test_ok():\n    assert False\n",
            "feat: breaking change",
        )
        baseline = bridge._count_tests_at_ref(work, "origin/main")

        cap = 0
        repairs = 0
        smoke_passed, _ = bridge._run_smoke_tests_with_shrink_guard(
            work, baseline, changed_files=["tests/test_smoke.py"],
        )
        while not smoke_passed and repairs < cap:
            repairs += 1

        assert repairs == 0
        record = stop_guards.revision_outcome(revisions=repairs, smoke_passed=smoke_passed, cap=cap)
        assert record["outcome"] == "blocked"
        assert record["capped"] is True
        assert record["count"] == 0


# ─── 5: the subagent turn budget (config.agents.defaults.max_tool_iterations)
# actually bounds a REAL SubagentManager turn ──────────────────────────────────


class _LoopingProvider(LLMProvider):
    """Always requests a tool call and never finishes on its own — the model
    the bridge's own turn-budget must be the thing that stops the loop.
    """

    def __init__(self):
        super().__init__()
        self.calls = 0

    async def chat(
        self, messages=None, tools=None, model=None, max_tokens=4096,
        temperature=0.7, reasoning_effort=None, tool_choice=None,
    ) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(id=f"call-{self.calls}", name="list_dir", arguments={"path": "."})],
        )

    def get_default_model(self) -> str:
        return "test-model"


class TestSubagentTurnIterationBudget:
    async def test_bridge_style_subagent_manager_bounds_a_turn(self, tmp_path):
        """Bridge constructs SubagentManager with
        ``max_iterations=config.agents.defaults.max_tool_iterations`` for both
        the initial spawn (bridge.py ~1496) and every repair turn (~1665).
        ``test_subagent_manager_honors_configured_max_iterations`` (in
        tests/test_subagent_manager.py) only asserts the attribute is set;
        this drives a REAL turn against a provider that loops forever and
        asserts the cap actually stops execution at exactly N iterations.
        """
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus

        provider = _LoopingProvider()
        manager = SubagentManager(
            provider=provider, workspace=tmp_path, bus=MessageBus(), max_iterations=3,
        )

        await manager.spawn(task="loop forever", label="test-loop")
        assert manager._running_tasks
        import asyncio
        await asyncio.gather(*list(manager._running_tasks.values()), return_exceptions=True)

        # Exactly max_iterations turns taken — no unbounded iteration, no crash.
        assert provider.calls == 3
