"""Tests for scripts/measure_package_1903.py (#1903 / ADR-031 rule 7).

Fixture rows are REAL excerpts from the eeepc host's own
state/ledger/cycles-2026-09-21.jsonl.gz, state/llm_calls/2026-09-21.jsonl
and state/demand/completed.json (read-only, via ssh), trimmed to only the
fields the script reads -- no private data, just operational telemetry
already reported in issue #1903.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "measure_package_1903", Path(__file__).parent.parent / "scripts" / "measure_package_1903.py",
)
assert _SPEC is not None and _SPEC.loader is not None
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"

    ledger_rows = [
        # cycle-45d79761fb17: partial/reject, 5 real executor calls.
        {"phase": "started", "cycle_id": "cycle-45d79761fb17", "ts": "2026-09-21T00:02:16.802957Z"},
        {"phase": "outcome", "cycle_id": "cycle-45d79761fb17", "outcome": "partial", "verdict": "reject",
         "ts": "2026-09-21T00:18:21.356575Z"},
        # cycle-aa9e21e4fcb1: success/accept, demand-linked AND usage-confirmed.
        {"phase": "started", "cycle_id": "cycle-aa9e21e4fcb1", "ts": "2026-09-21T00:29:21.703818Z"},
        {"phase": "outcome", "cycle_id": "cycle-aa9e21e4fcb1", "outcome": "success", "verdict": "accept",
         "ts": "2026-09-21T01:22:55.896973Z"},
        # cycle-91117c25f6d3: skipped-duplicate/reject, zero executor calls.
        {"phase": "started", "cycle_id": "cycle-91117c25f6d3", "ts": "2026-09-21T02:48:54.167671Z"},
        {"phase": "outcome", "cycle_id": "cycle-91117c25f6d3", "outcome": "skipped-duplicate",
         "verdict": "reject", "ts": "2026-09-21T02:49:16.633452Z"},
        # A pre-selection event reusing `cycle_id` for a candidate that was
        # REJECTED before ever starting (real phase name from the host's own
        # ledger) -- must never enter the denominator, has no `started` row.
        {"phase": "proposer_reject", "cycle_id": "goal-gap-not-a-cycle", "ts": "2026-09-21T03:00:00Z"},
        # Outside the test window (2026-09-21) -- must be excluded by --start.
        {"phase": "started", "cycle_id": "cycle-out-of-window", "ts": "2026-09-20T23:00:00Z"},
        {"phase": "outcome", "cycle_id": "cycle-out-of-window", "outcome": "success", "verdict": "accept",
         "ts": "2026-09-20T23:05:00Z"},
    ]
    _write_jsonl(state / "ledger" / "cycles.jsonl", ledger_rows)

    llm_calls_rows = [
        {"ts": "2026-09-21T00:05:04.175742Z", "cycle_id": "cycle-45d79761fb17", "component": "executor", "duration_ms": 123085.057},
        {"ts": "2026-09-21T00:08:34.381939Z", "cycle_id": "cycle-45d79761fb17", "component": "executor", "duration_ms": 206339.342},
        {"ts": "2026-09-21T00:11:38.381481Z", "cycle_id": "cycle-45d79761fb17", "component": "executor", "duration_ms": 180669.511},
        {"ts": "2026-09-21T00:14:49.261916Z", "cycle_id": "cycle-45d79761fb17", "component": "executor", "duration_ms": 189322.112},
        {"ts": "2026-09-21T00:18:13.083643Z", "cycle_id": "cycle-45d79761fb17", "component": "executor", "duration_ms": 201007.53},
        {"ts": "2026-09-21T00:30:50.699955Z", "cycle_id": "cycle-aa9e21e4fcb1", "component": "executor", "duration_ms": 43887.054},
    ]
    _write_jsonl(state / "llm_calls" / "2026-09-21.jsonl", llm_calls_rows)

    completed = {
        "schema_version": "demand-completed-v1",
        "entries": {
            "hypothesis-c085a8483773": {
                "cycle_id": "cycle-aa9e21e4fcb1",
                "ts": "2026-09-21T01:22:55.896973Z",
                "files_changed": ["scripts/run_all_tests.py", "tests/test_run_all_tests.py"],
                "confirmed": True,
                "confirmed_at": "2026-09-22T13:10:52.173135Z",
            },
        },
    }
    (state / "demand").mkdir(parents=True, exist_ok=True)
    (state / "demand" / "completed.json").write_text(json.dumps(completed), encoding="utf-8")

    return state


def test_only_started_rows_enter_the_denominator(tmp_path):
    """Pre-selection events (proposer_reject etc.) reuse `cycle_id` for
    candidates that never started -- they must not inflate n."""
    records = m.build_cycle_records(_state_dir(tmp_path))
    assert "goal-gap-not-a-cycle" not in records
    assert set(records) == {
        "cycle-45d79761fb17", "cycle-aa9e21e4fcb1", "cycle-91117c25f6d3", "cycle-out-of-window",
    }


def test_window_selection_and_per_cycle_fields(tmp_path):
    records = m.build_cycle_records(_state_dir(tmp_path))
    selected = m.select_window(
        records, start=m._parse_ts("2026-09-21T00:00:00Z"), end=m._parse_ts("2026-09-22T00:00:00Z"),
    )
    by_id = {r.cycle_id: r for r in selected}
    assert set(by_id) == {"cycle-45d79761fb17", "cycle-aa9e21e4fcb1", "cycle-91117c25f6d3"}

    partial = by_id["cycle-45d79761fb17"]
    assert partial.executor_calls == 5
    assert partial.condition_b is False
    assert partial.cause == "partial"
    assert partial.demand_linked is False

    success = by_id["cycle-aa9e21e4fcb1"]
    assert success.executor_calls == 1
    assert success.condition_b is True
    assert success.demand_linked is True
    assert success.usage_confirmed is True

    zero_call = by_id["cycle-91117c25f6d3"]
    assert zero_call.executor_calls == 0
    assert zero_call.condition_b is False
    assert zero_call.cause == "skipped-duplicate"


def test_summary_matches_manual_count(tmp_path):
    records = m.build_cycle_records(_state_dir(tmp_path))
    selected = m.select_window(
        records, start=m._parse_ts("2026-09-21T00:00:00Z"), end=m._parse_ts("2026-09-22T00:00:00Z"),
    )
    summary = m.summarize(selected)
    assert summary["n"] == 3
    assert summary["executor_calls_median"] == 1  # [0, 1, 5] -> median 1
    assert summary["condition_b_count"] == 1
    assert summary["usage_confirmed_count"] == 1


def test_base_fixture_recomputes_published_rule_c_counts():
    rows = json.loads((Path(__file__).parent / "fixtures/package_1903_base_2026-09-21.json").read_text())
    assert len(rows) == 42
    assert all(set(row) == {"cycle_id", "outcome", "verdict", "branch_files"} for row in rows)
    assert all(not path.startswith("/") and ".." not in Path(path).parts for row in rows for path in row["branch_files"])
    from nanobot.runtime.service_paths import is_service_only
    rule_c_count = lambda selected: sum(
        row["outcome"] == "success" and row["verdict"] == "accept"
        and not is_service_only(row["branch_files"]) for row in selected
    )
    first_24 = rows[:24]
    assert rule_c_count(rows) == 15
    assert rule_c_count(first_24) == 14


def test_service_paths_follow_the_published_rule_c_set():
    assert m.is_service_path("diary/2026-09-24.md")
    assert m.is_service_path("memory/HISTORY.md")
    assert m.is_service_path("memory/repeat_failures.json")
    assert not m.is_service_path("memory/facts/decay_archival_policy.md")
    assert not m.is_service_path("lessons/scaffold_first_reasoning_limits.md")
    assert not m.is_service_path("scripts/diary/tool.py")
    assert m.is_service_only(["diary/2026-09-24.md", "memory/MEMORY.md"])
    assert not m.is_service_only(["diary/2026-09-24.md", "lessons/x.md"])
    assert not m.is_service_only([])
    assert not m.is_service_only(None)


def _git(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _merge_cycle(repo: Path, cycle_id: str, files: dict[str, str], subject: str) -> None:
    _git(repo, "checkout", "-q", "-b", f"selfevo/cycle-{cycle_id}")
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", f"merge: integrate selfevo/cycle-{cycle_id}", f"selfevo/cycle-{cycle_id}")


def test_rule_c_excludes_service_only_branches_by_content_not_subject(tmp_path):
    """Rule C looks only at the files the branch changed: a diary-only
    branch is excluded even with an ordinary subject, and a residual
    auto-commit that carries real work is kept."""
    state = tmp_path / "state"
    rows = []
    for i, cid in enumerate(("cycle-diary", "cycle-work-residual", "cycle-nomerge", "cycle-partial")):
        rows.append({"phase": "started", "cycle_id": cid, "ts": f"2026-09-21T0{i}:00:00Z"})
        outcome = ("partial", "reject") if cid == "cycle-partial" else ("success", "accept")
        rows.append({"phase": "outcome", "cycle_id": cid, "outcome": outcome[0], "verdict": outcome[1],
                     "ts": f"2026-09-21T0{i}:30:00Z"})
    _write_jsonl(state / "ledger" / "cycles.jsonl", rows)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _merge_cycle(repo, "cycle-diary", {"diary/2026-09-21.md": "- note\n", "memory/HISTORY.md": "h\n"},
                 "docs: ordinary-looking subject")
    _merge_cycle(repo, "cycle-work-residual", {"diary/2026-09-21.md": "- more\n", "scripts/tool.py": "x = 1\n"},
                 "selfevo: auto-commit uncommitted subagent work — tool")

    records = m.build_cycle_records(state)
    selected = m.select_window(records, start=m._parse_ts("2026-09-21T00:00:00Z"))
    m.apply_rule_c(selected, repo, ref="main")
    by_id = {r.cycle_id: r for r in selected}

    assert by_id["cycle-diary"].excluded_by_rule_c is True
    assert by_id["cycle-work-residual"].excluded_by_rule_c is False
    assert by_id["cycle-nomerge"].branch_files is None
    assert by_id["cycle-nomerge"].excluded_by_rule_c is False
    assert by_id["cycle-partial"].excluded_by_rule_c is False

    summary = m.summarize(selected, rule_c_applied=True)
    assert summary["n"] == 4
    assert summary["condition_b_count"] == 3
    assert summary["rule_c_excluded_count"] == 1
    assert summary["condition_b_rule_c_count"] == 2
    assert summary["b_without_merge_count"] == 1


def test_rule_c_not_applied_is_reported_not_silently_equal(tmp_path):
    records = m.build_cycle_records(_state_dir(tmp_path))
    selected = m.select_window(records, start=m._parse_ts("2026-09-21T00:00:00Z"), end=m._parse_ts("2026-09-22T00:00:00Z"))
    summary = m.summarize(selected)
    assert summary["rule_c_applied"] is False
    assert summary["condition_b_rule_c_count"] is None
