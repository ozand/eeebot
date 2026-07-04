"""Task #12: route OUR goals into the subagent's executable task.

The bridge subagent reads the materialized artifact's `next_bounded_candidate`
(title + backlog_instructions, with an imperative "implement and commit" prompt).
When the MEMORY backlog is empty the coordinator fell back to a stale research
feed, so the subagent never worked on our goals. The todo.md fallback routes the
top open goal in as a concrete implement-and-commit task.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nanobot.runtime.coordinator import (
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
    _next_open_goal_as_backlog_task,
    _parse_backlog_task_from_goal_text,
    _title_already_done_in_git_log,
    _write_materialized_improvement_artifact,
)

TODO = """\
# todo
- [x] 0. done item
- [ ] 1. Approval truth normalization
  - Problem: approval file can be expired while dashboard still implies fresh.
  - Product changes:
    - recompute approval freshness from apply.ok
  - Acceptance:
    - UI says expired when expired
- [ ] 2. Next goal
"""

# Real on-host goal_text.json shape (#568) — see docs/changes/backlog-routing-real-dispatch/proposal.md.
GOAL_TEXT_JSON = """\
{
  "schema_version": "goal-text-v1",
  "goal_id": "goal-bootstrap",
  "updated_at_utc": "2026-06-21T15:30:00Z",
  "text": "eeebot is a resource-aware, self-evolving autonomous agent on a weak eeepc host.\\n\\nCurrent priority targets:\\n(A) Priority 5 \\u2014 Write scripts/cycle_logger.py: helper function append_cycle_summary(repo_root, cycle_id, action, files_changed) that appends one line to memory/HISTORY.md without duplicates. Test with python3 scripts/cycle_logger.py --test. Commit.\\n(B) Priority 6 \\u2014 Write scripts/smoke_test_loop.py: quick sanity check that state/current_health.json, state/host_capabilities.json, memory/MEMORY.md, and state/goals/history/ are non-empty. Output PASS: N/N or list failures. Test with python3 scripts/smoke_test_loop.py. Commit."
}
"""


def test_next_open_goal_as_backlog_task(tmp_path: Path):
    (tmp_path / "todo.md").write_text(TODO, encoding="utf-8")
    task = _next_open_goal_as_backlog_task(tmp_path)
    assert task is not None
    assert task["title"] == "Approval truth normalization"
    assert task["priority"] == 1
    assert "recompute approval freshness from apply.ok" in task["instructions"]
    # must stop at the next item (not bleed into goal 2)
    assert "Next goal" not in task["instructions"]


def test_no_todo_returns_none(tmp_path: Path):
    assert _next_open_goal_as_backlog_task(tmp_path) is None


def test_materialized_artifact_routes_goal_as_implement_and_commit(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    workspace = tmp_path
    (workspace / "todo.md").write_text(TODO, encoding="utf-8")

    path = _write_materialized_improvement_artifact(
        state_root=state_root,
        cycle_id="cycle-abc",
        goal_id="goal-bootstrap",
        current_task_id=MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
        summary="s",
        reward_signal={"value": 0.8},
        feedback_decision={"mode": "synthesize_next_candidate"},
        workspace=workspace,
    )
    assert path is not None
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    nbc = artifact["next_bounded_candidate"]
    # the subagent's concrete task is now OUR goal, with implement-and-commit acceptance
    assert nbc["title"] == "Approval truth normalization"
    assert "Implement and commit" in nbc["acceptance"]
    assert "recompute approval freshness" in (nbc["backlog_instructions"] or "")
    assert "Implement Priority 1" in artifact["recommended_next_action"]


def test_parse_backlog_task_from_goal_text_real_host_format(tmp_path: Path):
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / "goal_text.json").write_text(GOAL_TEXT_JSON, encoding="utf-8")

    task = _parse_backlog_task_from_goal_text(tmp_path)
    assert task is not None
    assert task["priority"] == 5
    assert "cycle_logger.py" in task["title"]
    assert "append_cycle_summary" in task["instructions"]
    assert task["source"] == "goal_text"


def test_parse_backlog_task_from_goal_text_missing_file_returns_none(tmp_path: Path):
    assert _parse_backlog_task_from_goal_text(tmp_path) is None


def test_parse_backlog_task_from_goal_text_no_priority_section_returns_none(tmp_path: Path):
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / "goal_text.json").write_text(
        json.dumps({"text": "just a plain goal description, no priority targets here."}),
        encoding="utf-8",
    )
    assert _parse_backlog_task_from_goal_text(tmp_path) is None


# ─── #575: skip already-done goal_text.json priorities via shared git-log heuristic ───

def _make_git_repo_with_commit(tmp_path: Path, *commit_messages: str) -> Path:
    """Create a tmp git repo (playing the role of eeebot-self-evolving) with one commit per message."""
    repo = tmp_path / "eeebot-self-evolving"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    for i, commit_message in enumerate(commit_messages):
        (repo / "f.txt").write_text(str(i), encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", commit_message], cwd=repo, check=True)
    return repo


def test_parse_backlog_task_from_goal_text_skips_done_priority_via_git_log(tmp_path: Path):
    """Priority 5's title words ('write', 'scripts', 'cycle', 'logger') all match one
    commit line (per-line proportional match, #592) → skipped, P6 returned."""
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / "goal_text.json").write_text(GOAL_TEXT_JSON, encoding="utf-8")

    repo = _make_git_repo_with_commit(
        tmp_path, "feat: write scripts/cycle_logger.py — confirmed done for cycle-999"
    )

    task = _parse_backlog_task_from_goal_text(tmp_path, selfevo_repo_root=repo)
    assert task is not None
    assert task["priority"] == 6
    assert "smoke_test_loop.py" in task["title"]


