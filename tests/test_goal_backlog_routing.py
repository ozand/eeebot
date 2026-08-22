"""#575/#592: shared git-log done-detection heuristic for goal_text.json priorities.

The coordinator-era backlog-routing tests that used to live here
(`_next_open_goal_as_backlog_task`, `_parse_backlog_task_from_goal_text`,
`_write_materialized_improvement_artifact`) were removed with the coordinator
module web (#916) — those functions no longer exist. What remains is
`_title_already_done_in_git_log` coverage (now `goal_text_utils`, one of the
three functions extracted for `bridge.py`/`llm_proposer.py`), plus the
`GOAL_TEXT_JSON`/`_make_git_repo_with_commit` fixtures that
`test_goal_text_priority_filter.py` and `test_demand.py` still import from
this module.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime.goal_text_utils import _title_already_done_in_git_log

# Real on-host goal_text.json shape (#568) — see docs/changes/backlog-routing-real-dispatch/proposal.md.
GOAL_TEXT_JSON = """\
{
  "schema_version": "goal-text-v1",
  "goal_id": "goal-bootstrap",
  "updated_at_utc": "2026-06-21T15:30:00Z",
  "text": "eeebot is a resource-aware, self-evolving autonomous agent on a weak eeepc host.\\n\\nCurrent priority targets:\\n(A) Priority 5 \\u2014 Write scripts/cycle_logger.py: helper function append_cycle_summary(repo_root, cycle_id, action, files_changed) that appends one line to memory/HISTORY.md without duplicates. Test with python3 scripts/cycle_logger.py --test. Commit.\\n(B) Priority 6 \\u2014 Write scripts/smoke_test_loop.py: quick sanity check that state/current_health.json, state/host_capabilities.json, memory/MEMORY.md, and state/goals/history/ are non-empty. Output PASS: N/N or list failures. Test with python3 scripts/smoke_test_loop.py. Commit."
}
"""


def _make_git_repo_with_commit(
    tmp_path: Path, *commit_messages: str, create_files: tuple[str, ...] = ()
) -> Path:
    """Create a tmp git repo (playing the role of eeebot-self-evolving) with one commit per message.

    Issue #748: ``create_files`` optionally creates real (empty-ish) files at
    the given repo-relative paths — needed by tests exercising the
    artifact+evidence done-detection (``_priority_done_by_artifact``), which
    requires the target file to actually exist on disk, not just be
    referenced in a commit message.
    """
    repo = tmp_path / "eeebot-self-evolving"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    for rel_path in create_files:
        file_path = repo / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("# created by test fixture\n", encoding="utf-8")
    for i, commit_message in enumerate(commit_messages):
        (repo / "f.txt").write_text(str(i), encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", commit_message], cwd=repo, check=True)
    return repo


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
