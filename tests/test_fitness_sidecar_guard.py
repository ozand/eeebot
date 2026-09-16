import pytest

from nanobot.agent.tools.filesystem import EditFileTool, WriteFileTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.runtime.scorecard import FITNESS_SIDECARS


@pytest.mark.asyncio
async def test_write_file_blocks_protected_sidecar(tmp_path):
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    target = (state_dir / "scorecard" / "latest.json").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")

    denied = { (state_dir / rel).resolve() for rel in FITNESS_SIDECARS }
    prevented = []
    tool = WriteFileTool(workspace=workspace, denied_paths=denied, on_prevent_write=lambda p: prevented.append(p.name))

    res = await tool.execute(path=str(target), content='{"hacked": 1}')
    assert "blocked" in res.lower()
    assert prevented == ["latest.json"]
    assert target.read_text(encoding="utf-8") == "{}"

@pytest.mark.asyncio
async def test_edit_file_blocks_protected_sidecar(tmp_path):
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    target = (state_dir / "demand" / "completed.json").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{\"done\": 0}", encoding="utf-8")

    denied = { (state_dir / rel).resolve() for rel in FITNESS_SIDECARS }
    prevented = []
    tool = EditFileTool(workspace=workspace, denied_paths=denied, on_prevent_write=lambda p: prevented.append(p.name))

    res = await tool.execute(path=str(target), old_text="0", new_text="1")
    assert "blocked" in res.lower()
    assert prevented == ["completed.json"]
    assert target.read_text(encoding="utf-8") == "{\"done\": 0}"

@pytest.mark.asyncio
async def test_exec_tool_blocks_protected_sidecar(tmp_path):
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    target = (state_dir / "scorecard" / "latest.json").resolve()

    denied = { (state_dir / rel).resolve() for rel in FITNESS_SIDECARS }
    prevented = []
    tool = ExecTool(denied_paths=denied, on_prevent_access=lambda p: prevented.append(p.name))

    cmd = f"echo 1 > {target}"
    res = await tool.execute(command=cmd, working_dir=str(workspace))
    assert "blocked" in res.lower()
    assert prevented == ["latest.json"]

@pytest.mark.asyncio
async def test_subagent_manager_registers_denied_paths(tmp_path):
    from nanobot.agent.subagent import SubagentManager

    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()

    denied = { (state_dir / rel).resolve() for rel in FITNESS_SIDECARS }
    mgr = SubagentManager(
        provider=None,
        workspace=workspace,
        bus=None,
        model="dummy-model",
        denied_paths=denied,
    )
    assert mgr.denied_paths == denied
    assert mgr.prevented_access_attempts == []

@pytest.mark.asyncio
async def test_exec_tool_does_not_block_generic_sidecar_filename_in_workspace(tmp_path):
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()

    denied = { (state_dir / rel).resolve() for rel in FITNESS_SIDECARS }
    prevented = []
    tool = ExecTool(denied_paths=denied, on_prevent_access=lambda p: prevented.append(p.name))

    cmd = "cat tests/fixtures/latest.json"
    res = await tool.execute(command=cmd, working_dir=str(workspace))
    assert "blocked by safety guard" not in (res or "").lower()
    assert prevented == []