def test_parse_backlog_task_from_goal_text_all_done_returns_none(tmp_path: Path):
    """When every found priority's title words all match on some single commit
    line, the function returns None (each priority "done" via its own commit)."""
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / "goal_text.json").write_text(GOAL_TEXT_JSON, encoding="utf-8")

    repo = _make_git_repo_with_commit(
        tmp_path,
        "feat: write scripts/cycle_logger.py finished",
        "feat: write scripts/smoke_test_loop.py finished with test",
    )

    task = _parse_backlog_task_from_goal_text(tmp_path, selfevo_repo_root=repo)
    assert task is None


def test_parse_backlog_task_from_goal_text_open_priority_regression(tmp_path: Path):
    """An open priority with no matching commits is still returned correctly."""
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / "goal_text.json").write_text(GOAL_TEXT_JSON, encoding="utf-8")

    repo = _make_git_repo_with_commit(tmp_path, "chore: unrelated housekeeping commit")

    task = _parse_backlog_task_from_goal_text(tmp_path, selfevo_repo_root=repo)
    assert task is not None
    assert task["priority"] == 5
    assert "cycle_logger.py" in task["title"]


def test_materialized_artifact_routes_goal_text_over_research_feed(tmp_path: Path):
    state_root = tmp_path / "state"
    goals_dir = state_root / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "goal_text.json").write_text(GOAL_TEXT_JSON, encoding="utf-8")

    research_dir = state_root / "research"
    research_dir.mkdir(parents=True)
    (research_dir / "feed.json").write_text(
        json.dumps({"entries": [{"title": "stale research candidate", "action": "do something vague"}]}),
        encoding="utf-8",
    )

    # No todo.md / no eeebot-self-evolving MEMORY.md — MEMORY.md exhausted.
    path = _write_materialized_improvement_artifact(
        state_root=state_root,
        cycle_id="cycle-goal-text",
        goal_id="goal-bootstrap",
        current_task_id=MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
        summary="s",
        reward_signal={"value": 0.8},
        feedback_decision={"mode": "synthesize_next_candidate"},
        workspace=tmp_path,
    )
    assert path is not None
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    nbc = artifact["next_bounded_candidate"]
    assert "cycle_logger.py" in nbc["title"]
    assert nbc["backlog_priority"] == 5
    assert "stale research candidate" not in json.dumps(artifact)


# ─── #592: per-line proportional match — stop false positives from commit-log word pooling ───

