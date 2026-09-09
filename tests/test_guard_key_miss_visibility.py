"""#1448: a guard keyed on a mutable identity must say when it stops matching.

Both guards here match against text that something ELSE owns and can rewrite:
`_try_mark_backlog_done` against the instance-authored MEMORY.md backlog title,
`mark_reflection_consumed` against reflector-authored recommendation text. On a
drift both previously returned quietly -- `updated == text` and `False` -- which
is observationally identical to "there was nothing to do".

Every test here pins BOTH directions. A miss counter with no sibling hit
counter cannot separate "nothing drifted" from "this never ran": zero rows
would read as health either way (#1188).

Neither guard's decision changes. Only the record does.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import reflector


def _guard_rows(state_dir: Path, guard: str) -> list[dict]:
    path = state_dir / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    return [
        row for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        if row.get("phase") == "guard_key_match" and row.get("guard") == guard
    ]


def _ledger_rows(state_dir: Path, phase: str) -> list[dict]:
    path = state_dir / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("phase") == phase]


def _write_reflection(state_dir: Path, detail: str) -> Path:
    journal = state_dir / "reflector" / "reflections.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps({
            "summary": "a summary",
            "recommendations": [{"detail": detail}],
        }) + "\n",
        encoding="utf-8",
    )
    return journal


def test_consumption_hit_is_recorded_so_the_miss_count_has_a_denominator(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    _write_reflection(state, "do the thing")

    assert reflector.mark_reflection_consumed(state, recommendation_detail="do the thing") is True

    rows = _ledger_rows(state, "reflection_consumption")
    assert [row["outcome"] for row in rows] == ["hit"]
    assert rows[0]["recommendation_detail"] == "do the thing"


def test_drifted_recommendation_detail_records_the_key_it_sought(tmp_path: Path):
    """The reflector reworded its recommendation; the consumer's key no longer matches."""
    state = tmp_path / "state"
    state.mkdir()
    _write_reflection(state, "do the thing")

    # Same intent, different wording -- exactly the drift this exists to catch.
    assert reflector.mark_reflection_consumed(state, recommendation_detail="do that thing") is False

    rows = _ledger_rows(state, "reflection_consumption")
    assert [row["outcome"] for row in rows] == ["miss"]
    assert rows[0]["recommendation_detail"] == "do that thing", (
        "the record must name the key that failed, not merely that one did"
    )


def test_consumption_return_value_is_unchanged_by_the_journalling(tmp_path: Path):
    """Behaviour preservation: the decision is identical, only the record is new."""
    state = tmp_path / "state"
    state.mkdir()
    _write_reflection(state, "match me")

    assert reflector.mark_reflection_consumed(state, recommendation_detail="match me") is True
    assert reflector.mark_reflection_consumed(state, recommendation_detail="no such detail") is False
    # A second consumption of an already-consumed entry stays False.
    assert reflector.mark_reflection_consumed(state, recommendation_detail="match me") is False


def test_journalling_failure_never_breaks_consumption(tmp_path: Path, monkeypatch):
    """Fail-open: bookkeeping must not be able to fail the caller."""
    state = tmp_path / "state"
    state.mkdir()
    _write_reflection(state, "resilient")

    import nanobot.runtime.cycle_ledger as cycle_ledger

    def _boom(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(cycle_ledger, "append_event", _boom)
    assert reflector.mark_reflection_consumed(state, recommendation_detail="resilient") is True


def _memory_repo(tmp_path: Path, backlog_title: str) -> Path:
    """A repo whose MEMORY.md carries one active backlog entry."""
    import subprocess

    repo = tmp_path / "repo"
    (repo / "memory").mkdir(parents=True)
    (repo / "memory" / "MEMORY.md").write_text(
        "# Memory\n\n"
        "## Active backlog\n\n"
        f"### Priority 1: {backlog_title}\n\n"
        "Some detail about the task.\n\n"
        "## Completed\n\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-qm", "init"],
        cwd=repo, capture_output=True,
    )
    return repo


def test_backlog_done_guard_records_a_miss_when_the_title_drifted(tmp_path: Path):
    """The instance reworded its own backlog heading; the completion key misses."""
    from nanobot.runtime import bridge

    state = tmp_path / "state"
    state.mkdir()
    repo = _memory_repo(tmp_path, "Add the widget")

    bridge._try_mark_backlog_done(
        repo_root=repo,
        backlog_title="Add a widget",          # reworded -- no longer matches
        what_was_done="did it",
        state_dir=state,
        cycle_id="cycle-drift",
    )

    rows = _ledger_rows(state, "backlog_done_guard")
    assert rows, "a drifted completion key left no trace at all"
    assert rows[-1]["outcome"].startswith("miss:")
    assert rows[-1]["backlog_title"] == "Add a widget", (
        "the record must name the title it sought"
    )


def test_backlog_done_guard_records_a_hit_so_misses_have_a_denominator(tmp_path: Path):
    from nanobot.runtime import bridge

    state = tmp_path / "state"
    state.mkdir()
    repo = _memory_repo(tmp_path, "Add the widget")

    bridge._try_mark_backlog_done(
        repo_root=repo,
        backlog_title="Add the widget",
        what_was_done="did it",
        state_dir=state,
        cycle_id="cycle-hit",
    )

    rows = _ledger_rows(state, "backlog_done_guard")
    assert rows, "a matching completion key left no trace"
    assert rows[-1]["outcome"].startswith("hit"), (
        "without a hit row, zero misses cannot be told from zero runs"
    )


def test_recent_failure_key_miss_is_recorded_without_changing_result(tmp_path: Path):
    from nanobot.runtime import bridge
    state = tmp_path / "state"
    state.mkdir()
    # No target path means structured intent cannot be derived; the legacy
    # no-match return remains None, while the sought title is journaled.
    assert bridge._recent_failure_match("reworded task", state, entries=[]) is None
    rows = _guard_rows(state, "recent_failure")
    assert rows and rows[-1]["outcome"] == "miss"
    assert rows[-1]["key"] == "reworded task"


def test_existence_index_miss_and_unavailable_are_distinct(tmp_path: Path, monkeypatch):
    from nanobot.runtime import existence_index
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    assert existence_index.find_similar(state, "novel title", limit=5) == []
    assert _guard_rows(state, "existence_index")[-1]["outcome"] == "miss"
    monkeypatch.setattr(existence_index, "_open_db", lambda *_: (_ for _ in ()).throw(OSError("unavailable")))
    assert existence_index.find_similar(state, "broken index", limit=5) == []
    assert _guard_rows(state, "existence_index")[-1]["outcome"] == "unavailable"


def test_backlog_done_guard_without_state_dir_is_unchanged(tmp_path: Path):
    """Callers that pass no state_dir keep the pre-#1448 behaviour exactly."""
    from nanobot.runtime import bridge

    repo = _memory_repo(tmp_path, "Add the widget")
    result = bridge._try_mark_backlog_done(
        repo_root=repo, backlog_title="Add a widget", what_was_done="did it",
    )
    assert result is False
