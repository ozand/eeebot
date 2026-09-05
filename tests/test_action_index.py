"""Tests for the deterministic durable action index (#1005)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nanobot.runtime import action_index
from nanobot.runtime.action_index import build_action_index, normalize_action


# #1124: every fixture in this file hardcodes prompt-day dates in the
# 2026-08-23..2026-08-25 window. build_action_index()'s own call to
# _rotate_and_prune() at the end of every run reads the REAL wall-clock date
# via datetime.now(timezone.utc) to decide which day-files are >=7 days old
# and should be archived (gzipped) — a calendar time bomb, since the fixture
# dates fall further behind the real date every day this suite exists.
# Freeze "now" to a date inside the fixtures' own window so the archive/
# retention math this module performs is stable regardless of when the test
# actually runs, without changing any production behavior.
class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 25, 12, 0, 0, tzinfo=tz or timezone.utc)


@pytest.fixture(autouse=True)
def _frozen_calendar(monkeypatch):
    monkeypatch.setattr(action_index, "datetime", _FrozenDatetime)


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
        # #1348: parallel high-resolution list (concrete workspace-relative paths)
        "actions_detail": ["edit:scripts/final.py", "write:memory/facts/result.md"],
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


def test_skips_fully_indexed_historical_day_files_without_opening(tmp_path: Path, monkeypatch):
    """Issue #1059: historical day files that are already fully indexed must not be opened."""
    import gzip

    # 3 prompt day files: 2026-08-23, 2026-08-24, 2026-08-25
    _seed_ledger(tmp_path, "c-23", "Day 23")
    _seed_ledger(tmp_path, "c-24", "Day 24")
    _seed_ledger(tmp_path, "c-25", "Day 25")

    _write(tmp_path / "llm_calls" / "prompts" / "2026-08-23.jsonl.gz", [
        {"cycle_id": "c-23", "seq": 1, "messages": [{"role": "assistant", "tool_calls": []}]},
    ])
    _write(tmp_path / "llm_calls" / "prompts" / "2026-08-24.jsonl.gz", [
        {"cycle_id": "c-24", "seq": 1, "messages": [{"role": "assistant", "tool_calls": []}]},
    ])
    _write(tmp_path / "llm_calls" / "prompts" / "2026-08-25.jsonl", [
        {"cycle_id": "c-25", "seq": 1, "messages": [{"role": "assistant", "tool_calls": []}]},
    ])

    # Index already exists for 2026-08-23 and 2026-08-24
    _write(tmp_path / "action_index" / "2026-08-23.jsonl.gz", [
        {"cycle_id": "c-23", "ts": "2026-08-23T00:00:00Z", "task_title": "Day 23", "outcome": "success", "actions": []}
    ])
    _write(tmp_path / "action_index" / "2026-08-24.jsonl.gz", [
        {"cycle_id": "c-24", "ts": "2026-08-24T00:00:00Z", "task_title": "Day 24", "outcome": "success", "actions": []}
    ])

    opened_paths: list[str] = []
    real_open = open
    real_gzip_open = gzip.open

    def tracking_open(file, *args, **kwargs):
        opened_paths.append(str(file))
        return real_open(file, *args, **kwargs)

    def tracking_gzip_open(filename, *args, **kwargs):
        opened_paths.append(str(filename))
        return real_gzip_open(filename, *args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking_open)
    monkeypatch.setattr(gzip, "open", tracking_gzip_open)

    summary = build_action_index(tmp_path)
    assert summary["written"] == 1

    prompt_opens = [p for p in opened_paths if "prompts" in p]
    # Old days (23 and 24) must NOT have been opened at all!
    assert not any("2026-08-23" in p for p in prompt_opens), f"Opened 2026-08-23: {prompt_opens}"
    assert not any("2026-08-24" in p for p in prompt_opens), f"Opened 2026-08-24: {prompt_opens}"
    assert any("2026-08-25" in p for p in prompt_opens), f"Did not open 2026-08-25: {prompt_opens}"
