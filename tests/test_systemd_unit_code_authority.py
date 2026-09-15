"""Every unit that imports the runtime must name the current release as its code authority.

The venv at /opt/eeepc-agent/venv carries an editable .pth pinning whichever
release was installed when the venv was built.  A unit that runs
`/opt/eeepc-agent/venv/bin/python` without PYTHONPATH therefore imports
`nanobot` from that stale release, not from `current/`.

eeebot-local-ci.service shipped without the line and failed on every firing with
`ImportError: cannot import name 'run_and_record_local_ci'` against a release
from June, leaving no artifact behind — a guard that runs and records nothing
reads exactly like one that was never installed.

The interpreter alone is not the test.  Several units correctly run the shared
venv over a script that never touches nanobot; demanding PYTHONPATH of those
would be pattern matching, not diagnosis.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_UNIT_DIR = Path(__file__).resolve().parents[1] / "host" / "eeepc" / "systemd"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CURRENT = "/opt/eeepc-agent/runtimes/self-evolving-agent/current"
# Two spellings reach the same interpreter: the shared venv directly, and the
# per-runtime symlink into it. Matching only the first let eeebot-dashboard
# out from under this guard.
_VENV_PYTHON = re.compile(r"/opt/eeepc-agent/\S*venv/bin/python")

_PYTHONPATH = re.compile(r"^Environment=PYTHONPATH=(?P<value>\S+)\s*$", re.MULTILINE)
_NANOBOT_IMPORT = re.compile(r"^[ \t]*(?:from|import)[ \t]+nanobot\b", re.MULTILINE)
_RELEASE_SCRIPT = re.compile(re.escape(_CURRENT) + r"/(\S+\.py)")


def _service_units() -> list[Path]:
    return sorted(_UNIT_DIR.glob("*.service"))


def _imports_nanobot(text: str) -> bool:
    """Does this unit's entrypoint resolve the `nanobot` package at all?

    A lazy import inside a function counts: it resolves through the same stale
    .pth the moment that branch runs.
    """
    for line in text.splitlines():
        if not line.startswith(("ExecStart=", "ExecStartPre=")):
            continue
        if _VENV_PYTHON.search(line) is None:
            continue
        if "-m nanobot" in line:
            return True
        match = _RELEASE_SCRIPT.search(line)
        if match is None:
            # Entrypoint lives outside the release (workspace, libexec); the
            # current symlink is not its code authority.
            continue
        script = _REPO_ROOT / match.group(1)
        if script.is_file() and _NANOBOT_IMPORT.search(script.read_text(encoding="utf-8")):
            return True
    return False


def test_at_least_one_unit_is_covered():
    """Guard the guard: a matcher that silently covers nothing passes forever."""
    covered = [
        unit.name
        for unit in _service_units()
        if _imports_nanobot(unit.read_text(encoding="utf-8"))
    ]
    assert len(covered) >= 5, f"matcher covers too few units to be trusted: {covered}"


@pytest.mark.parametrize("unit", _service_units(), ids=lambda p: p.name)
def test_nanobot_importing_units_pin_the_current_release(unit: Path):
    text = unit.read_text(encoding="utf-8")
    if not _imports_nanobot(text):
        pytest.skip(f"{unit.name} does not resolve the nanobot package")

    match = _PYTHONPATH.search(text)
    assert match is not None, (
        f"{unit.name} imports nanobot without Environment=PYTHONPATH; "
        "imports resolve through the venv's editable .pth to a stale release"
    )
    assert match.group("value") == _CURRENT, (
        f"{unit.name} pins PYTHONPATH to {match.group('value')!r}; the code "
        f"authority is the current symlink {_CURRENT!r}, never a dated release"
    )
