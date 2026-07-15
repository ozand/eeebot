"""Issue #690: never idle for lack of work.

When the finite goal_text.json "Current priority targets" list is exhausted
(every priority already done in recent git log), the loop must synthesize ONE
genuinely-new bounded improvement hypothesis from the GOAL VECTORS x concrete
live STATE signals, instead of falling through to the dead todo.md fallback
or the circular research-feed meta-task (the self-referential "Priority 99:
Synthesize one new bounded improvement candidate..." template).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nanobot.runtime.bridge import _task_already_done
from nanobot.runtime.coordinator import (
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
    _open_ended_novelty_directive,
    _pick_candidate_from_research_feed,
    _research_feed_entry_is_self_referential,
    _synthesize_hypothesis_from_state,
    _write_materialized_improvement_artifact,
)

# All priorities in this goal_text.json are already committed (see the git repo
# built by _make_git_repo_with_commit below) — the exhausted-backlog scenario.
GOAL_TEXT_JSON = """\
{
  "schema_version": "goal-text-v1",
  "goal_id": "goal-bootstrap",
  "updated_at_utc": "2026-07-06T07:30:00Z",
  "text": "eeebot is a resource-aware, self-evolving autonomous agent on a weak eeepc host. It has two development vectors.\\n\\nVector 1 \\u2014 Self-Optimization on Constrained Hardware: Improve runtime efficiency, reduce resource usage, build diagnostics and optimization tools.\\n\\nVector 2 \\u2014 Owner Utility and Creative Output: Create visible value for the operator: dashboards, workflow helpers, small utilities.\\n\\nCurrent priority targets:\\n(A) Priority 7 \\u2014 Wire host metrics into the dashboard: extend scripts/eeebot_dashboard.py to show the last host_metrics record.\\n(B) Priority 8 \\u2014 Correlate cycle outcomes with host resources: write scripts/cycle_resource_correlation.py.\\n"
}
"""

# Integrated cycles auto-commit the proposal title verbatim, and R30 proposals
# quote the goal_text entry verbatim — so done-evidence carries the
# "Priority N — <title>" label (#748 follow-up: for extend-type entries like
# P7 this label is REQUIRED; bare basename mentions no longer read as done).
DONE_GIT_LOG_MESSAGES = (
    "selfevo: auto-commit uncommitted subagent work — Priority 7 — Wire host "
    "metrics into the dashboard: extend scripts/eeebot_dashboard.py (#700)",
    "feat: correlate cycle outcomes with host resources in cycle_resource_correlation.py (#701)",
)


def _make_git_repo_with_commit(
    tmp_path: Path, *commit_messages: str, create_files: tuple[str, ...] = ()
) -> Path:
    """Issue #748: ``create_files`` creates real files at the given repo-relative
    paths so P7/P8 (which both name a target file: scripts/eeebot_dashboard.py,
    scripts/cycle_resource_correlation.py) are correctly recognized as done by
    the artifact+evidence check (``_priority_done_by_artifact``), which
    requires the target file to actually exist — a bare commit-message mention
    is no longer sufficient. Callers that want the "exhausted backlog"
    scenario (this module's whole premise) must pass both P7/P8 target files.
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


# Both P7/P8 target files — every test in this module that relies on the
# exhausted-backlog scenario (DONE_GIT_LOG_MESSAGES commits) must also create
# these so the artifact+evidence check (issue #748) recognizes them as done.
_P7_P8_TARGET_FILES = ("scripts/eeebot_dashboard.py", "scripts/cycle_resource_correlation.py")


def _write_goal_text(state_root: Path) -> None:
    goals_dir = state_root / "goals"
    goals_dir.mkdir(parents=True, exist_ok=True)
    (goals_dir / "goal_text.json").write_text(GOAL_TEXT_JSON, encoding="utf-8")


def test_synthesizes_new_candidate_from_host_metrics_when_priorities_exhausted(tmp_path: Path):
    state_root = tmp_path / "state"
    _write_goal_text(state_root)
    repo = _make_git_repo_with_commit(
        tmp_path, *DONE_GIT_LOG_MESSAGES, create_files=_P7_P8_TARGET_FILES,
    )

    (state_root / "host_metrics").mkdir(parents=True)
    (state_root / "host_metrics" / "metrics.jsonl").write_text(
        json.dumps({"cpu_percent": 92, "ram_percent": 81, "disk_percent": 40}) + "\n",
        encoding="utf-8",
    )

    candidate = _synthesize_hypothesis_from_state(state_root, repo, tmp_path)
    assert candidate is not None
    assert candidate["source"] == "state_synthesized_hypothesis"
    assert candidate["signal_kind"] == "host_metrics"
    assert candidate["title"].startswith("Vector ")
    assert "Synthesize one new bounded improvement candidate" not in candidate["title"]
    assert "cpu=92" in candidate["instructions"]


def test_materialized_artifact_uses_synthesized_hypothesis_not_priority_99_meta_task(tmp_path: Path):
    state_root = tmp_path / "state"
    _write_goal_text(state_root)
    _make_git_repo_with_commit(
        tmp_path, *DONE_GIT_LOG_MESSAGES, create_files=_P7_P8_TARGET_FILES,
    )

    (state_root / "host_metrics").mkdir(parents=True)
    (state_root / "host_metrics" / "metrics.jsonl").write_text(
        json.dumps({"cpu_percent": 92}) + "\n", encoding="utf-8",
    )
    # Circular research-feed meta-task must NOT be what gets materialized even
    # though it's present as a fallback candidate.
    research_dir = state_root / "research"
    research_dir.mkdir(parents=True)
    (research_dir / "feed.json").write_text(
        json.dumps({
            "entries": [{
                "title": "Synthesize one new bounded improvement candidate from retired lanes",
                "insights": ["selection_source=generated_from_synthesized_improvement"],
            }]
        }),
        encoding="utf-8",
    )

    path = _write_materialized_improvement_artifact(
        state_root=state_root,
        cycle_id="cycle-690",
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
    assert "Synthesize one new bounded improvement candidate" not in nbc["title"]
    assert nbc["title"].startswith("Vector ")
    assert "Implement and commit Priority 99: Synthesize" not in json.dumps(artifact)


def test_dedup_rejects_already_done_title_and_tries_next_vector(tmp_path: Path):
    state_root = tmp_path / "state"
    _write_goal_text(state_root)

    (state_root / "host_metrics").mkdir(parents=True)
    (state_root / "host_metrics" / "metrics.jsonl").write_text(
        json.dumps({"cpu_percent": 92}) + "\n", encoding="utf-8",
    )

    # A commit whose message matches the Vector-1-derived title's distinctive
    # words closely enough to trip _title_already_done_in_git_log.
    repo = _make_git_repo_with_commit(
        tmp_path,
        *DONE_GIT_LOG_MESSAGES,
        "feat: self optimization constrained hardware close gap latest host metrics sample (#702)",
        create_files=_P7_P8_TARGET_FILES,
    )

    candidate = _synthesize_hypothesis_from_state(state_root, repo, tmp_path)
    assert candidate is not None
    # Vector 1 was rejected as already-done; Vector 2 was used instead.
    assert candidate["title"].startswith("Vector 2:")


def test_dedup_returns_none_when_every_combination_already_done(tmp_path: Path):
    state_root = tmp_path / "state"
    _write_goal_text(state_root)

    (state_root / "host_metrics").mkdir(parents=True)
    (state_root / "host_metrics" / "metrics.jsonl").write_text(
        json.dumps({"cpu_percent": 92}) + "\n", encoding="utf-8",
    )

    repo = _make_git_repo_with_commit(
        tmp_path,
        *DONE_GIT_LOG_MESSAGES,
        "feat: self optimization constrained hardware close gap latest host metrics sample (#702)",
        "feat: owner utility creative output close gap latest host metrics sample (#703)",
        create_files=_P7_P8_TARGET_FILES,
    )

    candidate = _synthesize_hypothesis_from_state(state_root, repo, tmp_path)
    assert candidate is None


def test_no_signals_returns_none_without_crashing(tmp_path: Path):
    state_root = tmp_path / "state"
    _write_goal_text(state_root)
    # No host_metrics/, no reports/, no failure_learning, no lessons/ directory at all.
    candidate = _synthesize_hypothesis_from_state(state_root, None, tmp_path)
    assert candidate is None


def test_missing_goal_text_returns_none(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    candidate = _synthesize_hypothesis_from_state(state_root, None, tmp_path)
    assert candidate is None


def test_research_feed_drops_self_referential_synthesize_entry():
    entry = {
        "title": "Synthesize one new bounded improvement candidate from retired lanes",
        "insights": ["cycle_id=x", "selection_source=generated_from_synthesized_improvement"],
    }
    assert _research_feed_entry_is_self_referential(entry) is True


def test_pick_candidate_from_research_feed_skips_self_referential_entry(tmp_path: Path):
    state_root = tmp_path / "state"
    research_dir = state_root / "research"
    research_dir.mkdir(parents=True)
    (research_dir / "feed.json").write_text(
        json.dumps({
            "entries": [
                {
                    "title": "Synthesize one new bounded improvement candidate from retired lanes",
                    "insights": ["selection_source=generated_from_synthesized_improvement"],
                },
                {
                    "title": "A genuinely external research candidate",
                    "action": "do the external thing",
                    "insights": ["selection_source=external_scan"],
                },
            ]
        }),
        encoding="utf-8",
    )
    candidate = _pick_candidate_from_research_feed(state_root)
    assert candidate is not None
    assert candidate["title"] == "A genuinely external research candidate"


def test_open_ended_novelty_directive_fires_once_generator_is_exhausted(tmp_path: Path):
    # Issue #695: when every (signal, vector) combination the deterministic
    # #690 generator can compose is already done in git log (the same
    # exhausted scenario as test_dedup_returns_none_when_every_combination_
    # already_done above), the loop must not fall through to the dead/legacy
    # fallbacks — it must hand the subagent a stable, open-ended directive.
    state_root = tmp_path / "state"
    _write_goal_text(state_root)

    (state_root / "host_metrics").mkdir(parents=True)
    (state_root / "host_metrics" / "metrics.jsonl").write_text(
        json.dumps({"cpu_percent": 92}) + "\n", encoding="utf-8",
    )

    repo = _make_git_repo_with_commit(
        tmp_path,
        *DONE_GIT_LOG_MESSAGES,
        "feat: self optimization constrained hardware close gap latest host metrics sample (#702)",
        "feat: owner utility creative output close gap latest host metrics sample (#703)",
        create_files=_P7_P8_TARGET_FILES,
    )

    # Confirm the deterministic generator really is exhausted first.
    assert _synthesize_hypothesis_from_state(state_root, repo, tmp_path) is None

    directive = _open_ended_novelty_directive(state_root, repo, tmp_path)
    assert directive is not None
    assert directive["source"] == "open_ended_novelty_directive"
    assert "invent" in directive["title"].lower()
    assert "genuinely NEW" in directive["instructions"]
    # Recently-done work is listed so the subagent's own invention avoids it.
    assert "already done in git log" in directive["instructions"]


def test_open_ended_novelty_directive_returns_none_without_goal_vectors(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    assert _open_ended_novelty_directive(state_root, None, tmp_path) is None


def test_open_ended_novelty_title_is_never_falsely_already_done(tmp_path: Path):
    # Issue #695: the directive's title must never itself trip the bridge's
    # _task_already_done keyword-overlap check (>=3 matching words with a
    # real, non-maintenance commit subject) — otherwise the bridge would
    # skip spawning the subagent for a directive that, by design, never
    # names concrete already-done work.
    state_root = tmp_path / "state"
    _write_goal_text(state_root)
    repo = _make_git_repo_with_commit(
        tmp_path,
        *DONE_GIT_LOG_MESSAGES,
        "feat: self optimization constrained hardware close gap latest host metrics sample (#702)",
        "feat: owner utility creative output close gap latest host metrics sample (#703)",
        "fix: bridge auto-commits uncommitted subagent work — Vector 1 gap closure",
        "feat: continuous hypothesis generation from goal vectors x state — loop never idles",
        create_files=_P7_P8_TARGET_FILES,
    )
    directive = _open_ended_novelty_directive(state_root, repo, tmp_path)
    assert directive is not None
    assert _task_already_done(directive["title"], repo) is False


def test_materialized_artifact_routes_open_ended_directive_when_generator_exhausted(tmp_path: Path):
    state_root = tmp_path / "state"
    _write_goal_text(state_root)
    repo = _make_git_repo_with_commit(
        tmp_path,
        *DONE_GIT_LOG_MESSAGES,
        "feat: self optimization constrained hardware close gap latest host metrics sample (#702)",
        "feat: owner utility creative output close gap latest host metrics sample (#703)",
        create_files=_P7_P8_TARGET_FILES,
    )
    (state_root / "host_metrics").mkdir(parents=True)
    (state_root / "host_metrics" / "metrics.jsonl").write_text(
        json.dumps({"cpu_percent": 92}) + "\n", encoding="utf-8",
    )

    path = _write_materialized_improvement_artifact(
        state_root=state_root,
        cycle_id="cycle-695-exhausted",
        goal_id="goal-bootstrap",
        current_task_id=MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
        summary="s",
        reward_signal={"value": 0.8},
        feedback_decision={"mode": "synthesize_next_candidate"},
        selfevo_repo_root=repo,
        workspace=tmp_path,
    )
    assert path is not None
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    nbc = artifact["next_bounded_candidate"]
    assert "invent" in nbc["title"].lower()
    assert not _task_already_done(nbc["title"], repo)


def test_pick_candidate_from_research_feed_returns_none_when_all_self_referential(tmp_path: Path):
    state_root = tmp_path / "state"
    research_dir = state_root / "research"
    research_dir.mkdir(parents=True)
    (research_dir / "feed.json").write_text(
        json.dumps({
            "entries": [{
                "title": "Synthesize one new bounded improvement candidate from retired lanes",
                "insights": ["selection_source=feedback_complete_active_lane"],
            }]
        }),
        encoding="utf-8",
    )
    assert _pick_candidate_from_research_feed(state_root) is None
