"""Regression tests for the explore ledger recorders (2026-09-01 incident).

Release fe03357c shipped three explore recorders calling a nonexistent
``_append_event`` (NameError, silently swallowed by callers' try/except —
explore events could never be written) and a bridge daily-cap check importing
a nonexistent ``read_ledger_events``. These tests fail on that code.
"""

import ast
import json
from pathlib import Path

from nanobot.runtime import cycle_ledger as cl

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_record_explore_started_writes_phase_row(tmp_path) -> None:
    cl.record_explore_started(tmp_path, "cycle-x", 2, "latency")
    rows = cl.read_events(tmp_path)
    assert [r for r in rows if r.get("phase") == "explore_started"], rows
    row = rows[-1]
    assert row["cycle_id"] == "cycle-x"
    assert row["candidates_count"] == 2
    assert row["declared_measurement"] == "latency"
    assert row.get("ts"), "append_event must stamp ts"


def test_record_candidate_and_selected_write_rows(tmp_path) -> None:
    cl.record_explore_candidate(tmp_path, "cycle-x", "cycle-x-1", 0.5)
    cl.record_explore_selected(tmp_path, "cycle-x", "selfevo/cycle-x-1")
    phases = [r.get("phase") for r in cl.read_events(tmp_path)]
    assert "explore_candidate" in phases
    assert "explore_selected" in phases


def test_read_events_missing_file_and_malformed_lines(tmp_path) -> None:
    assert cl.read_events(tmp_path) == []
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "cycles.jsonl").write_text(
        'not json\n{"phase": "outcome", "cycle_id": "c1"}\n[1,2]\n', encoding="utf-8"
    )
    rows = cl.read_events(tmp_path)
    assert rows == [{"phase": "outcome", "cycle_id": "c1"}]


def test_bridge_ledger_imports_resolve() -> None:
    """Every ``from nanobot.runtime.cycle_ledger import X`` in bridge.py must
    name a real attribute — the incident import (`read_ledger_events`) was
    hidden inside try/except and only failed at runtime."""
    source = (REPO_ROOT / "nanobot" / "runtime" / "bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "nanobot.runtime.cycle_ledger":
            for alias in node.names:
                if not hasattr(cl, alias.name):
                    missing.append(f"line {node.lineno}: {alias.name}")
    assert not missing, f"bridge.py imports nonexistent cycle_ledger names: {missing}"
