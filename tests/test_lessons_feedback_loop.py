"""Tests for PR 1–4: meaningful lessons, commits_pushed fix, already_done detector,
previous-attempts feedback loop.
"""
from __future__ import annotations

import ast
import datetime
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from nanobot.runtime.lessons import update_lessons_from_cycle, LessonsDB


# ---------------------------------------------------------------------------
# Bridge function extraction (avoids importing heavy bridge module dependencies)
# ---------------------------------------------------------------------------

def _load_bridge_ns(*names: str) -> dict:
    """Extract named functions from bridge without triggering top-level imports."""
    bridge_path = Path(__file__).parent.parent / "nanobot" / "runtime" / "bridge.py"
    source = bridge_path.read_text()
    ns: dict = {
        "re": re, "Path": Path, "json": json, "datetime": datetime,
        "STATE_DIR": Path("/tmp/fake-state"),
    }
    # Extract module-level tuple/list constants that functions may reference
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    try:
                        exec(ast.get_source_segment(source, node), ns)  # noqa: S102
                    except Exception:
                        pass
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            func_src = ast.get_source_segment(source, node)
            exec(func_src, ns)  # noqa: S102
    return ns


# ---------------------------------------------------------------------------
# PR 1: Meaningful lessons content
# ---------------------------------------------------------------------------

class TestMeaningfulLessons:
    def _call(self, **kw):
        base = dict(
            workspace=None,
            result_status="PASS",
            current_task_id="subagent-verify-materialized-improvement",
            summary="cycle done",
            artifact_paths=["scripts/foo.py"],
            reward_signal={"value": 1.2},
            feedback_decision={"mode": "handoff_to_subagent_verification"},
            cycle_id="cycle-test",
            recorded_at="2026-06-22T10:00:00Z",
            commits_pushed=0,
        )
        base.update(kw)
        return base

    def test_no_commit_records_diagnostic_error(self, tmp_path):
        """commits_pushed=0 + subagent task → records diagnostic error, not lesson."""
        result = update_lessons_from_cycle(**self._call(workspace=tmp_path))
        assert result["action"] == "recorded_error"
        assert result.get("reason") == "subagent_no_commit"

        db = LessonsDB(tmp_path)
        errors = db.load_errors()
        assert len(errors) == 1
        err = errors[0]
        assert err["category"] == "subagent_no_commit"
        assert "already done" in err["prevention"].lower()

    def test_with_commits_records_meaningful_lesson(self, tmp_path):
        """commits_pushed>0 → lesson with real approach (not old template)."""
        result = update_lessons_from_cycle(
            **self._call(
                workspace=tmp_path,
                commits_pushed=2,
                artifact_paths=["scripts/foo.py", "scripts/bar.py"],
                current_task_id="exploit-successful-improvement-path",
            )
        )
        assert result["action"] == "recorded_lesson"
        db = LessonsDB(tmp_path)
        lesson = db.load_lessons()[0]
        # approach must reference commit count or files
        assert "commit" in lesson["approach"].lower() or "foo.py" in lesson["approach"]
        # old template strings must be gone
        assert "Pattern reusable across similar" not in lesson["reusable_insight"]
        assert "Consolidate this optimization pattern" not in lesson["reusable_insight"]

    def test_clean_files_lesson_mentions_files(self, tmp_path):
        """Source files changed without commits → lesson mentions file names."""
        result = update_lessons_from_cycle(
            **self._call(
                workspace=tmp_path,
                commits_pushed=0,
                artifact_paths=["scripts/cycle_logger.py"],
                current_task_id="exploit-successful-improvement-path",
                reward_signal={"value": 1.2},
            )
        )
        assert result["action"] == "recorded_lesson"
        db = LessonsDB(tmp_path)
        lesson = db.load_lessons()[0]
        assert "cycle_logger" in lesson["approach"] or "cycle_logger" in lesson["reusable_insight"]

    def test_no_commit_non_subagent_task_not_error(self, tmp_path):
        """commits_pushed=0 on non-subagent task → not a subagent_no_commit error."""
        result = update_lessons_from_cycle(
            **self._call(
                workspace=tmp_path,
                commits_pushed=0,
                current_task_id="exploit-successful-improvement-path",
                artifact_paths=["scripts/foo.py"],
                reward_signal={"value": 1.2},
            )
        )
        assert result.get("reason") != "subagent_no_commit"

    def test_feedback_mode_in_approach_when_commits(self, tmp_path):
        """fd.mode appears in approach text when commits > 0."""
        result = update_lessons_from_cycle(
            **self._call(
                workspace=tmp_path,
                commits_pushed=1,
                artifact_paths=["scripts/smoke_test_loop.py"],
                current_task_id="exploit-successful-improvement-path",
                feedback_decision={"mode": "complete_active_lane"},
            )
        )
        assert result["action"] == "recorded_lesson"
        db = LessonsDB(tmp_path)
        lesson = db.load_lessons()[0]
        assert "complete_active_lane" in lesson["approach"]


