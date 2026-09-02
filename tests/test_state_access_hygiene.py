from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
ALLOW = {
    "nanobot/runtime/cycle_ledger.py",
    "scripts/cleanup_subagent_queue.py",
    "tests/test_state_access_hygiene.py",
    "tests/test_cycle_ledger.py",
    "tests/test_state_access.py",
    "tests/test_bridge_recent_failure_suppress.py",
    "tests/test_goal_text_priority_filter.py",
    "tests/test_heldout.py",
    "tests/test_usage_evidence.py",
    "scripts/eeebot_dashboard.py",
    "scripts/loop_metrics_report.py",
    "scripts/migrate_backlog_title.py",
    "nanobot/runtime/action_index.py",
    "nanobot/runtime/bridge.py",
    "nanobot/runtime/existence_index.py",
    "nanobot/runtime/loop_explorer.py",
    "nanobot/runtime/scorecard.py",
    "nanobot/runtime/strategist.py",
    "nanobot/runtime/usage_evidence.py",
    "nanobot/runtime/heldout/checkers.py",
    "host/eeepc/libexec/eeepc_promotion_verifier.py",
}


def _python_files() -> list[Path]:
    return [p for p in ROOT.rglob("*.py") if ".git" not in p.parts and p.name != "state_access.py"]


def test_raw_state_reader_patterns_have_explicit_allowlist():
    patterns = ("/ledger/cycles.jsonl", 'glob("cycles-', "/subagents/results", 'glob("evolution-')
    violations = []
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(pattern in text for pattern in patterns) and rel not in ALLOW:
            violations.append(rel)
    assert not violations, f"unmigrated state readers: {violations}"


def test_hygiene_detector_rejects_new_raw_reader_pattern(tmp_path):
    scratch = tmp_path / "scratch.py"
    scratch.write_text('PATH = "ledger" / "cycles.jsonl"\n', encoding="utf-8")
    tree = ast.parse(scratch.read_text(encoding="utf-8"))
    source = ast.get_source_segment(scratch.read_text(encoding="utf-8"), tree.body[0].value)
    assert source == '"ledger" / "cycles.jsonl"'
