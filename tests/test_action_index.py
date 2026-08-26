"""Tests for the deterministic durable action index (#1005)."""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.action_index import build_action_index, normalize_action


def _write(path: Path, rows: list[dict] | list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(row if isinstance(row, str) else json.dumps(row))
            fh.write("\n")


def _seed_ledger(state: Path, cycle_id: str, title: str, outcome: str = "success") -> None:
    _write(state / "ledger" / "cycles.jsonl", [
        {"phase": "proposed", "cycle_id": cycle_id, "task_title": title},
        {"phase": "outcome", "cycle_id": cycle_id, "outcome": outcome, "ts": "2026-08-25T01:00:00Z"},
    ])


def test_normalization_strips_executor_prefixes_and_workspace_roots():
    assert normalize_action("read_file", {"path": "scripts/secret_name.py"}) == "read:scripts/*.py"
    assert normalize_action("edit_file", {"path": "nanobot/runtime/action_index.py"}) == "edit:nanobot/*.py"
    assert normalize_action("exec", {"command": "cd /workspace && pytest tests/test_action_index.py -q"}) == "exec:pytest"
    assert normalize_action("exec", {"command": "FOO=bar cd /workspace; git commit -am done"}) == "exec:git-commit"
    assert normalize_action(
        "read_file", {"path": "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving/scripts/x.py"},
        ("/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving",),
    ) == "read:scripts/*.py"


def test_extracts_final_ordered_call_sequence_and_is_idempotent(tmp_path: Path):
    cycle = "cycle-1"
    _seed_ledger(tmp_path, cycle, "Build action index")
    _write(tmp_path / "llm_calls" / "prompts" / "2026-08-25.jsonl", [
        {"cycle_id": cycle, "seq": 1, "messages": [{"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "lessons/old.md"}}},
            {"function": {"name": "exec", "arguments": {"command": "pytest -q"}}},
        ]}]},
        {"cycle_id": cycle, "seq": 2, "messages": [{"role": "assistant", "tool_calls": [
            {"function": {"name": "edit_file", "arguments": {"path": "scripts/final.py"}}},
            {"function": {"name": "write_file", "arguments": {"path": "memory/facts/result.md"}}},
        ]}]},
    ])

    first = build_action_index(tmp_path)
    second = build_action_index(tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "action_index" / "2026-08-25.jsonl").read_text().splitlines()]
    assert first["written"] == 1
    assert second["written"] == 0
    assert rows == [{
        "cycle_id": cycle,
        "ts": "2026-08-25T01:00:00Z",
        "task_title": "Build action index",
        "outcome": "success",
        "actions": ["edit:scripts/*.py", "write:memory/*.md"],
    }]


def test_malformed_records_counted_and_backfill_processes_two_days(tmp_path: Path):
    _seed_ledger(tmp_path, "cycle-a", "Day A")
    with (tmp_path / "ledger" / "cycles.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"phase": "proposed", "cycle_id": "cycle-b", "task_title": "Day B"}) + "\n")
        fh.write(json.dumps({"phase": "outcome", "cycle_id": "cycle-b", "outcome": "failed"}) + "\n")
    _write(tmp_path / "llm_calls" / "prompts" / "2026-08-24.jsonl", [
        '{bad json',
        {"cycle_id": "cycle-a", "seq": 1, "messages": [{"role": "assistant", "tool_calls": []}]},
    ])
    _write(tmp_path / "llm_calls" / "prompts" / "2026-08-25.jsonl", [
        {"cycle_id": "cycle-b", "seq": 1, "messages": [{"role": "assistant", "tool_calls": []}]},
    ])

    summary = build_action_index(tmp_path)
    assert summary["malformed_records"] == 1
    assert summary["written"] == 2
    assert sum(summary[key] for key in ("written", "skipped_existing", "skipped_incomplete", "skipped_write_error")) == summary["cycles"]
    assert {p.name for p in (tmp_path / "action_index").glob("*.jsonl")} == {
        "2026-08-24.jsonl", "2026-08-25.jsonl"
    }
