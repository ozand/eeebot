"""
Regression test for issue #718: the bridge spawned the implementation subagent
with `workspace=TARGET_WORKSPACE` (the deployed release tree — in prod, not a
git repo and never synced from the git checkout) while everything else
(branching, committing, gating, integrating) operates on `_selfevo_repo`
(`STATE_DIR.parent / 'eeebot-self-evolving'`). Any new file the subagent
authored via its workspace tools landed in the release tree and was silently
discarded — never committed, never gated, never integrated.

Statically verifies (mirroring test_bridge_repair_subagent_manager_kwargs.py)
that BOTH the main `SubagentManager` construction and the repair-turn `_SM2`
construction pass `workspace=_selfevo_repo`, not `workspace=TARGET_WORKSPACE`.
"""
from __future__ import annotations

import ast
from pathlib import Path

BRIDGE_PATH = Path(__file__).parent.parent / "nanobot" / "runtime" / "bridge.py"


def _parse_bridge() -> ast.Module:
    return ast.parse(BRIDGE_PATH.read_text(encoding="utf-8"), filename=str(BRIDGE_PATH))


def _workspace_kwarg_source(node: ast.Call) -> str | None:
    for kw in node.keywords:
        if kw.arg == "workspace":
            return ast.unparse(kw.value)
    return None


def _find_assign_call_workspace(tree: ast.Module, target_name: str) -> str:
    """Find `<target_name> = ...(...)` and return the source of its `workspace=` kwarg."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == target_name
            and isinstance(node.value, ast.Call)
        ):
            ws = _workspace_kwarg_source(node.value)
            if ws is not None:
                return ws
    raise AssertionError(f"could not find `{target_name} = ...(workspace=...)` in bridge script")


def test_main_subagent_manager_uses_selfevo_repo_workspace():
    """The main implementation-turn SubagentManager (`mgr = SubagentManager(...)`)
    must write into `_selfevo_repo` — the git checkout the bridge branches,
    commits, gates, and integrates — not `TARGET_WORKSPACE` (the deployed
    release, which is never synced back into the git repo).
    """
    tree = _parse_bridge()
    workspace_src = _find_assign_call_workspace(tree, "mgr")
    assert workspace_src == "_selfevo_repo"
    assert workspace_src != "TARGET_WORKSPACE"


def test_repair_subagent_manager_uses_selfevo_repo_workspace():
    """The repair-turn SubagentManager (`_repair_mgr = _SM2(...)`) must use the
    same workspace as the main turn, for the same reason — a repair fix that
    lands in TARGET_WORKSPACE instead of _selfevo_repo would be discarded too.
    """
    tree = _parse_bridge()
    workspace_src = _find_assign_call_workspace(tree, "_repair_mgr")
    assert workspace_src == "_selfevo_repo"
    assert workspace_src != "TARGET_WORKSPACE"


def test_selfevo_repo_defined_before_main_subagent_manager_construction():
    """#718 fix moved the `mgr = SubagentManager(...)` construction to AFTER
    `_selfevo_repo = STATE_DIR.parent / 'eeebot-self-evolving'` is defined (and
    after the cycle-branch setup validates it via `_cycle_setup['ok']`) — so
    `workspace=_selfevo_repo` refers to an already-resolved, checked-out path,
    not a NameError. Assert the lexical order in the source.
    """
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    selfevo_def_pos = source.index("_selfevo_repo = STATE_DIR.parent / 'eeebot-self-evolving'")
    mgr_construction_pos = source.index("mgr = SubagentManager(")
    assert selfevo_def_pos < mgr_construction_pos, (
        "_selfevo_repo must be defined before `mgr = SubagentManager(...)` is constructed"
    )