# Realistic synthetic 14-day bot commit log: the autonomous bot commits ~74x/24h with a narrow,
# repetitive vocabulary ("write", "scripts", "test", "subagent", "queue", "dashboard", ...). Under
# the old "any 2 words anywhere in the whole log" rule, this noise alone flipped fresh, never-done
# titles to "already done". None of these lines are actually about any of the three live titles.
BOT_NOISE_GIT_LOG = "\n".join([
    "a1b2c3d feat: add 18 self-tests to scripts/analyze_pass_streak.py with --test flag",
    "b2c3d4e chore: record structured lesson for [cycle-xxxxxx]",
    'c3d4e5f chore: move "Add self-tests to scripts/archive_subagent_requests.py" to Completed (bridge safety-net)',
    "d4e5f6a feat: add 10 self-tests to scripts/report_summary.py with --test flag",
    "e5f6a7b chore: log cycle-199d395d2f19 — analyze_pass_streak.py self-tests added",
])


def test_title_already_done_false_positive_host_metrics_sampler(tmp_path: Path):
    """Live bug: 'Write scripts/host_metrics_sampler.py' was never written, but the old
    anywhere-in-log rule pooled 'write'/'scripts' matches from unrelated commits → False now."""
    assert _title_already_done_in_git_log(
        "Write scripts/host_metrics_sampler.py", BOT_NOISE_GIT_LOG
    ) is False


def test_title_already_done_false_positive_subagent_queue(tmp_path: Path):
    """Live bug: the stale subagent queue (11 items) was not actually reduced, but
    'subagent'/'request' pooled from an unrelated commit about a different script → False now."""
    assert _title_already_done_in_git_log(
        "Reduce the stale subagent request queue below 10", BOT_NOISE_GIT_LOG
    ) is False


def test_title_already_done_false_positive_dashboard_wiring(tmp_path: Path):
    """Live bug: host metrics were not wired into the dashboard; no commit mentions
    either concept, so the old rule's incidental pooling must not fire here either."""
    assert _title_already_done_in_git_log(
        "Wire host metrics into the dashboard", BOT_NOISE_GIT_LOG
    ) is False


def test_title_already_done_true_positive_single_commit_line_preserved(tmp_path: Path):
    """A title IS done when a single commit line actually names it — per-line proportional
    matching must still detect this (regression guard against over-correcting #592)."""
    title = "Write scripts/cycle_logger.py"
    log = "\n".join([
        "a1b2c3d feat: add scripts/cycle_logger.py with append_cycle_summary helper",
        "b2c3d4e chore: unrelated housekeeping",
    ])
    assert _title_already_done_in_git_log(title, log) is True


def test_title_already_done_still_false_when_fewer_than_2_words(tmp_path: Path):
    """Unchanged edge case: fewer than 2 distinctive (4+ char) words → always False."""
    assert _title_already_done_in_git_log("Fix bug", "fix bug fix bug fix bug") is False


# Live goal_text.json shape from the #592 bug report: three fresh priorities, none ever
# actually implemented, against a noisy autonomous-bot git log that must not falsely mark
# any of them done.
LIVE_GOAL_TEXT_JSON = """\
{
  "schema_version": "goal-text-v1",
  "goal_id": "goal-bootstrap",
  "updated_at_utc": "2026-07-03T12:00:00Z",
  "text": "eeebot is a resource-aware, self-evolving autonomous agent on a weak eeepc host.\\n\\nCurrent priority targets:\\n(A) Priority 5 \\u2014 Write scripts/host_metrics_sampler.py: sample host CPU/mem/disk and write state/host_metrics.json. Test with python3 scripts/host_metrics_sampler.py --test. Commit.\\n(B) Priority 6 \\u2014 Reduce the stale subagent request queue below 10: archive or process stale entries in state/subagent_requests/. Commit.\\n(C) Priority 7 \\u2014 Wire host metrics into the dashboard: surface state/host_metrics.json fields on the operator dashboard. Commit."
}
"""


def test_parse_backlog_task_from_goal_text_live_bug_regression(tmp_path: Path):
    """End-to-end #592 regression: with a noisy bot-style git log, none of the three
    live fresh priorities should be falsely marked done — the parser must return
    Priority 5 (host_metrics_sampler), not None."""
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / "goal_text.json").write_text(LIVE_GOAL_TEXT_JSON, encoding="utf-8")

    repo = _make_git_repo_with_commit(tmp_path, *BOT_NOISE_GIT_LOG.split("\n"))

    task = _parse_backlog_task_from_goal_text(tmp_path, selfevo_repo_root=repo)
    assert task is not None
    assert task["priority"] == 5
    assert "host_metrics_sampler.py" in task["title"]
