"""Cycle Observe phase: task/goal/insight/lesson reading and lightweight fingerprinting.

Extracted from coordinator.py (issue #600). Holds the shared module-level
constants (task-id/status vocab, experiment-budget defaults, version tags)
that the other cycle_*.py modules and coordinator.py import back, plus the
read-only helpers run_self_evolving_cycle's Observe phase relies on: task
selection/status predicates, insight ranking, goal-hypothesis parsing, and
source/prompt-mass fingerprinting. No behavior change from the move.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime._io import read_json_safe as _safe_read_json
from nanobot.utils.helpers import estimate_prompt_tokens

PROMOTION_RECORD_VERSION = 'promotion-record-v1'
PATCH_BUNDLE_VERSION = 'promotion-patch-v1'


DEFAULT_ACTIVE_GOAL = "goal-bootstrap"
GOAL_ROTATION_STREAK_LIMIT = 3
TASK_PLAN_VERSION = "task-plan-v1"
EXPERIMENT_VERSION = "experiment-v1"
EXPERIMENT_CONTRACT_VERSION = "experiment-contract-v1"
HYPOTHESIS_BACKLOG_VERSION = "hypothesis-backlog-v1"
CREDITS_LEDGER_VERSION = "credits-ledger-v1"
DEFAULT_EXPERIMENT_BUDGET = {
    "max_requests": 2,
    "max_tool_calls": 12,
    "max_subagents": 2,
    "max_timeout_seconds": 900,
}
EXPERIMENT_BUDGET_HARD_CEILING = {
    "max_requests": 5,
    "max_tool_calls": 40,
    "max_subagents": 5,
    "max_timeout_seconds": 1800,
}
EXPANDED_EXPERIMENT_BUDGET = {
    "max_requests": 4,
    "max_tool_calls": 32,
    "max_subagents": 5,
    "max_timeout_seconds": 1800,
}
LOW_REWARD_THRESHOLD = 0.5
REPEATED_BLOCK_LIMIT = 2
AMBITION_UNDERUTILIZATION_STREAK_LIMIT = 5
CORE_TASK_IDS = {
    "refresh-approval-gate",
    "verify-approval-gate",
    "run-bounded-turn",
    "record-reward",
}
SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID = "synthesize-next-improvement-candidate"
MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID = "materialize-synthesized-improvement"

# Issue #568: task IDs that make real progress on the backlog-dispatch chain, preferred
# over pure bookkeeping lanes (refresh-approval-gate/run-bounded-turn/record-reward) on lane-switch.
_BACKLOG_PROGRESSION_IDS = {
    SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
    "materialize-pass-streak-improvement",
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
    "subagent-verify-materialized-improvement",
}

COMPLETED_TASK_STATUSES = {
    "blocked",
    "canceled",
    "cancelled",
    "closed",
    "done",
    "failed",
    "terminal",
    "terminal_blocked",
    "terminal_closed",
    "terminal_failed",
    "terminal_merged",
    "terminal_noop",
}


TASK_ACTION_CLASS_BY_ID = {
    "refresh-approval-gate": "remediation",
    "verify-approval-gate": "verification",
    "run-bounded-turn": "execution",
    "record-reward": "reflection",
    "inspect-pass-streak": "review",
    "materialize-pass-streak-improvement": "execution",
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID: "execution",
    "subagent-verify-materialized-improvement": "review",
    SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID: "review",
}

# Issue #580: the closed set of task_ids the CURRENT coordinator can generate,
# select, or otherwise manage. A persisted task record whose task_id falls
# outside this set is an orphan left behind by removed code (e.g. a task
# generator that existed on a prior revision and was later deleted) and can
# never be produced or progressed by the running coordinator again — see
# _retire_orphaned_task_ids. Derived from the existing constants plus the
# handful of task_ids that are only ever emitted as literal strings (not
# module-level constants), so this can't silently drift below what the code
# actually emits.
KNOWN_TASK_IDS: frozenset[str] = frozenset(
    CORE_TASK_IDS
    | _BACKLOG_PROGRESSION_IDS
    | set(TASK_ACTION_CLASS_BY_ID)
    | {
        "analyze-last-failed-candidate",
        "diagnose-blocker",
        "execute-queued-revert",
    }
)



def _json_files_sorted_by_mtime(desc: bool, *dirs: Path):
    """Yield (path, mtime) for all *.json files in *dirs*, sorted by mtime.

    Uses os.scandir() to avoid the double-stat penalty of
    ``path.is_file() + path.stat()`` — scandir caches the stat result
    from the directory entry, cutting syscalls in half for large
    subagent directories (143+ files → 143 stat calls instead of 286).
    """
    pairs: list[tuple[Path, float]] = []
    for d in dirs:
        if not d.exists():
            continue
        try:
            with os.scandir(str(d)) as it:
                for entry in it:
                    if entry.name.endswith('.json') and entry.is_file():
                        pairs.append((d / entry.name, entry.stat().st_mtime))
        except OSError:
            continue
    pairs.sort(key=lambda p: p[1], reverse=desc)
    yield from pairs


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_artifact_paths(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if item not in (None, ""))
    return (str(value),)


def _task_action_class(task_id: str | None) -> str:
    if not task_id:
        return "unknown"
    return TASK_ACTION_CLASS_BY_ID.get(str(task_id), "other")


def _task_status(task: dict[str, Any] | None) -> str:
    if not isinstance(task, dict):
        return ""
    return str(task.get("status") or "pending").strip().lower()


def _task_is_selectable(task: dict[str, Any] | None) -> bool:
    status = _task_status(task)
    if status in COMPLETED_TASK_STATUSES:
        return False
    if isinstance(task, dict):
        task_id = task.get("task_id") or task.get("taskId")
        if task_id and str(task_id) not in KNOWN_TASK_IDS:
            return False
    return True


def _retire_orphaned_task_ids(task_records: list[dict[str, Any]]) -> int:
    """Mark task records with an unrecognized task_id as retired in place.

    Issue #580: task_ids left over from removed code (the current coordinator
    can no longer generate or progress them) must not stay selectable forever
    — otherwise fallback task-selection logic can ping-pong on a dead task
    indefinitely. Returns the number of records retired.
    """
    retired = 0
    for task in task_records:
        if not isinstance(task, dict):
            continue
        task_id = task.get("task_id") or task.get("taskId")
        if not task_id or str(task_id) in KNOWN_TASK_IDS:
            continue
        if _task_status(task) in COMPLETED_TASK_STATUSES:
            continue
        task["status"] = "canceled"
        task["terminal_reason"] = "orphaned_unrecognized_task_id"
        retired += 1
    return retired


def _task_is_terminal_selfevo_retired(task: dict[str, Any] | None, terminal_selfevo_issue: dict[str, Any] | None) -> bool:
    if not isinstance(task, dict) or not isinstance(terminal_selfevo_issue, dict):
        return False
    if task.get("task_id") != "analyze-last-failed-candidate":
        return False

    terminal_status = str(terminal_selfevo_issue.get("terminal_status") or "").strip().lower()
    if not terminal_status:
        return False

    task_status = _task_status(task)
    terminal_reason = str(task.get("terminal_reason") or "").strip().lower()
    return task_status in COMPLETED_TASK_STATUSES and (task_status == terminal_status or terminal_reason == terminal_status)


def _task_has_recorded_terminal_selfevo_retirement(task: dict[str, Any] | None) -> bool:
    if not isinstance(task, dict):
        return False
    if task.get("task_id") != "analyze-last-failed-candidate":
        return False
    task_status = _task_status(task)
    terminal_reason = str(task.get("terminal_reason") or "").strip().lower()
    return task_status in COMPLETED_TASK_STATUSES and terminal_reason.startswith("terminal_")


def _render_task_selection(task: dict[str, Any]) -> str:
    task_id = task.get("task_id") or task.get("taskId")
    task_title = task.get("title") or task.get("summary") or task_id or "task"
    if task_id:
        return f"{task_title} [task_id={task_id}]"
    return str(task_title)


def _task_title_for_id(task_id: str | None, *task_sets: list[dict[str, Any]]) -> str | None:
    if not task_id:
        return None
    for task_set in task_sets:
        for task in task_set:
            if not isinstance(task, dict):
                continue
            candidate_id = task.get("task_id") or task.get("taskId")
            if candidate_id == task_id:
                return task.get("title") or task.get("summary") or str(task_id)
    return str(task_id)


def _pick_task_for_classes(
    task_records: list[dict[str, Any]],
    current_task_id: str | None,
    preferred_classes: list[str],
) -> dict[str, Any] | None:
    for preferred_class in preferred_classes:
        for task in task_records:
            task_id = task.get("task_id") or task.get("taskId")
            if task_id == current_task_id:
                continue
            if _task_action_class(task_id) == preferred_class:
                return task
    for task in task_records:
        task_id = task.get("task_id") or task.get("taskId")
        if task_id != current_task_id:
            return task
    return None


def _freshest_reusable_insight(workspace: Path) -> str | None:
    """Return the most recent non-empty reusable insight from the lessons DB.

    Closes the HADI Insight -> next-Hypothesis arc: accumulated insights become
    the seed for the next synthesized improvement candidate instead of a static
    template, so an empty backlog is no longer a terminal stall state while
    insights exist. Defensive — never raises; returns None when none available.
    """
    try:
        from nanobot.runtime.lessons import LessonsDB

        for lesson in LessonsDB(workspace).load_lessons():
            if not isinstance(lesson, dict):
                continue
            text = str(
                lesson.get("reusable_insight") or lesson.get("generalized_insight") or ""
            ).strip()
            if text:
                return text
    except Exception:
        return None
    return None


def _lesson_insight_text(lesson: dict[str, Any]) -> str:
    return str(
        lesson.get("reusable_insight") or lesson.get("generalized_insight") or ""
    ).strip()


def _lesson_reward_value(lesson: dict[str, Any]) -> float:
    """Extract the reward magnitude embedded in a lesson by lessons.py.

    lessons.py stores the cycle reward in the lesson body (e.g. impact
    "Positive reward signal: 1.2" and reusable_insight "... reward=1.2 ..."),
    so we recover it without any lesson-schema change. Returns 0.0 when absent.
    """
    import re as _re

    for field in ("impact", "reusable_insight", "approach"):
        text = str(lesson.get(field) or "")
        match = _re.search(
            r"reward(?:\s*signal)?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", text, _re.IGNORECASE
        )
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return 0.0


def _goal_relevance_tokens(goal_id: str | None) -> set[str]:
    import re as _re

    return {w.lower() for w in _re.findall(r"[A-Za-z]{4,}", str(goal_id or ""))}


def _rank_insights_for_goal(
    workspace: Path, goal_id: str | None, *, top_n: int = 3
) -> list[str]:
    """Rank reusable insights by goal relevance, reward, then recency.

    Closes the I->H arc more sharply than "freshest wins": a newer but
    off-goal / low-reward insight should not crowd out a more relevant,
    higher-reward one. Goal relevance dominates (weight 10), reward is the
    secondary signal, recency the final tiebreak. Defensive — never raises.
    """
    try:
        from nanobot.runtime.lessons import LessonsDB

        lessons = [item for item in LessonsDB(workspace).load_lessons() if isinstance(item, dict)]
    except Exception:
        return []
    if not lessons:
        return []

    goal_tokens = _goal_relevance_tokens(goal_id)
    total = len(lessons)
    scored: list[tuple[float, int, str]] = []
    for idx, lesson in enumerate(lessons):  # load_lessons() is newest-first
        insight = _lesson_insight_text(lesson)
        if not insight:
            continue
        haystack = f"{lesson.get('title') or ''} {insight}".lower()
        relevance = sum(1 for token in goal_tokens if token in haystack)
        reward = _lesson_reward_value(lesson)
        recency = (total - idx) / total  # (0, 1], newest highest
        score = 10.0 * relevance + reward + recency
        scored.append((score, idx, insight))

    scored.sort(key=lambda item: (-item[0], item[1]))  # score desc, newest-first tiebreak
    return [insight for _, _, insight in scored[:top_n]]


def _select_insight_for_goal(workspace: Path, goal_id: str | None) -> str | None:
    """Return the single best insight for the active goal (relevance+reward+recency).

    Falls back to the freshest insight in the degenerate all-zero case (the
    ranker's recency term already yields that ordering).
    """
    ranked = _rank_insights_for_goal(workspace, goal_id, top_n=1)
    if ranked:
        return ranked[0]
    return _freshest_reusable_insight(workspace)


def _insight_is_actionable(text: str | None) -> bool:
    """True if the insight names a concrete artifact (a path/file) to change.

    A loop running metadata-only cycles tends to generate vague lessons
    ("Consolidate this optimization pattern") that give a materialize subagent no
    concrete target. Those are not actionable; an insight naming a source file is.
    """
    if not text:
        return False
    import re as _re

    return bool(_re.search(r"[\w./-]+\.(py|md|ya?ml|json|sh|ts|js)\b", text))


def _next_open_goal_hypothesis(workspace: Path | None) -> str | None:
    """Return the top open goal from the repo todo.md as a concrete hypothesis.

    When the loop has no actionable insight, its own goals (todo.md, shipped in the
    workspace/release) are the autoresearch-style concrete target so it self-evolves
    on OUR goals instead of looping on vague meta-lessons. Returns the first
    unchecked `- [ ] N. Title` item plus its Problem: line. Defensive — never raises.
    """
    if workspace is None:
        return None
    import re as _re

    try:
        text = (workspace / "todo.md").read_text(encoding="utf-8")
    except Exception:
        return None
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        match = _re.match(r"\s*-\s*\[ \]\s*\d*\.?\s*(.+)", line)
        if not match:
            continue
        title = match.group(1).strip()
        detail = ""
        for follow in lines[idx + 1: idx + 8]:
            problem = _re.search(r"Problem:\s*(.+)", follow)
            if problem:
                detail = problem.group(1).strip()
                break
        return f"{title}. {detail}".strip() if detail else title
    return None


def _next_open_goal_as_backlog_task(workspace: Path | None) -> dict[str, Any] | None:
    """Return the top open todo.md goal as a concrete backlog task to implement.

    Shape matches _parse_backlog_task_from_memory: {'title', 'instructions', 'priority'}.
    This routes OUR goals into the materialized artifact's next_bounded_candidate
    (which the bridge subagent reads with imperative "implement and commit"
    instructions) when the MEMORY backlog is empty — so the loop executes our goals
    instead of a stale research-feed candidate. Defensive — never raises.
    """
    if workspace is None:
        return None
    import re as _re

    try:
        lines = (workspace / "todo.md").read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for idx, line in enumerate(lines):
        match = _re.match(r"\s*-\s*\[ \]\s*(\d+)?\.?\s*(.+)", line)
        if not match:
            continue
        priority = match.group(1)
        title = match.group(2).strip()
        # Collect the indented detail block until the next top-level item / heading.
        body: list[str] = []
        for follow in lines[idx + 1:]:
            if follow.startswith("## ") or _re.match(r"-\s*\[[ x~]\]", follow):
                break
            if follow.strip():
                body.append(follow.strip())
        instructions = " ".join(body)[:800] or f"Implement the improvement: {title}"
        return {
            "title": title,
            "instructions": instructions,
            "priority": int(priority) if priority and priority.isdigit() else None,
        }
    return None


def _git_output(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)
        text = (result.stdout or '').strip()
        return text or None
    except Exception:
        return None


def _observed_product_head_source_fingerprint(workspace: Path) -> dict[str, Any] | None:
    current_state_path = workspace / "state" / "self_evolution" / "current_state.json"
    payload = _safe_read_json(current_state_path)
    if not isinstance(payload, dict):
        return None
    observed = payload.get("observed_product_head") if isinstance(payload.get("observed_product_head"), dict) else {}
    commit = observed.get("commit") or payload.get("product_head")
    if not commit:
        return None
    return {
        "source_repo_root": observed.get("repo_root") or str(workspace),
        "source_commit": commit,
        "source_branch": observed.get("branch"),
        "source_tree": observed.get("tree"),
        "source_authority": "observed_product_head",
    }


def _release_metadata_source_fingerprint(search_roots: list[Path]) -> dict[str, Any] | None:
    """Read source provenance from archive/release metadata when .git is absent.

    eeepc deploys the system emitter from git archives, so the runtime tree has no
    `.git` directory.  A release-side `SOURCE_COMMIT` file is the auditable source
    of truth for those pinned trees and must win over unrelated cwd git repos.
    """

    def _read_first(root: Path, names: tuple[str, ...]) -> str | None:
        for name in names:
            path = root / name
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value:
                return value
        return None

    for root in search_roots:
        commit = _read_first(root, ("SOURCE_COMMIT", "REVISION", "COMMIT"))
        if not commit:
            continue
        return {
            "source_repo_root": str(root),
            "source_commit": commit,
            "source_branch": _read_first(root, ("SOURCE_BRANCH", "BRANCH")),
            "source_tree": _read_first(root, ("SOURCE_TREE", "TREE")),
            "source_authority": "release_metadata",
        }
    return None


def _runtime_source_fingerprint(workspace: Path) -> dict[str, Any]:
    env_commit = os.environ.get('NANOBOT_SOURCE_COMMIT') or os.environ.get('SOURCE_COMMIT')
    if env_commit:
        return {
            'source_repo_root': os.environ.get('NANOBOT_SOURCE_REPO_ROOT') or os.environ.get('SOURCE_REPO_ROOT') or str(Path(__file__).resolve().parents[2]),
            'source_commit': env_commit,
            'source_branch': os.environ.get('NANOBOT_SOURCE_BRANCH') or os.environ.get('SOURCE_BRANCH'),
            'source_tree': os.environ.get('NANOBOT_SOURCE_TREE') or os.environ.get('SOURCE_TREE'),
            'source_authority': 'environment',
        }
    search_roots = [workspace, Path(__file__).resolve().parents[2], Path.cwd()]
    release_metadata = _release_metadata_source_fingerprint(search_roots)
    if release_metadata:
        return release_metadata
    for candidate_root in search_roots:
        repo_root = candidate_root
        while repo_root != repo_root.parent and not (repo_root / '.git').exists():
            repo_root = repo_root.parent
        if (repo_root / '.git').exists():
            commit = _git_output(['git', 'rev-parse', 'HEAD'], repo_root)
            branch = _git_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], repo_root)
            tree = _git_output(['git', 'rev-parse', 'HEAD^{tree}'], repo_root)
            if commit:
                return {
                    'source_repo_root': str(repo_root),
                    'source_commit': commit,
                    'source_branch': branch,
                    'source_tree': tree,
                    'source_authority': 'git',
                }
    observed_fingerprint = _observed_product_head_source_fingerprint(workspace)
    if observed_fingerprint:
        return observed_fingerprint
    return {
        'source_repo_root': str(workspace),
        'source_commit': None,
        'source_branch': None,
        'source_tree': None,
    }


def _prompt_mass_snapshot(
    *,
    selected_tasks: str,
    current_plan: dict[str, Any],
    hypothesis_backlog: dict[str, Any],
) -> dict[str, Any]:
    proposal_parts = {
        'selected_tasks': selected_tasks,
        'task_plan': current_plan,
        'hypothesis_backlog': hypothesis_backlog,
    }
    text_payload = json.dumps(proposal_parts, ensure_ascii=False)
    estimated_tokens = estimate_prompt_tokens([
        {'role': 'user', 'content': text_payload},
    ])
    char_count = len(text_payload)
    if estimated_tokens > 16000:
        risk = 'high'
    elif estimated_tokens > 8000:
        risk = 'medium'
    else:
        risk = 'low'
    return {
        'bytes': len(text_payload.encode('utf-8')),
        'chars': char_count,
        'estimated_tokens': estimated_tokens,
        'risk': risk,
    }


def _load_recent_history_entries(history_dir: Path, limit: int = 4) -> list[dict[str, Any]]:
    if not history_dir.exists():
        return []
    # Use os.scandir-based helper to avoid double-stat penalty of glob()+stat().
    history_files = [p for p, _ in _json_files_sorted_by_mtime(True, history_dir) if p.name.startswith("cycle-")][:limit]
    entries: list[dict[str, Any]] = []
    for path in history_files:
        payload = _safe_read_json(path)
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def _resolve_runtime_state_root(workspace: Path) -> Path:
    from nanobot.runtime.state import resolve_runtime_state_root

    return resolve_runtime_state_root(workspace)