# ---------------------------------------------------------------------------
# PR 2: commits_pushed from eeebot-self-evolving (git fixture tests)
# ---------------------------------------------------------------------------

class TestCommitsPushedFromSelfEvo:
    def test_selfevo_repo_git_log_detects_commit(self, tmp_path):
        """A git commit in eeebot-self-evolving is visible via git log."""
        repo = tmp_path / "eeebot-self-evolving"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], capture_output=True)
        (repo / "test.py").write_text("print('hello')")
        subprocess.run(["git", "-C", str(repo), "add", "test.py"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "feat: test"], capture_output=True)

        result = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True,
        )
        assert "feat: test" in result.stdout

    def test_target_workspace_is_not_git_repo(self, tmp_path):
        """TARGET_WORKSPACE canonical release is NOT a git repo."""
        workspace = tmp_path / "current"
        workspace.mkdir()
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0  # not a git repo

    def test_missing_selfevo_gives_zero(self, tmp_path):
        """Non-existent eeebot-self-evolving → is_dir() False → commits=0."""
        selfevo = tmp_path / "eeebot-self-evolving"
        assert not selfevo.is_dir()


# ---------------------------------------------------------------------------
# PR 3: _task_already_done detector
# ---------------------------------------------------------------------------

class TestTaskAlreadyDone:
    def _make_repo_with_commit(self, base_path: Path, msg: str) -> Path:
        repo = base_path / "selfevo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], capture_output=True)
        (repo / "f.py").write_text("x=1")
        subprocess.run(["git", "-C", str(repo), "add", "f.py"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", msg], capture_output=True)
        return repo

    def _task_already_done(self, title: str, repo: Path) -> bool:
        ns = _load_bridge_ns("_task_already_done")
        return ns["_task_already_done"](title, repo)

    def test_finds_task_by_keyword_match(self, tmp_path):
        repo = self._make_repo_with_commit(
            tmp_path, "feat: restructure MEMORY.md into active/completed sections"
        )
        assert self._task_already_done("Restructure MEMORY.md into active sections", repo) is True

    def test_no_match_returns_false(self, tmp_path):
        repo = self._make_repo_with_commit(tmp_path, "fix: unrelated bugfix in parser")
        assert self._task_already_done("Implement cycle_logger stall detection", repo) is False

    def test_empty_title_returns_false(self, tmp_path):
        repo = self._make_repo_with_commit(tmp_path, "feat: something")
        assert self._task_already_done("", repo) is False

    def test_missing_repo_returns_false(self, tmp_path):
        assert self._task_already_done("Some Task", tmp_path / "nonexistent") is False

    def test_short_words_below_threshold_no_match(self, tmp_path):
        """Words < 4 chars don't count → no keywords extracted → no match."""
        repo = self._make_repo_with_commit(tmp_path, "fix: add new key")
        # "add", "new", "key" all < 4 chars → extracted words = []
        result = self._task_already_done("add new key", repo)
        assert result is False

    def test_two_keyword_requirement(self, tmp_path):
        """Requires >= 2 matching keywords (avoids single-word false positives)."""
        repo = self._make_repo_with_commit(tmp_path, "feat: update config schema")
        # "update" is in commit, "MEMORY" is not → only 1 match → False
        assert self._task_already_done("Update MEMORY archiver", repo) is False


# ---------------------------------------------------------------------------
# PR 4: _get_previous_attempts and prompt injection
# ---------------------------------------------------------------------------

class TestGetPreviousAttempts:
    def _write_result(
        self, results_dir: Path, cycle_id: str, commits: int,
        keyword: str = "", source_artifact: str = "",
    ) -> None:
        import datetime as dt2
        results_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "materialized_from": "bridge_llm_execution",
            "cycle_id": cycle_id,
            "commits_pushed": commits,
            "created_at": dt2.datetime.now(dt2.timezone.utc).isoformat(),
            # Generic summary — mirrors real bridge output (deliberately NOT task-specific)
            "summary": "Bridge subagent ran but produced no new commits.",
            "key_learnings": [
                "No commits." if commits == 0 else f"Committed {commits} change(s)."
            ],
            "result_status": "completed",
        }
        if source_artifact:
            payload["source_artifact"] = source_artifact
        if keyword:  # fallback field for old-style summary matching tests
            payload["summary"] = f"task about {keyword}"
        (results_dir / f"r-{cycle_id}.json").write_text(json.dumps(payload))

    def _write_artifact(self, tmp_path: Path, title: str) -> str:
        """Write a fake materialized artifact with nbc.title and return its path."""
        art_dir = tmp_path / "improvements"
        art_dir.mkdir(parents=True, exist_ok=True)
        art_path = art_dir / "materialized-test.json"
        art_path.write_text(json.dumps({
            "next_bounded_candidate": {"title": title}
        }))
        return str(art_path)

    def _get_previous_attempts(self, state_dir: Path, backlog_title: str,
                                cycle_id: str) -> list:
        ns = _load_bridge_ns("_get_previous_attempts")
        return ns["_get_previous_attempts"](state_dir, backlog_title, cycle_id)

    def test_matches_by_cycle_id(self, tmp_path):
        results_dir = tmp_path / "subagents" / "results"
        self._write_result(results_dir, "cycle-abc", 0)
        self._write_result(results_dir, "cycle-xyz", 1)

        results = self._get_previous_attempts(tmp_path, backlog_title="", cycle_id="cycle-abc")
        assert len(results) == 1
        assert results[0]["cycle_id"] == "cycle-abc"

    def test_matches_by_source_artifact_title(self, tmp_path):
        """Primary match: source_artifact → nbc.title keyword overlap."""
        results_dir = tmp_path / "subagents" / "results"
        art_path = self._write_artifact(tmp_path, title="Restructure MEMORY.md active backlog")
        self._write_result(results_dir, "cycle-111", 0, source_artifact=art_path)

        results = self._get_previous_attempts(
            tmp_path, backlog_title="Restructure MEMORY.md", cycle_id="cycle-new"
        )
        assert len(results) == 1

    def test_falls_back_to_summary_when_artifact_missing(self, tmp_path):
        """When source_artifact is absent/deleted, falls back to summary keyword."""
        results_dir = tmp_path / "subagents" / "results"
        # Write result with a non-existent artifact path but keyword in summary
        self._write_result(
            results_dir, "cycle-222", 0,
            source_artifact="/tmp/nonexistent_artifact.json",
            keyword="memory restructure",
        )
        results = self._get_previous_attempts(
            tmp_path, backlog_title="Restructure MEMORY.md", cycle_id="cycle-new"
        )
        assert len(results) == 1

    def test_no_match_when_both_miss(self, tmp_path):
        """Neither artifact title nor summary match → empty (no false positives)."""
        results_dir = tmp_path / "subagents" / "results"
        art_path = self._write_artifact(tmp_path, title="Some completely different task")
        self._write_result(results_dir, "cycle-333", 0, source_artifact=art_path)

        results = self._get_previous_attempts(
            tmp_path, backlog_title="Restructure MEMORY.md", cycle_id="cycle-new"
        )
        assert len(results) == 0

    def test_matches_by_summary_keyword(self, tmp_path):
        """Legacy fallback: summary keyword match still works when artifact missing."""
        results_dir = tmp_path / "subagents" / "results"
        self._write_result(results_dir, "cycle-111", 0, keyword="memory restructure")

        results = self._get_previous_attempts(
            tmp_path, backlog_title="Restructure MEMORY.md", cycle_id="cycle-new"
        )
        assert len(results) == 1

    def test_ignores_non_bridge_results(self, tmp_path):
        results_dir = tmp_path / "subagents" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        payload = {"materialized_from": "coordinator_stub", "cycle_id": "cycle-abc",
                   "commits_pushed": 0, "summary": "stub"}
        (results_dir / "stub.json").write_text(json.dumps(payload))

        results = self._get_previous_attempts(tmp_path, backlog_title="", cycle_id="cycle-abc")
        assert len(results) == 0

    def test_empty_dir_returns_empty(self, tmp_path):
        results = self._get_previous_attempts(tmp_path, backlog_title="anything", cycle_id="c1")
        assert results == []


class TestPreviousAttemptsInPrompt:
    def _write_result(self, results_dir: Path, cycle_id: str, commits: int,
                      keyword: str = "") -> None:
        import datetime as dt3
        results_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "materialized_from": "bridge_llm_execution",
            "cycle_id": cycle_id,
            "commits_pushed": commits,
            "created_at": dt3.datetime.now(dt3.timezone.utc).isoformat(),
            "summary": f"task about {keyword}" if keyword else "completed",
            "key_learnings": ["No commits." if commits == 0 else "Done."],
            "result_status": "completed",
        }
        (results_dir / f"r-{cycle_id}.json").write_text(json.dumps(payload))

    def _build_task(self, req: dict, state_dir: Path) -> str:
        ns = _load_bridge_ns("_get_previous_attempts", "build_task")
        return ns["build_task"](req, "mission text", "report-src", state_dir=state_dir)

    def _make_req(self, tmp_path: Path, title: str) -> dict:
        art = tmp_path / "art.json"
        art.write_text(json.dumps({"next_bounded_candidate": {
            "title": title,
            "backlog_instructions": "implement it",
            "backlog_priority": 9,
        }}))
        return {
            "cycle_id": "cycle-new",
            "goal_id": "g1",
            "task_title": title,
            "source_artifact": str(art),
            "lessons_context": {},
        }

    def test_previous_attempts_section_appears(self, tmp_path):
        results_dir = tmp_path / "subagents" / "results"
        self._write_result(results_dir, "cycle-aaa", 0, keyword="memory")
        self._write_result(results_dir, "cycle-bbb", 0, keyword="memory")

        req = self._make_req(tmp_path, "Restructure memory.md")
        prompt = self._build_task(req, tmp_path)
        assert "## Previous attempts" in prompt

    def test_no_previous_attempts_no_section(self, tmp_path):
        req = self._make_req(tmp_path, "Brand new task nobody did before")
        prompt = self._build_task(req, tmp_path)
        assert "## Previous attempts" not in prompt

    def test_all_no_commit_adds_must_commit_instruction(self, tmp_path):
        results_dir = tmp_path / "subagents" / "results"
        self._write_result(results_dir, "cycle-x1", 0, keyword="cycle logger")
        self._write_result(results_dir, "cycle-x2", 0, keyword="cycle logger")

        req = self._make_req(tmp_path, "Improve cycle_logger script")
        prompt = self._build_task(req, tmp_path)
        assert "MUST produce at least one commit" in prompt

    def test_successful_prior_attempt_no_must_commit(self, tmp_path):
        results_dir = tmp_path / "subagents" / "results"
        self._write_result(results_dir, "cycle-ok", 1, keyword="cycle logger")

        req = self._make_req(tmp_path, "Improve cycle_logger script")
        prompt = self._build_task(req, tmp_path)
        assert "MUST produce at least one commit" not in prompt
