"""Guard against tests that only pass on their author's machine.

`tests/test_deploy_release.py` shipped with

    shutil.copy(Path("T:/Code/eeebot-wt-1146/host/eeepc/scripts/deploy_release.sh"), ...)

which resolved on the worktree it was written in and nowhere else. The suite
was green locally, the PR merged, and every job on main went red with
`FileNotFoundError` -- blocking unrelated work until someone read the log.

A hardcoded absolute path is not a style question here: it is the difference
between a test that verifies the repository and a test that verifies one
checkout of it.

Only literals that reach a filesystem call are flagged. Absolute paths appear
legitimately in this suite as *data* -- command-injection payloads in
test_eeepc_privileged_rollout_preflight, validator inputs in
test_tool_validation -- and those are not file references, so a plain grep
would flag them too and the guard would have to be disabled.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# A Windows drive-letter path (T:/..., C:\...) or a POSIX path under a user's
# home. Both name a location that exists only on one machine.
_MACHINE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/(?:home|Users)/)")

# Callables whose string arguments are filesystem locations.
_PATH_CALLS = {"Path", "PurePath", "open"}
_PATH_MODULES = {"shutil", "os", "io"}


def _is_path_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _PATH_CALLS
    if isinstance(func, ast.Attribute):
        value = func.value
        if isinstance(value, ast.Name) and value.id in _PATH_MODULES:
            return True
        # os.path.join(...), io.open(...)
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            return value.value.id in _PATH_MODULES
    return False


def _offenders() -> list[str]:
    found = []
    this_file = Path(__file__).resolve()
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        if path.resolve() == this_file:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_path_call(node):
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                    continue
                if _MACHINE_PATH.match(arg.value):
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    found.append(f"{rel}:{arg.lineno}: {arg.value!r}")
    return found


def test_no_test_opens_a_machine_local_absolute_path() -> None:
    offenders = _offenders()
    assert not offenders, (
        "tests must reach files through the repository root "
        "(Path(__file__).resolve().parents[1]), not an absolute path that "
        "exists only on one machine:\n  " + "\n  ".join(offenders)
    )
