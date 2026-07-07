"""Tests for Issue #659: pytest coverage for scripts/compile_project_lessons.py.

Derived from the stranded ad-hoc T1-T14 `--test` runner referenced in issue #659
(recovered from /var/tmp/selfevo-dirty-tree-20260706.patch on the eeepc host as a
reference guide), rewritten as idiomatic pytest with tmp_path fixtures. The
`--test` mechanism itself is not reused; only the coverage intent is.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.compile_project_lessons import (  # noqa: I001
    clean_auto_generated_markdowns,
    load_yaml,
    main,
    save_yaml,
)


# ── load_yaml ────────────────────────────────────────────────────────────────


def test_load_yaml_missing_file_returns_empty_list(tmp_path):
    assert load_yaml(tmp_path / "nonexistent.yaml") == []


def test_load_yaml_non_list_yaml_returns_empty_list(tmp_path):
    path = tmp_path / "not_a_list.yaml"
    path.write_text("key: value\n", encoding="utf-8")
    assert load_yaml(path) == []


def test_load_yaml_valid_list_round_trips(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n- c\n", encoding="utf-8")
    assert load_yaml(path) == ["a", "b", "c"]


def test_load_yaml_corrupt_file_returns_empty_list(tmp_path):
    path = tmp_path / "corrupt.yaml"
    path.write_text("{{invalid yaml: [\n", encoding="utf-8")
    assert load_yaml(path) == []


# ── save_yaml ────────────────────────────────────────────────────────────────


def test_save_yaml_creates_parent_dirs_and_round_trips(tmp_path):
    target = tmp_path / "sub" / "dir" / "test.yaml"
    save_yaml(target, [{"key": "val"}])
    assert target.exists()
    assert load_yaml(target) == [{"key": "val"}]


# ── clean_auto_generated_markdowns ──────────────────────────────────────────


def test_clean_auto_generated_markdowns_removes_old_cycle_pattern(tmp_path):
    (tmp_path / "errors").mkdir()
    (tmp_path / "lessons").mkdir()
    (tmp_path / "errors" / "ERR-cycle-abc123.md").write_text("old")
    (tmp_path / "lessons" / "LESS-cycle-def456.md").write_text("old")
    (tmp_path / "errors" / "ERR-AUTO-something.md").write_text("keep")
    (tmp_path / "lessons" / "LESS-AUTO-something.md").write_text("keep")

    clean_auto_generated_markdowns(tmp_path)

    assert not (tmp_path / "errors" / "ERR-cycle-abc123.md").exists()
    assert not (tmp_path / "lessons" / "LESS-cycle-def456.md").exists()
    assert (tmp_path / "errors" / "ERR-AUTO-something.md").exists()
    assert (tmp_path / "lessons" / "LESS-AUTO-something.md").exists()


def test_clean_auto_generated_markdowns_handles_missing_dirs(tmp_path):
    # No errors/ or lessons/ subdirectories exist under tmp_path.
    clean_auto_generated_markdowns(tmp_path)  # should not raise


# ── main(): CLI / integration behavior ──────────────────────────────────────


def _write_cycle(history_dir: Path, name: str, data: dict) -> None:
    (history_dir / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def test_main_exits_cleanly_when_history_dir_missing(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"  # never created
    monkeypatch.setenv("TARGET_WORKSPACE", str(workspace))
    monkeypatch.setenv("STATE_ROOT", str(state_root))

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert "History directory not found" in capsys.readouterr().out


def test_main_compiles_pass_and_block_cycles(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    history_dir = state_root / "goals" / "history"
    history_dir.mkdir(parents=True)

    _write_cycle(
        history_dir,
        "cycle-test-pass",
        {
            "cycle_id": "cycle-test-pass",
            "result_status": "PASS",
            "summary": "Test pass cycle",
            "current_task": "Test Task",
            "current_task_id": "test-task-id",
            "recorded_at_utc": "2026-07-01T12:00:00Z",
            "reward_signal": {"value": 0.9, "real_work_detected": True},
            "artifact_paths": ["scripts/test.py", "state/reports/ignored.json"],
        },
    )
    _write_cycle(
        history_dir,
        "cycle-test-block",
        {
            "cycle_id": "cycle-test-block",
            "result_status": "BLOCK",
            "summary": "Test block cycle",
            "current_task_id": "blocked-task-id",
            "recorded_at_utc": "2026-07-02T12:00:00Z",
            "feedback_decision": {
                "repeat_block_failure_class": "no-concrete-changes",
                "reason": "No files changed",
            },
            "next_hint": "Try again with real changes",
        },
    )
    # Expired-approval blocks are expected/manual — must be skipped entirely.
    _write_cycle(
        history_dir,
        "cycle-test-approval",
        {
            "cycle_id": "cycle-test-approval",
            "result_status": "BLOCK",
            "summary": "Approval expired",
            "current_task_id": "approval-task-id",
            "recorded_at_utc": "2026-07-03T12:00:00Z",
            "feedback_decision": {
                "repeat_block_failure_class": "approval-expired",
                "reason": "Approval expired",
            },
        },
    )

    monkeypatch.setenv("TARGET_WORKSPACE", str(workspace))
    monkeypatch.setenv("STATE_ROOT", str(state_root))

    main()

    errors = load_yaml(workspace / "lessons" / "errors.yaml")
    lessons = load_yaml(workspace / "lessons" / "lessons.yaml")

    assert len(errors) == 1
    assert errors[0]["category"] == "no-concrete-changes"
    assert len(lessons) == 1
    assert lessons[0]["id"] == "LESS-AUTO-test-task-id"
    # state/reports/ artifact must be filtered out of files_changed.
    assert lessons[0]["files_changed"] == ["scripts/test.py"]

    # Markdown detail cards were generated for the compiled entries.
    err_md = workspace / "lessons" / "errors" / f"{errors[0]['id']}.md"
    less_md = workspace / "lessons" / "lessons" / f"{lessons[0]['id']}.md"
    assert err_md.exists()
    assert less_md.exists()
    assert "no-concrete-changes" in err_md.read_text(encoding="utf-8")


def test_main_deduplicates_same_task_id_across_cycles(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    history_dir = state_root / "goals" / "history"
    history_dir.mkdir(parents=True)

    for i in range(2):
        _write_cycle(
            history_dir,
            f"cycle-dup-{i}",
            {
                "cycle_id": f"cycle-dup-{i}",
                "result_status": "PASS",
                "summary": f"Dup cycle {i}",
                "current_task": "Dup Task",
                "current_task_id": "dup-task-id",
                "recorded_at_utc": f"2026-07-{10 + i}T12:00:00Z",
                "reward_signal": {"value": 0.9, "real_work_detected": True},
                "artifact_paths": [f"scripts/file{i}.py"],
            },
        )

    monkeypatch.setenv("TARGET_WORKSPACE", str(workspace))
    monkeypatch.setenv("STATE_ROOT", str(state_root))

    main()

    lessons = load_yaml(workspace / "lessons" / "lessons.yaml")
    assert len(lessons) == 1
    assert lessons[0]["occurrences"] == 2
    assert lessons[0]["files_changed"] == ["scripts/file0.py", "scripts/file1.py"]


def test_main_preserves_handwritten_errors_and_keeps_them_first(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    history_dir = state_root / "goals" / "history"
    history_dir.mkdir(parents=True)

    lessons_dir = workspace / "lessons"
    lessons_dir.mkdir()
    save_yaml(
        lessons_dir / "errors.yaml",
        [
            {"id": "ERR-2026-hand-written", "category": "manual", "title": "Manual entry"},
            {"id": "ERR-AUTO-stale-entry", "category": "stale", "title": "Should be dropped"},
        ],
    )

    _write_cycle(
        history_dir,
        "cycle-new-block",
        {
            "cycle_id": "cycle-new-block",
            "result_status": "BLOCK",
            "summary": "New failure",
            "current_task_id": "new-task-id",
            "recorded_at_utc": "2026-07-05T12:00:00Z",
            "feedback_decision": {
                "repeat_block_failure_class": "new-failure-class",
                "reason": "Something broke",
            },
        },
    )

    monkeypatch.setenv("TARGET_WORKSPACE", str(workspace))
    monkeypatch.setenv("STATE_ROOT", str(state_root))

    main()

    errors = load_yaml(lessons_dir / "errors.yaml")
    ids = [e["id"] for e in errors]
    # Handwritten entry survives; stale ERR-AUTO- entry from a prior run does not
    # (it gets recomputed fresh from history each run), and the handwritten one
    # is prepended ahead of freshly-compiled entries.
    assert "ERR-2026-hand-written" in ids
    assert "ERR-AUTO-stale-entry" not in ids
    assert ids[0] == "ERR-2026-hand-written"


def test_main_skips_malformed_history_files_without_crashing(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    history_dir = state_root / "goals" / "history"
    history_dir.mkdir(parents=True)

    (history_dir / "cycle-corrupt.json").write_text("{not valid json", encoding="utf-8")
    _write_cycle(
        history_dir,
        "cycle-ok",
        {
            "cycle_id": "cycle-ok",
            "result_status": "PASS",
            "summary": "Fine",
            "current_task": "Fine Task",
            "current_task_id": "fine-task-id",
            "recorded_at_utc": "2026-07-06T12:00:00Z",
            "reward_signal": {"value": 1.0, "real_work_detected": True},
            "artifact_paths": ["scripts/fine.py"],
        },
    )

    monkeypatch.setenv("TARGET_WORKSPACE", str(workspace))
    monkeypatch.setenv("STATE_ROOT", str(state_root))

    main()  # should not raise despite the corrupt JSON file

    lessons = load_yaml(workspace / "lessons" / "lessons.yaml")
    assert len(lessons) == 1
    assert lessons[0]["id"] == "LESS-AUTO-fine-task-id"


def test_main_ignores_pass_cycle_with_no_real_work_and_no_artifacts(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    history_dir = state_root / "goals" / "history"
    history_dir.mkdir(parents=True)

    _write_cycle(
        history_dir,
        "cycle-noop",
        {
            "cycle_id": "cycle-noop",
            "result_status": "PASS",
            "summary": "Did nothing notable",
            "current_task": "Noop Task",
            "current_task_id": "noop-task-id",
            "recorded_at_utc": "2026-07-06T12:00:00Z",
            "reward_signal": {"value": 0.0, "real_work_detected": False},
            "artifact_paths": [],
        },
    )

    monkeypatch.setenv("TARGET_WORKSPACE", str(workspace))
    monkeypatch.setenv("STATE_ROOT", str(state_root))

    main()

    lessons = load_yaml(workspace / "lessons" / "lessons.yaml")
    assert lessons == []


def test_main_defaults_target_workspace_and_state_root_when_unset(tmp_path, monkeypatch):
    # No TARGET_WORKSPACE / STATE_ROOT set: defaults to "." and the real
    # /var/lib/... path, which normally won't exist in a test sandbox, so
    # main() should just print a message and exit(0) rather than crash.
    monkeypatch.delenv("TARGET_WORKSPACE", raising=False)
    monkeypatch.delenv("STATE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0


def test_module_help_via_subprocess_does_not_crash():
    """The script has no argparse --help, but it must at least be importable
    and runnable as `python -m` style module without unrelated fatal errors
    when there is no history directory to process."""
    import subprocess

    script_path = Path(__file__).parent.parent / "scripts" / "compile_project_lessons.py"
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "STATE_ROOT": "/nonexistent/state/root"},
    )
    assert result.returncode == 0
    assert "History directory not found" in result.stdout
