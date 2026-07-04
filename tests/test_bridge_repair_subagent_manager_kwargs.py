"""
Regression test for issue #574: the bridge's repair-turn SubagentManager spawn
used kwargs (`config`, `message_bus`) that don't exist on SubagentManager.__init__,
causing every smoke-gate repair attempt to crash the bridge with:
    TypeError: SubagentManager.__init__() got an unexpected keyword argument 'config'

Statically verifies the repair-spawn call site's keyword arguments are a valid
subset of SubagentManager.__init__'s real signature, without importing the
heavy bridge script (which pulls in nanobot config/agent machinery not
available in this test environment).
"""
import ast
import inspect
from pathlib import Path

from nanobot.agent.subagent import SubagentManager

BRIDGE_PATH = Path(__file__).parent.parent / "scripts" / "eeepc_self_evolving_subagent_bridge.py"


def _find_repair_mgr_call_kwargs() -> set[str]:
    tree = ast.parse(BRIDGE_PATH.read_text(encoding="utf-8"), filename=str(BRIDGE_PATH))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_repair_mgr"
            and isinstance(node.value, ast.Call)
        ):
            return {kw.arg for kw in node.value.keywords if kw.arg is not None}
    raise AssertionError("could not find `_repair_mgr = ...(...)` assignment in bridge script")


def test_repair_subagent_manager_call_uses_only_valid_kwargs():
    valid_params = set(inspect.signature(SubagentManager.__init__).parameters) - {"self"}
    used_kwargs = _find_repair_mgr_call_kwargs()

    invalid = used_kwargs - valid_params
    assert not invalid, (
        f"repair-turn SubagentManager call passes kwargs not accepted by "
        f"SubagentManager.__init__: {invalid} (valid: {valid_params})"
    )


def test_repair_subagent_manager_call_passes_required_bus_kwarg():
    used_kwargs = _find_repair_mgr_call_kwargs()
    # `bus` is a required positional-or-keyword param with no default; the
    # original bug used `message_bus` (invalid) and omitted `bus` entirely.
    assert "bus" in used_kwargs
    assert "message_bus" not in used_kwargs
    assert "config" not in used_kwargs
