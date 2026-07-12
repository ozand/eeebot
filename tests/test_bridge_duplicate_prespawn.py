"""Issue #713: close the #711 pre-spawn duplicate bypass.

The pre-spawn `_task_already_done` gate in bridge.py previously only checked
the coordinator-derived `backlog_title`, never an arbitrary request's own
`task_title` (or `semantic_task_id`). That let duplicate proposals whose only
title lives on the request itself reach full subagent spawn. `_duplicate_check_title`
widens the check without touching `_task_already_done` itself.
"""
from __future__ import annotations

from pathlib import Path

from nanobot.runtime.bridge import _duplicate_check_title, _task_already_done
from tests.test_goal_backlog_routing import _make_git_repo_with_commit


def test_duplicate_check_title_prefers_backlog_then_task_title():
    req = {"task_title": "task title value", "semantic_task_id": "semantic-id"}
    assert _duplicate_check_title(req, "backlog title value") == "backlog title value"

    req_no_backlog = {"task_title": "task title value", "semantic_task_id": "semantic-id"}
    assert _duplicate_check_title(req_no_backlog, "") == "task title value"

    req_only_semantic = {"semantic_task_id": "semantic-id"}
    assert _duplicate_check_title(req_only_semantic, "") == "semantic-id"

    assert _duplicate_check_title({}, "") == ""


def test_task_title_only_request_is_duplicate(tmp_path: Path):
    """A request with ONLY task_title (no backlog_title) whose title matches a
    recent commit must be flagged as a duplicate — the #711 bypass is closed."""
    repo = _make_git_repo_with_commit(
        tmp_path, "feat: write scripts/cycle_logger.py — confirmed done"
    )
    req = {"task_title": "write scripts cycle_logger"}

    title = _duplicate_check_title(req, "")
    assert title == "write scripts cycle_logger"
    assert _task_already_done(title, repo) is True


def test_non_duplicate_task_title_not_flagged(tmp_path: Path):
    repo = _make_git_repo_with_commit(tmp_path, "chore: unrelated bookkeeping commit")
    req = {"task_title": "implement brand new dashboard widget feature"}

    title = _duplicate_check_title(req, "")
    assert title == "implement brand new dashboard widget feature"
    assert _task_already_done(title, repo) is False
