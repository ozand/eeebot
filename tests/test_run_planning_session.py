"""ADR-031 rule 5, ADR-032 rule 2 (#1852): the planning session itself --
:func:`bridge._run_planning_session`.

``SubagentManager`` is faked here (not the real agent loop -- no LLM call
reachable in this suite): a fake spawn() writes the telemetry JSON the real
one would produce and completes instantly. This proves the session's own
contract -- parses its final response, writes the plan to the diary,
journals the outcome, degrades on failure, and defends the workspace
against whatever its tools touched -- not the underlying agent loop, which
is exercised elsewhere.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.runtime import bridge
from nanobot.runtime.day_diary import PLAN_BEGIN, PLAN_END, diary_relpath


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _init_repo_with_origin(tmp_path: Path) -> Path:
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True, check=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _ledger_rows(state: Path, phase: str) -> list[dict]:
    path = state / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("phase") == phase]


def _fake_config() -> SimpleNamespace:
    return SimpleNamespace(
        tools=SimpleNamespace(
            web=SimpleNamespace(search=None, proxy=None),
            exec=None,
            subagent=SimpleNamespace(max_running=1),
        ),
    )


def _make_fake_mgr_factory(
    state_dir: Path, result_obj: object, *, dirty_repo: bool = False, raise_on_init: bool = False,
    tamper_sidecar_rel: "str | None" = None,
):
    """A factory returning a fake SubagentManager class bound to one test's
    expectations -- writes the telemetry file real SubagentManager.spawn's
    background task would produce, iterations included."""

    class _FakeSubagentManager:
        def __init__(self, **kwargs):
            if raise_on_init:
                raise RuntimeError("provider unavailable")
            self.workspace = Path(kwargs["workspace"])
            self.role_system_prompt = kwargs.get("role_system_prompt")
            self.max_iterations = kwargs.get("max_iterations")
            self._telemetry_component = kwargs.get("telemetry_component", "")
            self._running_tasks: dict[str, asyncio.Task] = {}

        async def spawn(self, *, task, label, origin_channel, origin_chat_id):
            task_id = "plannerfake1"
            if dirty_repo:
                (self.workspace / "stray.txt").write_text("oops\n", encoding="utf-8")
            if tamper_sidecar_rel:
                sidecar_path = state_dir / tamper_sidecar_rel
                sidecar_path.parent.mkdir(parents=True, exist_ok=True)
                sidecar_path.write_text('{"tampered": true}', encoding="utf-8")

            async def _finish():
                telem_dir = state_dir / "subagents"
                telem_dir.mkdir(parents=True, exist_ok=True)
                payload = {
                    "subagent_id": task_id,
                    "result": result_obj if isinstance(result_obj, str) else json.dumps(result_obj),
                    "context_usage": {"peak_tokens": 100, "iterations": [10, 20, 30]},
                }
                (telem_dir / f"{task_id}.json").write_text(json.dumps(payload), encoding="utf-8")

            bg = asyncio.create_task(_finish())
            self._running_tasks[task_id] = bg
            return f"Subagent [{label}] started (id: {task_id})."

    return _FakeSubagentManager


@pytest.fixture(autouse=True)
def _stub_role_prompt(monkeypatch):
    """Role file assembly is exercised in ``role_prompt``'s own tests --
    here it only needs to succeed without touching a real release tree.
    ``_run_planning_session`` does ``from nanobot.runtime import
    role_prompt`` locally, which binds to this same cached module object,
    so patching the attribute here reaches it."""
    import nanobot.runtime.role_prompt as role_prompt_mod

    monkeypatch.setattr(
        role_prompt_mod, "build_role_system_prompt",
        lambda role, **kwargs: ("fixed planner prompt", {"role": role}),
    )


async def _run(**overrides):
    kwargs = dict(
        provider=SimpleNamespace(), bus=SimpleNamespace(), config=_fake_config(),
        model="an/test-model", cycle_id="cycle-1",
    )
    kwargs.update(overrides)
    return await bridge._run_planning_session(**kwargs)


def test_happy_path_writes_the_diary_and_journals_success(tmp_path: Path, monkeypatch):
    repo = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    result_obj = {
        "insight": "three cycles retried the same excluded-file fix",
        "plan": "connect the orphaned validator to the pre-commit hook",
        "iterations_planned": 35,
        "hypotheses": ["a fixed pre-commit hook stops the repeat"],
        "futility_advisories": [],
    }
    monkeypatch.setattr(bridge, "SubagentManager", _make_fake_mgr_factory(state, result_obj))

    outcome = asyncio.run(_run(state_dir=state, selfevo_repo=repo, denied_paths=set()))

    assert outcome == {"ran": True, "iterations_used": 3, "iterations_planned": 35, "tampered_files": []}

    _git(repo, "fetch", "origin", "main")
    pushed = subprocess.run(
        ["git", "-C", str(repo), "show", f"origin/main:{diary_relpath()}"], capture_output=True, text=True,
    ).stdout
    assert "connect the orphaned validator to the pre-commit hook" in pushed
    assert "three cycles retried the same excluded-file fix" in pushed
    assert "a fixed pre-commit hook stops the repeat" in pushed
    assert pushed.count(PLAN_BEGIN) == 1 and pushed.count(PLAN_END) == 1

    rows = _ledger_rows(state, "planning_session")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "integrated"
    assert rows[0]["iterations_used"] == 3
    assert rows[0]["iterations_planned"] == 35


def test_a_fitness_sidecar_write_during_planning_spawn_is_detected_and_refused(tmp_path: Path, monkeypatch):
    """The planner's tools reach the same FITNESS_SIDECARS paths the
    executor's do (exec is not fenced by denied_paths) -- its own spawn
    window needs its own pre/post-hash bracket, or a tamper here would be
    invisible to the executor's LATER bracket (both sides of that
    comparison would already reflect it, since this window closes first).
    """
    repo = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    result_obj = {"insight": "x", "plan": "y", "iterations_planned": 10}
    monkeypatch.setattr(
        bridge, "SubagentManager",
        _make_fake_mgr_factory(state, result_obj, tamper_sidecar_rel="demand/completed.json"),
    )

    outcome = asyncio.run(_run(state_dir=state, selfevo_repo=repo, denied_paths=set()))

    assert outcome["ran"] is False
    assert outcome["tampered_files"] == ["demand/completed.json"]
    # the plan must not reach the diary from a session whose spawn window
    # tampered with a fitness sidecar
    assert not (repo / diary_relpath()).exists()

    integrity_rows = [r for r in _ledger_rows(state, "integrity")]
    assert len(integrity_rows) == 1
    assert integrity_rows[0]["reason"] == "sidecar_write_during_spawn"
    assert integrity_rows[0]["files"] == ["demand/completed.json"]
    assert integrity_rows[0]["component"] == "planner"

    rows = _ledger_rows(state, "planning_session")
    assert rows[0]["outcome"] == "spawn_failed"


def test_malformed_final_response_degrades_without_touching_the_diary(tmp_path: Path, monkeypatch):
    repo = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    before = (repo / diary_relpath()).exists()
    monkeypatch.setattr(bridge, "SubagentManager", _make_fake_mgr_factory(state, "not json at all"))

    outcome = asyncio.run(_run(state_dir=state, selfevo_repo=repo, denied_paths=set()))

    assert outcome["ran"] is True
    assert outcome["iterations_planned"] is None
    assert (repo / diary_relpath()).exists() == before  # diary untouched

    rows = _ledger_rows(state, "planning_session")
    assert rows[0]["outcome"] == "malformed"
    assert rows[0]["iterations_planned"] is None


def test_spawn_failure_degrades_to_the_ranked_queue(tmp_path: Path, monkeypatch):
    """AC: a failed planning session degrades to the ranked queue (the
    caller proceeds unaffected) and says so in the journal."""
    repo = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setattr(bridge, "SubagentManager", _make_fake_mgr_factory(state, {}, raise_on_init=True))

    outcome = asyncio.run(_run(state_dir=state, selfevo_repo=repo, denied_paths=set()))

    assert outcome == {"ran": False, "iterations_used": None, "iterations_planned": None, "tampered_files": []}
    rows = _ledger_rows(state, "planning_session")
    assert rows[0]["outcome"] == "spawn_failed"


def test_a_dirty_workspace_after_the_session_is_hard_reset(tmp_path: Path, monkeypatch):
    """Safety net: whatever the session's own tools touched must not
    survive -- only this function's own diary write may change main."""
    repo = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    pre_sha = _git(repo, "rev-parse", "HEAD")
    result_obj = {"insight": "x", "plan": "y", "iterations_planned": 10}
    monkeypatch.setattr(bridge, "SubagentManager", _make_fake_mgr_factory(state, result_obj, dirty_repo=True))

    outcome = asyncio.run(_run(state_dir=state, selfevo_repo=repo, denied_paths=set()))

    assert not (repo / "stray.txt").exists()
    assert outcome["ran"] is True
    # the diary write still lands cleanly after the reset
    _git(repo, "fetch", "origin", "main")
    pushed = subprocess.run(
        ["git", "-C", str(repo), "show", f"origin/main:{diary_relpath()}"], capture_output=True, text=True,
    ).stdout
    assert "y" in pushed
    assert _git(repo, "rev-parse", "HEAD") != pre_sha  # the plan commit landed after the reset
    assert _git(repo, "status", "--porcelain") == ""
