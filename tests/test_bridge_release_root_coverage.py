"""Regression coverage for #966 release-root and prompt-dump contracts."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

from nanobot.runtime import bridge, llm_proposer
from nanobot.runtime.bridge import build_task
from tests.test_cycle_ledger import _init_selfevo_repo, _seed_bridge_request


CANONICAL_RELEASE_ROOT = "/opt/eeepc-agent/runtimes/self-evolving-agent/current"


def test_release_root_default_and_env_override(monkeypatch, tmp_path: Path):
    """The module default is canonical, while RELEASE_ROOT overrides it."""
    monkeypatch.delenv("RELEASE_ROOT", raising=False)
    importlib.reload(bridge)
    assert bridge.RELEASE_ROOT.as_posix() == CANONICAL_RELEASE_ROOT
    assert llm_proposer._release_root_from_env() == Path(CANONICAL_RELEASE_ROOT)

    override = tmp_path / "release"
    monkeypatch.setenv("RELEASE_ROOT", str(override))
    importlib.reload(bridge)
    assert bridge.RELEASE_ROOT == override
    assert llm_proposer._release_root_from_env() == override

    monkeypatch.delenv("RELEASE_ROOT", raising=False)
    importlib.reload(bridge)


def test_build_task_charter_pointer_only_when_in_system():
    req = {"task_title": "x", "request_id": "r", "cycle_id": "c", "goal_id": "g"}
    inline = build_task(req, "CHARTER_SENTINEL", "", charter_in_system=False)
    pointer = build_task(req, "CHARTER_SENTINEL", "", charter_in_system=True)

    assert "CHARTER_SENTINEL" in inline
    assert "see system context" not in inline
    assert "see system context" in pointer
    assert pointer.count("CHARTER_SENTINEL") == 1
    assert pointer.index("see system context") < pointer.index("CHARTER_SENTINEL")


class _ReleaseRootManager:
    instances: list["_ReleaseRootManager"] = []

    def __init__(self, *, workspace, system_context="", **_kwargs):
        self.workspace = workspace
        self.system_context = system_context
        self._running_tasks: dict = {}
        self._skill_reads_this_cycle: list[dict] = []
        self.__class__.instances.append(self)

    def _build_subagent_prompt(self) -> str:
        agents = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")
        return f"{agents}\n\n---\n\n{self.system_context}"

    async def spawn(self, **_kwargs):
        (self.workspace / "scripts").mkdir(exist_ok=True)
        (self.workspace / "scripts" / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")
        from tests.test_cycle_ledger import _run

        _run(self.workspace, "add", "scripts/feature.py")
        _run(self.workspace, "commit", "-m", "feat: add feature")
        return "fake subagent spawned"

    def collect_skill_reads(self) -> int:
        return 0


def test_spawn_reads_charter_and_identity_from_release_root_not_target(
    tmp_path: Path, monkeypatch
):
    base = tmp_path
    state_dir = base / "state"
    state_dir.mkdir()
    _origin, work = _init_selfevo_repo(base)
    (work / "AGENTS.md").write_text("INSTANCE AGENTS", encoding="utf-8")
    from tests.test_cycle_ledger import _run

    _run(work, "add", "AGENTS.md")
    _run(work, "commit", "-m", "add agents")
    _run(work, "push", "origin", "HEAD:main")

    release = base / "release"
    target = base / "target-workspace"
    release.mkdir()
    target.mkdir()
    (release / "goals.md").write_text("RELEASE CHARTER", encoding="utf-8")
    (release / "IDENTITY.md").write_text("RELEASE IDENTITY", encoding="utf-8")
    (target / "goals.md").write_text("WRONG TARGET CHARTER", encoding="utf-8")
    (target / "IDENTITY.md").write_text("WRONG TARGET IDENTITY", encoding="utf-8")

    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", target)
    monkeypatch.setattr(bridge, "RELEASE_ROOT", release)
    monkeypatch.setattr(bridge, "SubagentManager", _ReleaseRootManager)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))
    monkeypatch.setenv("SELFEVO_DUMP_PROMPTS", "1")
    _ReleaseRootManager.instances.clear()

    _seed_bridge_request(state_dir, "req-root", "cycle-root")
    assert asyncio.run(bridge._main_impl()) == 0

    manager = _ReleaseRootManager.instances[0]
    assert "RELEASE CHARTER" in manager.system_context
    assert "RELEASE IDENTITY" in manager.system_context
    assert "WRONG TARGET" not in manager.system_context
    assert manager.workspace == work
    dumps = list((state_dir / "prompts").glob("cycle-root.system.txt"))
    assert len(dumps) == 1


def test_faithful_dump_contains_workspace_and_system_context(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("AGENTS_SENTINEL", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("SELFEVO_DUMP_PROMPTS", "1")

    manager = _ReleaseRootManager(workspace=workspace, system_context=(
        "# Immutable operator charter\nCHARTER_SENTINEL\n\n"
        "# Loop agent identity\nIDENTITY_SENTINEL"
    ))
    bridge.dump_spawn_prompts(
        state_dir,
        "faithful",
        manager._build_subagent_prompt(),
        "task",
    )
    dumped = (state_dir / "prompts" / "faithful.system.txt").read_text(encoding="utf-8")
    assert "AGENTS_SENTINEL" in dumped
    assert "# Immutable operator charter" in dumped
    assert "# Loop agent identity" in dumped
