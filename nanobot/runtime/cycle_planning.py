"""Cycle Planning phase: task-plan-snapshot generation pipeline.

Extracted from coordinator.py (issue #600). Holds ``_build_task_plan_snapshot``
and its helpers: candidate generation (failure-learning, curriculum/backlog
parsing, research-feed), subagent request/verification-id materialization,
and research-feed writing. Depends on nanobot.runtime.cycle_observe and
nanobot.runtime.cycle_feedback for shared task/insight/experiment helpers.
No behavior change from the move.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime._io import read_json_safe as _safe_read_json
from nanobot.runtime.autoevolve import resolve_terminal_selfevo_issue
from nanobot.runtime.cycle_feedback import (
    _derive_mutation_lane,
    _derive_reward_signal,
    _synthesized_materialize_improvement_candidate,
    _synthesized_next_improvement_candidate,
    _task_readiness_contract,
    _task_readiness_gate,
)
from nanobot.runtime.cycle_observe import (
    _TERMINAL_SUBAGENT_RESULT_STATUSES,
    CORE_TASK_IDS,
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
    SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
    TASK_PLAN_VERSION,
    _insight_is_actionable,
    _json_files_sorted_by_mtime,
    _load_recent_history_entries,
    _next_open_goal_as_backlog_task,
    _render_task_selection,
    _resolve_runtime_state_root,
    _select_insight_for_goal,
    _task_action_class,
    _task_has_recorded_terminal_selfevo_retirement,
    _task_is_selectable,
    _task_is_terminal_selfevo_retired,
    _task_title_for_id,
)
from nanobot.runtime.subagent_materializer import _result_path_for

_logger = logging.getLogger(__name__)

# Issue #739: deterministic-planner minting kill-switch. Default "1" (unset
# behaves identically) preserves today's behavior byte-for-byte — this ships
# inert, same rollout pattern as SELFEVO_LLM_PROPOSER_ENABLED (#707). Setting
# "0" stops the deterministic planner from minting NEW subagent-verify
# requests at its two write call sites (_write_subagent_request_artifact and
# _ensure_verify_request_for_fresh_materialization); everything else in the
# coordinator cycle (goals, reports, learning, HADI bookkeeping) is
# untouched, and callers already tolerate these functions returning None.
DETERMINISTIC_PLANNER_ENABLED_ENV = "SELFEVO_DETERMINISTIC_PLANNER_ENABLED"


def _deterministic_planner_enabled() -> bool:
    """Return whether the deterministic planner may mint new requests.

    Mirrors the style of ``SELFEVO_LLM_PROPOSER_ENABLED`` in
    ``nanobot.runtime.llm_proposer``: a single small env-backed helper, no
    other config surface. Any value other than the literal ``"0"`` (absent,
    ``"1"``, or garbage) preserves current behavior.
    """
    return os.environ.get(DETERMINISTIC_PLANNER_ENABLED_ENV, "1").strip() != "0"


def _productive_subagent_request_ids(state_root: Path) -> set[str]:
    """Issue #697 idle backstop: request_ids of live subagent-verify-materialized-
    improvement / materialize-pass-streak-improvement requests that are NOT a
    mere already_done short-circuit (mirrors the existing already_done
    distinction in _TERMINAL_SUBAGENT_RESULT_STATUSES, cycle_observe.py). A
    request counts as "productive" if it has no result yet (still live/in
    flight) or a terminal result other than already_done. Used to detect
    whether THIS cycle spawned genuine new subagent work, resetting the
    cycles_since_productive_spawn counter.
    """
    ids: set[str] = set()
    request_dir = state_root / "subagents" / "requests"
    result_dir = state_root / "subagents" / "results"
    if not request_dir.exists():
        return ids
    for path, _mtime in _json_files_sorted_by_mtime(True, request_dir):
        payload = _safe_read_json(path)
        if not isinstance(payload, dict) or payload.get("task_id") not in {
            "subagent-verify-materialized-improvement",
            "materialize-pass-streak-improvement",
        }:
            continue
        request_id = str(payload.get("request_id") or path.stem)
        result_path = _result_path_for(result_dir, path, payload)
        result_payload = _safe_read_json(result_path) if result_path.exists() else None
        result_status = result_payload.get("result_status") if isinstance(result_payload, dict) else None
        if result_status == "already_done":
            continue
        ids.add(request_id)
    return ids


def _parse_backlog_task_from_goal_text(
    state_root: Path, selfevo_repo_root: Path | None = None
) -> dict[str, Any] | None:
    """Read the lowest-numbered open (not-already-done) priority from state/goals/goal_text.json.

    goal_text.json is freshly seeded on every deploy with the operator's actual
    current priorities (see deploy_release.sh), but was previously only used by
    the bridge to build an LLM prompt string — never read by the coordinator.
    Shape matches _parse_backlog_task_from_memory: {'priority', 'title', 'instructions'}.
    Defensive — never raises; returns None if missing/unreadable/malformed.

    Issue #575: a priority is skipped (treated as already done) when its title's
    keywords match recent commits in selfevo_repo_root's git log, via the same
    heuristic used for the MEMORY.md backlog (_title_already_done_in_git_log).
    If selfevo_repo_root is None or not a valid git directory, every priority is
    treated as not-done (fail-open, matching the MEMORY.md path's behavior when
    git access fails).
    """
    goal_text_path = state_root / "goals" / "goal_text.json"
    if not goal_text_path.exists():
        return None
    try:
        raw = goal_text_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    text = data.get("text")
    if not isinstance(text, str):
        return None

    import re as _re

    marker = "Current priority targets:"
    marker_idx = text.find(marker)
    if marker_idx == -1:
        return None
    section = text[marker_idx + len(marker):]

    matches = _re.findall(
        r"\([A-Za-z]\)\s*Priority\s+(\d+)\s*[—-]\s*(.+?):\s*(.+?)(?=\n\([A-Za-z]\)|\Z)",
        section,
        _re.DOTALL,
    )
    if not matches:
        return None

    git_log = ""
    if selfevo_repo_root is not None and selfevo_repo_root.is_dir():
        git_log = _recent_git_log(selfevo_repo_root)

    for num, title, instructions in sorted(matches, key=lambda m: int(m[0])):
        title = title.strip()
        if git_log and _title_already_done_in_git_log(title, git_log):
            continue  # treat as done — skip to next lowest-numbered priority
        return {
            "priority": int(num),
            "title": title,
            "instructions": instructions.strip()[:300],
            "source": "goal_text",
        }
    return None  # all found priorities already done


def filter_completed_priorities_from_goal_text(
    raw_text: str, selfevo_repo_root: Path | None
) -> str:
    """Rewrite goal_text.json's raw "text" to move already-done priorities out of
    the "Current priority targets:" section and into a "Completed (do not
    repeat)" sentence, before it is injected verbatim into the bridge's subagent
    prompt (issue #712).

    Root cause: the bridge (bridge.py) injects goal_text.json's raw "text" into
    the subagent prompt as-is. The deterministic coordinator path already skips
    done priorities via the #575 git-log heuristic
    (_parse_backlog_task_from_goal_text / _title_already_done_in_git_log), but
    that heuristic was never applied to the raw prompt text itself — so a
    priority the coordinator has already marked done keeps being shown to the
    subagent as a live "Current priority target" every cycle, causing it to be
    re-proposed (novelty collapse, per the #711 shadow run).

    Reuses the exact same "Current priority targets:" regex as
    `_parse_backlog_task_from_goal_text` to enumerate priority entries, and the
    same `_recent_git_log` / `_title_already_done_in_git_log` helpers to decide
    done-ness — no new done-detection logic. Fail-open (matching this module's
    existing convention): returns `raw_text` unchanged if `selfevo_repo_root` is
    None/not a directory, the marker/regex don't match, or on any exception.
    """
    try:
        if selfevo_repo_root is None or not selfevo_repo_root.is_dir():
            return raw_text
        if not isinstance(raw_text, str):
            return raw_text

        import re as _re

        marker = "Current priority targets:"
        marker_idx = raw_text.find(marker)
        if marker_idx == -1:
            return raw_text
        section_start = marker_idx + len(marker)
        section = raw_text[section_start:]

        pattern = r"\([A-Za-z]\)\s*Priority\s+(\d+)\s*[—-]\s*(.+?):\s*(.+?)(?=\n\([A-Za-z]\)|\Z)"
        matches = list(_re.finditer(pattern, section, _re.DOTALL))
        if not matches:
            return raw_text

        git_log = _recent_git_log(selfevo_repo_root)
        if not git_log:
            return raw_text

        kept_entries: list[str] = []
        done_titles: list[str] = []
        for m in matches:
            title = m.group(2).strip()
            entry_text = m.group(0)
            if _title_already_done_in_git_log(title, git_log):
                done_titles.append(title)
            else:
                kept_entries.append(entry_text.rstrip("\n"))

        if not done_titles:
            return raw_text  # nothing to move — leave text byte-identical

        new_section = "\n" + "\n".join(kept_entries) if kept_entries else "\n"
        new_text = raw_text[:section_start] + new_section

        if "Completed (do not repeat):" in new_text:
            new_text = new_text.replace(
                "Completed (do not repeat):",
                "Completed (do not repeat): " + "; ".join(done_titles) + ";",
                1,
            )
        else:
            completed_sentence = "Completed (do not repeat): " + "; ".join(done_titles) + "."
            new_text = new_text.rstrip("\n") + "\n\n" + completed_sentence
        return new_text
    except Exception:
        return raw_text


def _latest_failure_learning(workspace: Path) -> dict[str, Any] | None:
    candidate_paths = []
    try:
        candidate_paths.append(_resolve_runtime_state_root(workspace) / 'self_evolution' / 'failure_learning' / 'latest.json')
    except Exception:
        pass
    candidate_paths.append(workspace / 'state' / 'self_evolution' / 'failure_learning' / 'latest.json')
    seen: set[Path] = set()
    for path in candidate_paths:
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        try:
            mtime = path.stat().st_mtime
            age_seconds = max(0, int(datetime.now(timezone.utc).timestamp() - mtime))
        except Exception:
            age_seconds = None
        data['_age_seconds'] = age_seconds
        data['_source_path'] = str(path)
        return data
    return None


def _derive_generated_candidates(
    *,
    goals_dir: Path,
    result_status: str,
    current_task_id: str | None,
    failure_learning: dict[str, Any] | None = None,
    retire_analyze_last_failed_candidate: bool = False,
) -> list[dict[str, Any]]:
    history_entries = _load_recent_history_entries(goals_dir / "history", limit=6)
    if result_status != "PASS":
        return []
    pass_streak = 0
    for entry in history_entries:
        if (entry.get("result_status") or entry.get("status")) == "PASS":
            pass_streak += 1
        else:
            break
    candidates: list[dict[str, Any]] = []
    if isinstance(failure_learning, dict) and not retire_analyze_last_failed_candidate:
        candidates.append({
            'task_id': 'analyze-last-failed-candidate',
            'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
            'status': 'pending',
            'kind': 'review',
            'acceptance': 'produce a bounded explanation of the failed candidate and one safer follow-up mutation idea',
            'selection_source': 'generated_from_failure_learning',
            'failed_candidate_id': failure_learning.get('candidate_id'),
            'failed_commit': failure_learning.get('failed_commit'),
            'health_reasons': failure_learning.get('health_reasons'),
        })
    if current_task_id == "inspect-pass-streak":
        candidates.append({
            "task_id": "materialize-pass-streak-improvement",
            "title": "Materialize one concrete bounded improvement from the repeated PASS insight",
            "status": "pending",
            "kind": "execution",
            "acceptance": "produce one concrete bounded follow-up candidate derived from the inspect-pass-streak review",
            "selection_source": "generated_from_inspect_pass_streak",
            "parent_task_id": "inspect-pass-streak",
        })
    elif current_task_id in {"materialize-pass-streak-improvement", MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID}:
        candidates.append({
            "task_id": "subagent-verify-materialized-improvement",
            "title": "Use one bounded subagent-assisted review to verify the materialized improvement artifact",
            "status": "pending",
            "kind": "review",
            "acceptance": "create one bounded subagent request that reviews the materialized improvement artifact and reports a verification recommendation",
            "selection_source": "generated_from_hadi_materialized_improvement" if current_task_id == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID else "generated_from_materialized_improvement",
            "parent_task_id": current_task_id,
            "subagent_profile": "bounded_execution",
            "subagent_budget": "standard",
        })
    elif current_task_id == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID and pass_streak >= 3:
        candidates.append({
            "task_id": MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
            "title": "Materialize one bounded improvement from the synthesized candidate",
            "status": "pending",
            "kind": "execution",
            "acceptance": "write a concrete bounded improvement proposal or artifact and route it into self-evolution",
            "selection_source": "generated_from_synthesized_improvement",
            "parent_task_id": SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
            "strong_pass_count": pass_streak,
            "hadi_required": True,
            "hadi_cycle": {
                "hypothesis": "A concrete bounded materialization will break the synthesized review discard loop.",
                "action": "Materialize the synthesized candidate into a reviewable artifact with explicit acceptance checks.",
                "data": "Use repeated PASS/discard history and underutilized budget evidence.",
                "insight": "Route the materialized artifact to delegated verification or block it with a concrete reason.",
            },
            "task_readiness": _task_readiness_contract(
                definition_of_ready=[
                    "HADI hypothesis/action/data/insight is attached",
                    "acceptance defines a concrete durable artifact",
                    "DoD includes delegated verification or explicit blocker evidence",
                ],
                definition_of_done=[
                    "materialized improvement artifact exists",
                    "artifact is correlated to the source synthesized candidate",
                    "subagent verification request/result/blocker path is recorded",
                ],
            ),
        })
    elif pass_streak >= 3 and current_task_id != "inspect-pass-streak":
        candidates.append({
            "task_id": "inspect-pass-streak",
            "title": "Inspect repeated PASS streak for a new bounded improvement",
            "status": "pending",
            "kind": "review",
            "acceptance": "derive one new bounded improvement candidate from repeated PASS evidence",
            "selection_source": "generated_pass_streak",
            "pass_streak": pass_streak,
        })
    return candidates


def _inferred_generated_candidates_from_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inferred: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = task.get("task_id") or task.get("taskId")
        if not task_id or task_id in CORE_TASK_IDS or not _task_is_selectable(task):
            continue
        inferred.append({
            "task_id": task_id,
            "title": task.get("title") or task.get("summary") or str(task_id),
            "status": task.get("status") or "pending",
            "kind": task.get("kind") or _task_action_class(str(task_id)),
            "acceptance": task.get("acceptance"),
            "selection_source": task.get("selection_source") or "carried_forward_task_plan",
        })
    return inferred


def _recent_git_log(repo_root: Path, since: str = "14 days ago") -> str:
    """Return `git log --oneline --since=<since>` output for repo_root, or "" on any failure.

    Shared helper: both `_curriculum_level` (MEMORY.md backlog) and
    `_parse_backlog_task_from_goal_text` (goal_text.json priorities) need
    "recent git log text for a repo" to feed the done-detection heuristic (#575).
    """
    import subprocess as _sp

    git_cmd = [
        "git", "-c", f"safe.directory={repo_root}",
        "-C", str(repo_root),
        "log", "--oneline", f"--since={since}",
    ]
    try:
        return _sp.check_output(git_cmd, stderr=_sp.DEVNULL, timeout=10).decode(errors="replace")
    except Exception:
        return ""


def _title_already_done_in_git_log(title: str, git_log: str) -> bool:
    """Return True if some SINGLE commit line contains a proportional share of title words.

    Shared heuristic: a priority/backlog title is treated as already completed
    when its distinctive words show up together in one recent commit message,
    even if the priority itself carries no explicit [Done] marker (used for
    both the MEMORY.md backlog curriculum and goal_text.json priority parsing
    — #575).

    #592: the original rule counted a title as done when >=2 of its words (4+
    chars) appeared ANYWHERE in the whole multi-day git log, pooling matches
    across unrelated commits. The autonomous bot commits ~70+ times/24h with a
    narrow, repetitive commit vocabulary ("write", "scripts", "test", "subagent",
    "queue", "dashboard", ...), so that pooled-anywhere check saturates and
    produces false positives (a title's words each individually appear in some
    commit, even though no single commit is actually about that title). The fix
    requires a proportional share of the title's words to appear together on
    ONE commit line: at least `max(2, ceil(0.6 * len(words)))` of them,
    matching per-word substring containment as before.
    """
    import math as _math
    import re as _re

    if not git_log:
        return False
    words = [w.lower() for w in _re.findall(r'[A-Za-z]{4,}', title)]
    if len(words) < 2:
        return False
    threshold = max(2, _math.ceil(0.6 * len(words)))
    for line in git_log.splitlines():
        line_lower = line.lower()
        matches = sum(1 for w in words if w in line_lower)
        if matches >= threshold:
            return True
    return False


def _curriculum_level(selfevo_repo_root: Path) -> int:
    """Return the curriculum level: the priority number of the first non-Done backlog item.

    Checks both [Done] marker in title AND git log (via a simple search for the title words
    in recent commits).  Returns 9999 if all priorities are done (backlog exhausted).
    Inspired by Darwin Mode curriculum.ts: only admit difficulty <= L, raise L when mastered.
    """
    memory_path = selfevo_repo_root / "memory" / "MEMORY.md"
    if not memory_path.exists():
        return 9  # default: start at P9
    try:
        text = memory_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 9

    import re as _re

    backlog_match = _re.search(r"## (?:Concrete backlog|Active backlog).*?(?=\n## |\Z)", text, _re.DOTALL)
    if not backlog_match:
        return 9
    backlog_text = backlog_match.group(0)

    priority_blocks = _re.findall(
        r"###\s+Priority\s+(\d+):\s+(.+?)\n(.*?)(?=###\s+Priority|\Z)",
        backlog_text,
        _re.DOTALL,
    )

    git_log = _recent_git_log(selfevo_repo_root)

    for num, title, _body in priority_blocks:
        title = title.strip()
        # Done by explicit marker
        if _re.search(r"\[Done\]", title, _re.IGNORECASE):
            continue
        # Done by git log: ≥2 keywords (4+ chars) found in recent commits
        if _title_already_done_in_git_log(title, git_log):
            continue
        return int(num)
    return 9999  # all done


def _parse_backlog_task_from_memory(selfevo_repo_root: Path) -> dict[str, Any] | None:
    """Read the first incomplete priority from memory/MEMORY.md in eeebot-self-evolving.

    Returns a dict with 'title', 'instructions', 'priority' or None if unavailable.
    Enforces curriculum ordering: only returns the task at the current curriculum level.
    """
    memory_path = selfevo_repo_root / "memory" / "MEMORY.md"
    if not memory_path.exists():
        return None
    try:
        text = memory_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    import re as _re
    # Find the backlog section (supports both legacy "Concrete backlog" and new "Active backlog" headings)
    backlog_match = _re.search(r"## (?:Concrete backlog|Active backlog).*?(?=\n## |\Z)", text, _re.DOTALL)
    if not backlog_match:
        return None
    backlog_text = backlog_match.group(0)

    # Find priority blocks: ### Priority N: <title>
    priority_blocks = _re.findall(
        r"###\s+Priority\s+(\d+):\s+(.+?)\n(.*?)(?=###\s+Priority|\Z)",
        backlog_text,
        _re.DOTALL,
    )
    for num, title, body in priority_blocks:
        title = title.strip()
        body = body.strip()
        # Skip if marked as Done — check ONLY the title line (not body, which may reference [Done])
        if _re.search(r"\[Done\]", title, _re.IGNORECASE):
            continue
        # Curriculum gate: only return task at or below current curriculum level
        curr_level = _curriculum_level(selfevo_repo_root)
        if int(num) > curr_level:
            # Higher-difficulty task blocked until current level is mastered
            _logger.debug(
                "[curriculum] blocking Priority %s (current level=%s) — master P%s first",
                num, curr_level, curr_level,
            )
            return None
        # Extract first meaningful instruction line (not empty, not a label)
        instructions_lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        instructions = " ".join(instructions_lines[:3])  # first 3 lines as summary
        return {
            "priority": int(num),
            "title": title,
            "instructions": instructions,
            "curriculum_level": curr_level,
        }
    return None


def _research_feed_entry_is_self_referential(entry: dict[str, Any]) -> bool:
    """True if a research/feed.json entry is the pipeline's own meta-task template.

    Issue #690: feed.json entries are written by ``_write_research_feed`` straight
    from ``_derive_generated_candidates``'s generic review/materialize templates
    (e.g. "Synthesize one new bounded improvement candidate from retired lanes...").
    Feeding those back into ``_pick_candidate_from_research_feed`` as "new"
    content is circular — the subagent is handed a description of the pipeline's
    own review step, which it cannot meaningfully commit, so the lane never
    completes. Detect this via the known circular title substring and via the
    ``selection_source=`` marker embedded in the entry's ``insights`` list (set
    by ``_write_research_feed`` from each candidate's ``selection_source``):
    anything starting ``generated_from_``, ``feedback_``, or ``retire_`` is an
    internal pipeline artifact, not externally-sourced research.
    """
    title = str(entry.get("title") or "")
    if "Synthesize one new bounded improvement candidate" in title:
        return True
    for insight in entry.get("insights") or []:
        insight_str = str(insight)
        if insight_str.startswith("selection_source="):
            source = insight_str.split("=", 1)[1]
            if source.startswith(("generated_from_", "feedback_", "retire_")):
                return True
    return False


def _pick_candidate_from_research_feed(state_root: Path) -> dict[str, Any] | None:
    """Read state/research/feed.json and return the top non-self-referential entry.

    Returns None if feed is missing, empty, unreadable, or every entry is a
    self-referential pipeline meta-task (see
    ``_research_feed_entry_is_self_referential``, issue #690).
    Used as fallback when MEMORY.md backlog is exhausted (all priorities Done).
    """
    feed_path = state_root / "research" / "feed.json"
    if not feed_path.exists():
        return None
    try:
        raw = feed_path.read_text(encoding="utf-8", errors="replace")
        feed = json.loads(raw)
    except Exception:
        return None
    entries = feed.get("entries") if isinstance(feed.get("entries"), list) else []
    if not entries:
        return None
    top = next(
        (e for e in entries if isinstance(e, dict) and not _research_feed_entry_is_self_referential(e)),
        None,
    )
    if top is None:
        return None
    title = str(top.get("title") or top.get("hypothesis") or "Research candidate").strip()
    acceptance = str(top.get("acceptance") or top.get("action") or "").strip()
    instructions = acceptance or f"Implement: {title}"
    # Use a synthetic high priority number so it sorts after real backlog
    return {
        "priority": 99,
        "title": title,
        "instructions": instructions,
        "source": "research_feed",
    }


def _parse_goal_vectors_from_text(text: str) -> list[dict[str, Any]]:
    """Parse the "Vector N — Title: description" paragraphs from goal_text.json's text.

    goal_text.json's free-text mission statement carries two development
    vectors (see host/eeepc/etc/goal_text.json), but previously only the
    numbered "Current priority targets" list was ever read — the vectors
    themselves were unparsed (issue #690). Returns
    ``[{"number": 1, "title": "...", "statement": "..."}, ...]``; ``[]`` if
    no vectors are found. Defensive — never raises.
    """
    import re as _re

    matches = _re.findall(
        r"Vector\s+(\d+)\s*[—-]\s*(.+?):\s*(.+?)"
        r"(?=\n\nVector\s+\d+|\n\nValid progress|\n\nCurrent priority targets|\Z)",
        text,
        _re.DOTALL,
    )
    vectors: list[dict[str, Any]] = []
    for num, title, statement in matches:
        vectors.append({
            "number": int(num),
            "title": title.strip(),
            "statement": " ".join(statement.split()).strip(),
        })
    return vectors


def _latest_host_metrics_sample(state_root: Path) -> dict[str, Any] | None:
    """Return the most recent JSON-lines record from state/host_metrics/*.jsonl.

    Reads the newest-by-mtime .jsonl file and returns its last well-formed
    JSON line (typically a CPU/RAM/disk sample). None if the directory is
    absent/empty or no file yields a parseable line. Defensive — never raises.
    """
    metrics_dir = state_root / "host_metrics"
    if not metrics_dir.exists():
        return None
    try:
        jsonl_files = sorted(
            (p for p in metrics_dir.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return None
    for path in jsonl_files:
        try:
            lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        except Exception:
            continue
        for line in reversed(lines):
            try:
                sample = json.loads(line)
            except Exception:
                continue
            if isinstance(sample, dict):
                return sample
    return None


def _recent_report_streak(state_root: Path) -> dict[str, Any] | None:
    """Return the current PASS/FAIL streak from the most recent state/reports/*.json.

    Reads up to the 10 newest report files (schema: ``{"result_status": "PASS"|...}``,
    written by coordinator.py) and counts the run of matching ``result_status``
    values starting from the newest. None if the directory is absent/empty or
    no report carries a ``result_status``. Defensive — never raises.
    """
    reports_dir = state_root / "reports"
    if not reports_dir.exists():
        return None
    files = [p for p, _ in _json_files_sorted_by_mtime(True, reports_dir)][:10]
    if not files:
        return None
    statuses = []
    for path in files:
        data = _safe_read_json(path)
        if isinstance(data, dict) and data.get("result_status"):
            statuses.append(str(data["result_status"]))
    if not statuses:
        return None
    top_status = statuses[0]
    streak = 0
    for status in statuses:
        if status == top_status:
            streak += 1
        else:
            break
    return {"status": top_status, "streak": streak}


def _actionable_lessons_insight(workspace: Path) -> dict[str, str] | None:
    """Return the most recent file-path-bearing LessonsDB lesson/error entry.

    Reuses ``_insight_is_actionable`` (issue #656) so the synthesized
    hypothesis is grounded in something concrete enough for a subagent to
    act on, rather than a vague meta-lesson. None if LessonsDB is unavailable
    or no entry is actionable. Defensive — never raises.
    """
    try:
        from nanobot.runtime.lessons import LessonsDB

        db = LessonsDB(workspace)
        entries = [*db.load_lessons(), *db.load_errors()]
    except Exception:
        return None
    entries = sorted(
        (e for e in entries if isinstance(e, dict)),
        key=lambda e: str(e.get("last_seen") or e.get("date") or ""),
        reverse=True,
    )
    for entry in entries:
        text = " ".join(
            str(entry.get(field) or "")
            for field in ("title", "reusable_insight", "prevention", "root_cause", "approach")
        )
        if _insight_is_actionable(text):
            return {"title": str(entry.get("title") or "").strip(), "text": " ".join(text.split()).strip()[:300]}
    return None


def _synthesize_hypothesis_from_state(
    state_root: Path,
    selfevo_repo_root: Path | None,
    workspace: Path,
) -> dict[str, Any] | None:
    """Generate ONE genuinely-new bounded improvement hypothesis from goal vectors x state signals.

    Issue #690: when the finite goal_text.json "Current priority targets" list
    is exhausted (every explicit priority already done in git log), the loop
    must not idle for lack of work and must not fall through to the dead
    todo.md fallbacks or the circular research-feed meta-task. This generator
    is the always-on, open-ended source of new work: it parses the two GOAL
    VECTORS from goal_text.json's free-text mission statement (see
    ``_parse_goal_vectors_from_text`` — previously unparsed) and pairs them
    with the single most salient concrete STATE signal already on disk, tried
    in this priority order (most to least directly actionable):

      1. ``_latest_failure_learning`` — a fresh, concrete failed-candidate record.
      2. an actionable LessonsDB insight (file-path-bearing, via
         ``_insight_is_actionable``) from lessons.yaml/errors.yaml.
      3. a PASS/FAIL streak derived from the most recent state/reports/*.json.
      4. a state/host_metrics/*.jsonl CPU/RAM/disk sample.

    The composed title/instructions are a grounded, OPEN-ENDED seed ("propose
    AND implement one concrete bounded improvement toward this vector") — the
    actual invention is delegated to the downstream subagent (LLM); this
    function makes no LLM call and is deterministic/side-effect-free (no
    wall-clock or randomness dependence — it only varies with the state
    signals themselves).

    Dedup (issue #575 machinery, reused): a composed title that
    ``_title_already_done_in_git_log`` matches against ``_recent_git_log`` is
    rejected, and the next (signal, vector) combination is tried — trying all
    signals x all vectors before giving up. Returns None only when goal_text
    is missing/unparseable, no vectors are found, no state signal exists at
    all, or every (signal, vector) combination composes an already-done title.
    """
    goal_text_path = state_root / "goals" / "goal_text.json"
    if not goal_text_path.exists():
        return None
    try:
        data = json.loads(goal_text_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        return None
    vectors = _parse_goal_vectors_from_text(text)
    if not vectors:
        return None

    git_log = ""
    if selfevo_repo_root is not None and selfevo_repo_root.is_dir():
        git_log = _recent_git_log(selfevo_repo_root)
    recent_titles = ", ".join(
        line.split(" ", 1)[1].strip()
        for line in git_log.splitlines()[:8]
        if " " in line
    )

    signals: list[tuple[str, str]] = []
    failure = _latest_failure_learning(workspace)
    if isinstance(failure, dict):
        signals.append((
            "failure_learning",
            f"self-evolution candidate {failure.get('candidate_id') or 'unknown'} failed at commit "
            f"{failure.get('failed_commit') or 'unknown'}; health reasons: {failure.get('health_reasons')}",
        ))
    lessons_insight = _actionable_lessons_insight(workspace)
    if lessons_insight:
        signals.append(("lessons_insight", f"{lessons_insight['title']}: {lessons_insight['text']}"))
    report_streak = _recent_report_streak(state_root)
    if report_streak:
        signals.append((
            "report_streak",
            f"{report_streak['streak']}x consecutive {report_streak['status']} cycle result in state/reports",
        ))
    host_metrics = _latest_host_metrics_sample(state_root)
    if host_metrics:
        cpu = host_metrics.get("cpu_percent", host_metrics.get("cpu"))
        ram = host_metrics.get("ram_percent", host_metrics.get("ram", host_metrics.get("mem")))
        disk = host_metrics.get("disk_percent", host_metrics.get("disk"))
        signals.append(("host_metrics", f"latest host metrics sample: cpu={cpu} ram={ram} disk={disk}"))

    if not signals:
        return None

    for signal_kind, digest in signals:
        for vector in vectors:
            gap_phrase = digest.split(";")[0].split(":")[0][:80].strip()
            title = f"Vector {vector['number']}: {vector['title']} — close gap: {gap_phrase}"
            if git_log and _title_already_done_in_git_log(title, git_log):
                continue  # already done — try the next signal/vector combination
            instructions = (
                "Propose and implement ONE concrete, bounded, committable improvement toward this vector "
                "grounded in the state signal below. It must produce a real git commit (code/script/tool/doc). "
                "Do NOT restate this directive. "
                f"Vector: {vector['statement'][:300]} "
                f"State signal ({signal_kind}): {digest[:400]}. "
                f"Avoid anything already done: {recent_titles[:300]}."
            )
            return {
                "priority": 99,
                "title": title,
                "instructions": instructions[:1200],
                "source": "state_synthesized_hypothesis",
                "signal_kind": signal_kind,
            }
    return None  # every (signal, vector) combination was already done


# Issue #695: a stable, deliberately generic directive title. It never names a
# concrete gap (unlike _synthesize_hypothesis_from_state's composed titles), so
# it can never itself match "already done in git log" — the diverse, LLM-authored
# commit messages the subagent produces for genuinely new work essentially never
# share 3+ keywords with these rare/distinctive words (verified against this
# repo's own git log). That keeps the bridge's _task_already_done keyword-overlap
# check (nanobot/runtime/bridge.py) from ever falsely skipping this fallback.
_OPEN_ENDED_NOVELTY_TITLE = (
    "Open-ended novelty directive: invent and land one genuinely new committable improvement"
)


def _open_ended_novelty_directive(
    state_root: Path,
    selfevo_repo_root: Path | None,
    workspace: Path,
) -> dict[str, Any] | None:
    """Fallback 2b (issue #695): LLM-driven novelty once the deterministic generator is exhausted.

    _synthesize_hypothesis_from_state (issue #690) composes a title that names
    the concrete state-signal gap it grounds on — that makes it precise, but
    also fragile: once every (signal, vector) combination it can compose is
    already done in git log, it has nothing left to try and returns None. The
    previous fallbacks below it (todo.md, research-feed) are legacy/near-dead
    and can hand back stale or self-referential work — never a genuine novel
    improvement.

    This fallback fires under the same preconditions
    (goal_text vectors parse, at least one state signal exists) but does NOT
    compose a concrete title itself. Instead it hands the subagent a STABLE,
    open-ended directive: invent and implement ONE new bounded improvement
    toward the goal vectors, grounded in the subagent's own read of the
    current repo/host state, explicitly avoiding a list of recently-done
    commit subjects. The novelty is delegated entirely to the subagent (LLM)
    — the fixed directive title never collapses into a repeating bounded set
    the way a deterministically pre-composed title can.

    Returns None only when goal_text is missing/unparseable or no goal vectors
    are found — mirrors _synthesize_hypothesis_from_state's own preconditions
    minus the per-combination dedup loop (there is nothing left to dedup: the
    directive intentionally never names concrete already-done work).
    """
    goal_text_path = state_root / "goals" / "goal_text.json"
    if not goal_text_path.exists():
        return None
    try:
        data = json.loads(goal_text_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        return None
    vectors = _parse_goal_vectors_from_text(text)
    if not vectors:
        return None

    git_log = ""
    if selfevo_repo_root is not None and selfevo_repo_root.is_dir():
        git_log = _recent_git_log(selfevo_repo_root)
    recent_titles = ", ".join(
        line.split(" ", 1)[1].strip()
        for line in git_log.splitlines()[:12]
        if " " in line
    )
    vector_summary = "; ".join(f"Vector {v['number']}: {v['statement'][:200]}" for v in vectors)
    instructions = (
        "Every concrete improvement candidate this generator can compose from current state "
        "signals is already done in git log. Propose AND implement ONE genuinely NEW, concrete, "
        "bounded, committable improvement toward the project goal vectors below, grounded in your "
        "own reading of the current repo/host state (code, tests, docs, host metrics, lessons). "
        "It must produce a real git commit (code/script/tool/doc). Do NOT restate this directive "
        "as your commit message; invent the concrete work yourself. "
        f"Goal vectors: {vector_summary[:500]}. "
        f"It must NOT duplicate any of these recently-done items: {recent_titles[:400]}."
    )
    return {
        "priority": 100,
        "title": _OPEN_ENDED_NOVELTY_TITLE,
        "instructions": instructions[:1400],
        "source": "open_ended_novelty_directive",
    }


def _write_materialized_improvement_artifact(
    *,
    state_root: Path,
    cycle_id: str,
    goal_id: str,
    current_task_id: str | None,
    summary: str,
    reward_signal: dict[str, Any] | None,
    feedback_decision: dict[str, Any] | None,
    runtime_source: dict[str, Any] | None = None,
    selfevo_repo_root: Path | None = None,
    workspace: Path | None = None,
) -> str | None:
    if current_task_id not in {"materialize-pass-streak-improvement", MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID}:
        return None
    improvements_dir = state_root / "improvements"
    improvements_dir.mkdir(parents=True, exist_ok=True)
    path = improvements_dir / f"materialized-{cycle_id}.json"
    is_synthesized_materialization = current_task_id == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID

    # Try to read a concrete backlog task from eeebot-self-evolving MEMORY.md
    backlog_task: dict[str, Any] | None = None
    # Derive selfevo_repo_root from explicit param or from state_root parent convention:
    # state_root = .../self-evolving-agent/state  →  parent = .../self-evolving-agent
    _selfevo_root = selfevo_repo_root or (state_root.parent / "eeebot-self-evolving")
    if _selfevo_root.is_dir():
        backlog_task = _parse_backlog_task_from_memory(_selfevo_root)

    # Fallback 1: when the MEMORY backlog is empty (all Done), route in the
    # operator's actual current priorities from state/goals/goal_text.json — this
    # is freshly seeded on every deploy and is more current than todo.md (#568).
    if backlog_task is None:
        backlog_task = _parse_backlog_task_from_goal_text(state_root, selfevo_repo_root=_selfevo_root)

    # Fallback 2 (issue #690, PRIMARY open-ended fallback): when the finite
    # goal_text.json priority list is also exhausted, never idle for lack of
    # work — synthesize ONE genuinely-new bounded hypothesis from the GOAL
    # VECTORS x concrete live STATE signals (host metrics, report PASS/FAIL
    # streak, failure learning, actionable lessons). See
    # _synthesize_hypothesis_from_state for the full generator contract.
    if backlog_task is None:
        backlog_task = _synthesize_hypothesis_from_state(state_root, _selfevo_root, workspace) if workspace is not None else None

    # Fallback 2b (issue #695, exhausted-generator robustness): once every
    # (signal, vector) combination the #690 generator can compose is already
    # done, shift novelty to the subagent (LLM) via a stable open-ended
    # directive instead of falling through to the dead/legacy fallbacks below.
    # See _open_ended_novelty_directive for why its title can never itself be
    # dedup-skipped as already_done.
    if backlog_task is None:
        backlog_task = _open_ended_novelty_directive(state_root, _selfevo_root, workspace) if workspace is not None else None

    # Fallback 3: legacy no-op — todo.md does not exist in the release
    # workspace (only in the separate eeebot-self-evolving instance repo), so
    # this never fires there; superseded by _synthesize_hypothesis_from_state
    # above (issue #690). Left in place (minimal diff) rather than removed,
    # since _next_open_goal_hypothesis (its sibling) still has a live call
    # site in cycle_feedback.py.
    if backlog_task is None:
        backlog_task = _next_open_goal_as_backlog_task(workspace)

    # Fallback 4: last resort — top non-self-referential candidate from
    # research/feed.json (issue #690: self-referential pipeline meta-task
    # entries are now filtered out by _pick_candidate_from_research_feed).
    if backlog_task is None:
        backlog_task = _pick_candidate_from_research_feed(state_root)

    concrete_statement = (
        "A synthesized review lane was materialized into a concrete bounded improvement artifact."
        if is_synthesized_materialization
        else "A repeated PASS pattern was strong enough to justify promoting a distinct bounded execution follow-up."
    )
    if backlog_task:
        concrete_statement = (
            f"Priority {backlog_task['priority']}: {backlog_task['title']}. "
            f"{backlog_task['instructions'][:200]}"
        )
    rationale = (
        "The system converted the synthesized candidate into an artifact so the lane can complete instead of repeating discard-only execution."
        if is_synthesized_materialization
        else "The system observed repeated successful cycles and converted that insight into a materialized bounded improvement artifact."
    )
    hadi_cycle = {
        "hypothesis": "A concrete bounded materialization will create stronger self-improvement evidence than another reward/candidate bookkeeping cycle.",
        "action": "Materialize one reviewable improvement artifact and route it to a follow-up verification lane.",
        "data": {
            "task_id": current_task_id,
            "reward_signal": reward_signal,
            "feedback_mode": feedback_decision.get("mode") if isinstance(feedback_decision, dict) else None,
            "ambition_escalation_reasons": ((feedback_decision.get("ambition_escalation") or {}).get("reasons") if isinstance(feedback_decision, dict) and isinstance(feedback_decision.get("ambition_escalation"), dict) else None),
        },
        "insight": "The artifact must either qualify as material progress or trigger explicit subagent verification/blocker handling.",
    }
    payload = {
        "schema_version": "materialized-improvement-v1",
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "task_id": current_task_id,
        "summary": summary,
        "reward_signal": reward_signal,
        "feedback_decision": feedback_decision,
        "runtime_source": runtime_source or {},
        "hadi_cycle": hadi_cycle,
        "concrete_improvement_statement": concrete_statement,
        "recommended_next_action": (
            (
                f"Implement Priority {backlog_task['priority']}: {backlog_task['title']}. "
                f"{backlog_task['instructions'][:300]}"
            )
            if backlog_task
            else (
                str(hadi_cycle.get("action") or "").strip()
                or str(feedback_decision.get("selected_task_title") if isinstance(feedback_decision, dict) else "").strip()
                or str(current_plan.get("current_task") or "").strip()
            )
        ),
        "rationale": rationale,
        "acceptance_checks": [
            "distinct materialized improvement artifact exists",
            "feedback decision references completion or follow-up semantics",
            "next bounded candidate is explicit and reviewable",
        ],
        "next_bounded_candidate": {
            "task_id": current_task_id,
            "title": (
                backlog_task["title"]
                if backlog_task
                else (
                    "Materialize one bounded improvement from the synthesized candidate"
                    if is_synthesized_materialization
                    else "Materialize one concrete bounded improvement from the repeated PASS insight"
                )
            ),
            "acceptance": (
                f"Implement and commit the improvement described in Priority {backlog_task['priority']}"
                if backlog_task
                else (
                    "write a concrete bounded improvement proposal or artifact and route it into self-evolution"
                    if is_synthesized_materialization
                    else "produce one concrete bounded follow-up candidate derived from the inspect-pass-streak review"
                )
            ),
            "task_class": "execution",
            "backlog_priority": backlog_task["priority"] if backlog_task else None,
            "backlog_instructions": backlog_task["instructions"] if backlog_task else None,
        },
        "derived_candidate": {
            "task_id": current_task_id,
            "title": (
                backlog_task["title"]
                if backlog_task
                else (
                    "Materialize one bounded improvement from the synthesized candidate"
                    if is_synthesized_materialization
                    else "Materialize one concrete bounded improvement from the repeated PASS insight"
                )
            ),
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


# _TERMINAL_SUBAGENT_RESULT_STATUSES (terminal bridge/materializer
# result_status values meaning the subagent verify lane genuinely finished —
# including a clean "already_done" verdict, issue #656/#661) now lives in
# cycle_observe.py, shared with cycle_feedback's generation-restart guard.


def _subagent_lane_health(*, state_root: Path, current_task_id: str | None, stale_after_seconds: int = 3600) -> dict[str, Any]:
    if current_task_id != "subagent-verify-materialized-improvement":
        return {"state": "not_applicable", "stale_request_count": 0, "queued_request_count": 0, "recommended_action": None}
    request_dir = state_root / "subagents" / "requests"
    result_dir = state_root / "subagents" / "results"
    now = time.time()
    queued: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    if request_dir.exists():
        # Use os.scandir-based helper to avoid double-stat penalty of glob()+stat().
        for path, mtime in list(_json_files_sorted_by_mtime(True, request_dir))[:100]:
            payload = _safe_read_json(path)
            if not payload or payload.get("task_id") != current_task_id:
                continue
            status = payload.get("request_status") or payload.get("status") or "queued"
            age = max(0, int(now - mtime))
            item = {"path": str(path), "status": status, "age_seconds": age, "task_id": current_task_id}

            # Scope completion detection to the result file actually produced
            # for THIS request — correlated via the same request_id (== the
            # _generation_scoped_verification_id materialized when the
            # request was written) that _write_bridge_completed_result and
            # the coordinator materializer use to name result-<request_id>.json.
            # An unscoped `len(glob("*.json"))` would (and previously did)
            # match a stale/prior-cycle result and hide real stagnation.
            result_path = _result_path_for(result_dir, path, payload)
            result_payload = _safe_read_json(result_path) if result_path.exists() else None
            result_status = result_payload.get("result_status") if isinstance(result_payload, dict) else None
            if result_status in _TERMINAL_SUBAGENT_RESULT_STATUSES:
                completed.append({
                    **item,
                    "status": "completed",
                    "result_status": result_status,
                    "result_path": str(result_path),
                })
                continue

            if status in {"queued", "pending"}:
                queued.append(item)
                if age >= stale_after_seconds:
                    stale.append({**item, "status": "stale"})
    state = "completed" if completed else ("stale" if stale else ("queued" if queued else "missing_request"))
    return {
        "schema_version": "subagent-lane-health-v1",
        "state": state,
        "queued_request_count": len(queued),
        "stale_request_count": len(stale),
        "latest_stale_request": stale[0] if stale else None,
        "latest_request": queued[0] if queued else None,
        "completed_result_count": len(completed),
        "latest_completed_result": completed[0] if completed else None,
        "recommended_action": "retire_or_block_stale_subagent_lane" if state in {"stale", "missing_request"} else None,
    }


def _generation_scoped_verification_id(*, semantic_task_id: str, cycle_id: str, source_artifact: str | None) -> str:
    artifact_hash = hashlib.sha256(str(source_artifact or "").encode("utf-8")).hexdigest()[:8]
    return f"{semantic_task_id}-{cycle_id}-{artifact_hash}"


def _live_verify_request_for_artifact(
    *,
    state_root: Path,
    source_artifact_path: str,
    task_id: str = "subagent-verify-materialized-improvement",
) -> Path | None:
    """Return the path of an existing LIVE verify request correlated to
    source_artifact_path, or None if none exists.

    Issue #700: a request counts as "live" (still in flight, blocking a
    duplicate write) unless its correlated result file already carries a
    terminal result_status (``_TERMINAL_SUBAGENT_RESULT_STATUSES``:
    already_done/completed/no_commit/blocked). A STALE queued request whose
    result already resolved does NOT count as live and must not block a
    fresh write for the same artifact — this is what lets the accumulated
    stale requests on a stuck host be safely ignored.
    """
    request_dir = state_root / "subagents" / "requests"
    result_dir = state_root / "subagents" / "results"
    if not request_dir.exists():
        return None
    for path, _mtime in _json_files_sorted_by_mtime(True, request_dir):
        payload = _safe_read_json(path)
        if not isinstance(payload, dict) or payload.get("task_id") != task_id:
            continue
        if payload.get("source_artifact") != source_artifact_path:
            continue
        result_path = _result_path_for(result_dir, path, payload)
        result_payload = _safe_read_json(result_path) if result_path.exists() else None
        result_status = result_payload.get("result_status") if isinstance(result_payload, dict) else None
        if result_status in _TERMINAL_SUBAGENT_RESULT_STATUSES:
            continue  # stale — already has a terminal result, doesn't block
        return path
    return None


def _write_subagent_request_artifact(
    *,
    state_root: Path,
    cycle_id: str,
    goal_id: str,
    current_plan: dict[str, Any],
    workspace: Path | None = None,
) -> str | None:
    if not _deterministic_planner_enabled():
        _logger.info(
            "deterministic planner minting disabled "
            "(SELFEVO_DETERMINISTIC_PLANNER_ENABLED=0) — skipping request write"
        )
        return None
    if current_plan.get("current_task_id") != "subagent-verify-materialized-improvement":
        return None
    request_dir = state_root / "subagents" / "requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    path = request_dir / f"request-{cycle_id}.json"
    current_task_id = current_plan.get("current_task_id")
    current_task = next((task for task in current_plan.get("tasks", []) if isinstance(task, dict) and (task.get("task_id") or task.get("taskId")) == current_task_id), None)
    source_artifact = current_plan.get("materialized_improvement_artifact_path") or ((current_plan.get("feedback_decision") or {}).get("artifact_path") if isinstance(current_plan.get("feedback_decision"), dict) else None)
    if not source_artifact:
        improvements_dir = state_root / "improvements"
        # Use os.scandir-based helper to avoid double-stat penalty of glob()+stat().
        _materialized = [p for p, _ in _json_files_sorted_by_mtime(True, improvements_dir) if p.name.startswith("materialized-")] if improvements_dir.exists() else []
        source_artifact = str(_materialized[0]) if _materialized else None

    # Issue #700: never write a second live request for the same generation.
    # If a non-terminally-resulted request already exists for source_artifact,
    # reuse it instead of minting a duplicate (this is the root cause of the
    # accumulated stale-request pile and of the idle-backstop counter
    # resetting on every re-selection of the same generation).
    if source_artifact:
        existing_live = _live_verify_request_for_artifact(
            state_root=state_root, source_artifact_path=str(source_artifact)
        )
        if existing_live is not None:
            return str(existing_live)

    # Attach relevant lessons context so subagent can avoid known pitfalls
    lessons_context: dict[str, Any] = {}
    if workspace is not None:
        try:
            from nanobot.runtime.lessons import LessonsDB
            lessons_context = LessonsDB(workspace).query_for_task(str(current_task_id or ""))
        except Exception:  # noqa: BLE001
            pass

    concrete_improvement_statement = str(current_plan.get("concrete_improvement_statement") or "").strip()
    hadi_cycle = current_plan.get("hadi_cycle") if isinstance(current_plan.get("hadi_cycle"), dict) else {}
    hadi_action = str(hadi_cycle.get("action") or "").strip()
    materialized_task = " ".join(
        part
        for part in (
            concrete_improvement_statement,
            f"next action: {hadi_action}" if hadi_action else "",
        )
        if part
    ).strip()
    recommended_next_action = hadi_action or str(current_plan.get("selected_task_title") or current_plan.get("current_task") or "").strip()

    # When the materialized artifact carries a concrete implementable goal (title +
    # instructions, e.g. routed from todo.md), make the subagent's primary directive
    # IMPLEMENT-and-commit rather than "review to verify the artifact" — otherwise the
    # request's verify framing overrides the implement goal and the subagent only
    # reviews (no code). Per the operating contract, Execute must perform the work.
    implement_title: str | None = None
    implement_directive: str | None = None
    if source_artifact:
        try:
            _art = json.loads(Path(str(source_artifact)).read_text(encoding="utf-8"))
            _nbc = _art.get("next_bounded_candidate") if isinstance(_art, dict) else None
            if isinstance(_nbc, dict) and _nbc.get("title") and _nbc.get("backlog_instructions"):
                _pri = _nbc.get("backlog_priority")
                implement_title = f"Implement and commit: {_nbc['title']}"
                implement_directive = (
                    "Implement and commit"
                    + (f" Priority {_pri}" if _pri else "")
                    + f": {_nbc['title']}. {str(_nbc['backlog_instructions'])[:500]}"
                ).strip()
        except Exception:  # noqa: BLE001
            pass

    payload = {
        "schema_version": "subagent-request-v1",
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "task_id": current_task_id,
        "semantic_task_id": current_task_id,
        "request_id": _generation_scoped_verification_id(semantic_task_id=str(current_task_id), cycle_id=cycle_id, source_artifact=source_artifact),
        "verification_task_id": _generation_scoped_verification_id(semantic_task_id=str(current_task_id), cycle_id=cycle_id, source_artifact=source_artifact),
        "verification_role": "materialized_improvement_implementation" if implement_directive else "materialized_improvement_review",
        "task_title": implement_title or ((current_task.get("title") or current_task.get("summary")) if isinstance(current_task, dict) else current_plan.get("current_task")),
        "task": implement_directive or materialized_task or current_plan.get("selected_task_title") or current_plan.get("current_task"),
        "recommended_next_action": implement_directive or recommended_next_action,
        "request_status": "queued",
        "profile": "bounded_execution",
        "budget": "standard",
        "source_artifact": source_artifact,
        "feedback_decision": current_plan.get("feedback_decision"),
        "lessons_context": lessons_context,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _ensure_verify_request_for_fresh_materialization(
    *,
    state_root: Path,
    cycle_id: str,
    goal_id: str,
    workspace: Path | None = None,
    selfevo_repo_root: Path | None = None,
) -> str | None:
    """Issue #700 decouple guard: generate->execute safety net.

    Runs every cycle in the coordinator's cycle-run path — deliberately
    OUTSIDE ``_derive_feedback_decision`` — so a fresh materialized-
    improvement hypothesis always reliably gets a verify request written,
    independent of the feedback decision's current mode/lane. Logic:

    1. Find the newest ``state/improvements/materialized-*.json`` artifact.
       If none exists, nothing to do.
    2. If its hypothesis title is already done in the selfevo git log
       (``_title_already_done_in_git_log`` / ``_recent_git_log``), do
       nothing — dedup.
    3. Otherwise, write a verify request via the same
       ``_write_subagent_request_artifact`` helper the normal feedback-
       decision handoff uses (identical schema/fields). That helper's own
       liveness check (``_live_verify_request_for_artifact``) makes this a
       no-op — returning the existing path rather than a duplicate — when a
       live (non-terminally-resulted) request already exists for this exact
       artifact, whether written by the normal handoff earlier this same
       cycle or by a prior cycle. Stale requests that already resolved to a
       terminal result (already_done/completed/no_commit/blocked) do not
       count as live and never block a fresh write.
    """
    if not _deterministic_planner_enabled():
        _logger.info(
            "deterministic planner minting disabled "
            "(SELFEVO_DETERMINISTIC_PLANNER_ENABLED=0) — skipping request write"
        )
        return None
    improvements_dir = state_root / "improvements"
    if not improvements_dir.exists():
        return None
    materialized = [
        p for p, _mtime in _json_files_sorted_by_mtime(True, improvements_dir)
        if p.name.startswith("materialized-")
    ]
    if not materialized:
        return None
    newest_path = materialized[0]
    artifact = _safe_read_json(newest_path)
    if not isinstance(artifact, dict):
        return None

    title = str(
        (artifact.get("next_bounded_candidate") or {}).get("title")
        or (artifact.get("derived_candidate") or {}).get("title")
        or ""
    ).strip()
    _selfevo_root = selfevo_repo_root or (state_root.parent / "eeebot-self-evolving")
    if title and _selfevo_root.is_dir():
        git_log = _recent_git_log(_selfevo_root)
        if git_log and _title_already_done_in_git_log(title, git_log):
            return None  # already done — dedup, nothing to spawn

    fallback_plan: dict[str, Any] = {
        "current_task_id": "subagent-verify-materialized-improvement",
        "tasks": [],
        "materialized_improvement_artifact_path": str(newest_path),
        "feedback_decision": artifact.get("feedback_decision"),
        "concrete_improvement_statement": artifact.get("concrete_improvement_statement"),
        "hadi_cycle": artifact.get("hadi_cycle"),
        "selected_task_title": title or None,
        "current_task": title or None,
    }
    return _write_subagent_request_artifact(
        state_root=state_root,
        cycle_id=cycle_id,
        goal_id=goal_id,
        current_plan=fallback_plan,
        workspace=workspace,
    )


def _write_research_feed(
    *,
    state_root: Path,
    generated_candidates: list[dict[str, Any]],
    cycle_id: str,
    goal_id: str,
) -> dict[str, Any]:
    research_dir = state_root / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    feed_path = research_dir / "feed.json"
    entries = []
    for candidate in generated_candidates:
        entries.append({
            "id": candidate.get("task_id"),
            "title": candidate.get("title"),
            "summary": candidate.get("acceptance"),
            "action": candidate.get("acceptance"),
            "hypothesis": candidate.get("title"),
            "score": 15.0,
            "insights": [
                f"cycle_id={cycle_id}",
                f"goal_id={goal_id}",
                f"selection_source={candidate.get('selection_source')}",
            ],
            "acceptance": candidate.get("acceptance"),
        })
    payload = {
        "schema_version": "research-feed-v1",
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "entry_count": len(entries),
        "entries": entries,
    }
    feed_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    payload["feed_path"] = str(feed_path)

    # Also write hypotheses.json alongside feed — structured for lesson tracking
    hyp_path = research_dir / "hypotheses.json"
    try:
        existing_hyps: list[dict[str, Any]] = []
        if hyp_path.exists():
            _raw = json.loads(hyp_path.read_text(encoding="utf-8"))
            existing_hyps = _raw if isinstance(_raw, list) else []
        new_entry: dict[str, Any] = {
            "date": __import__("datetime").date.today().isoformat(),
            "cycle_id": cycle_id,
            "goal_id": goal_id,
            "candidates": [
                {"title": c.get("title"), "hypothesis": c.get("title"), "acceptance": c.get("acceptance")}
                for c in generated_candidates
            ],
        }
        # Prepend newest, keep last 50
        existing_hyps.insert(0, new_entry)
        hyp_path.write_text(json.dumps(existing_hyps[:50], indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # hypotheses.json is advisory only

    return payload


def _build_task_plan_snapshot(
    *,
    workspace: Path,
    cycle_id: str,
    goal_id: str,
    result_status: str,
    approval_gate_state: str,
    next_hint: str,
    experiment: dict[str, Any],
    report_path: Path,
    history_path: Path,
    improvement_score: Any,
    feedback_decision: dict[str, Any] | None,
    goals_dir: Path,
    materialized_improvement_artifact_path: str | None = None,
) -> dict[str, Any]:
    blocked_next_step = next_hint if result_status == "BLOCK" else ""
    recorded_current_task_id = None
    recorded_materialized_improvement_artifact_path = None
    recorded_feedback_artifact_path = None
    recorded_terminal_selfevo_task_before_activation = None
    if result_status == "BLOCK":
        file_action = {
            "kind": "file_write",
            "path": "state/approvals/apply.ok",
            "summary": "Write a fresh approval gate with a valid TTL",
        }
        verification_command = "PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py"
        tasks = [
            {"task_id": "refresh-approval-gate", "title": file_action["summary"], "status": "active", **file_action},
            {"task_id": "verify-approval-gate", "title": f"Verify the gate with `{verification_command}`", "status": "pending", "command": verification_command},
        ]
    elif result_status == "ERROR":
        tasks = [
            {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
            {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "active"},
            {"task_id": "record-reward", "title": "Record cycle reward", "status": "pending"},
        ]
        file_action = None
        verification_command = None
    else:
        recorded_task_plan = _safe_read_json(goals_dir / "current.json")
        recorded_tasks = recorded_task_plan.get("tasks") if isinstance(recorded_task_plan, dict) and isinstance(recorded_task_plan.get("tasks"), list) else None
        recorded_generated_candidates = recorded_task_plan.get("generated_candidates") if isinstance(recorded_task_plan, dict) and isinstance(recorded_task_plan.get("generated_candidates"), list) else []
        recorded_current_task_id = recorded_task_plan.get("current_task_id") if isinstance(recorded_task_plan, dict) else None
        recorded_materialized_improvement_artifact_path = recorded_task_plan.get("materialized_improvement_artifact_path") if isinstance(recorded_task_plan, dict) else None
        recorded_feedback_artifact_path = (recorded_task_plan.get("feedback_decision") or {}).get("artifact_path") if isinstance(recorded_task_plan, dict) and isinstance(recorded_task_plan.get("feedback_decision"), dict) else None
        if recorded_tasks:
            tasks = [dict(task) for task in recorded_tasks if isinstance(task, dict)]
            recorded_terminal_selfevo_task_before_activation = next((dict(task) for task in tasks if task.get('task_id') == 'analyze-last-failed-candidate'), None)
            has_active = False
            for task in tasks:
                if task.get("task_id") == recorded_current_task_id:
                    task["status"] = "active"
                    has_active = True
                elif task.get("status") == "active":
                    task["status"] = "pending"
            if not has_active:
                for task in tasks:
                    if task.get("task_id") == "record-reward":
                        task["status"] = "active"
                        has_active = True
                        break
            if not has_active:
                tasks.append({"task_id": "record-reward", "title": "Record cycle reward", "status": "active"})
        else:
            tasks = [
                {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
                {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "done"},
                {"task_id": "record-reward", "title": "Record cycle reward", "status": "active"},
            ]
        file_action = None
        verification_command = None

    # Precompute task lookup dict for O(1) lookups in the rest of this function
    _task_by_id: dict[str, dict[str, Any]] = {
        str(t.get("task_id")): t for t in tasks if t.get("task_id")
    }

    # Use _task_by_id for O(1) active-task lookup instead of O(n) linear scan.
    # Scan dict values (typically small) rather than the full task list.
    current_task_id = next(
        (tid for tid, t in _task_by_id.items() if t["status"] == "active"),
        None,
    )
    if current_task_id is None:
        # Fallback: linear scan only if dict lookup fails (should not happen).
        current_task_id = next(task["task_id"] for task in tasks if task["status"] == "active")
    reward_signal = dict(experiment.get("reward_signal")) if isinstance(experiment.get("reward_signal"), dict) else _derive_reward_signal(result_status, improvement_score)
    active_artifact_path = materialized_improvement_artifact_path or (recorded_materialized_improvement_artifact_path if 'recorded_materialized_improvement_artifact_path' in locals() else None) or (recorded_feedback_artifact_path if 'recorded_feedback_artifact_path' in locals() else None)
    if feedback_decision and feedback_decision.get("selected_task_id"):
        selected_task_id = str(feedback_decision["selected_task_id"])
        current_task_id = selected_task_id
        # Use _task_by_id for O(1) selected-task lookup; deactivate old active via dict.
        selected_task = _task_by_id.get(selected_task_id)
        if selected_task is not None:
            selected_task["status"] = "active"
        # Deactivate any other active tasks using the precomputed dict.
        for _tid, _t in _task_by_id.items():
            if _tid != selected_task_id and _t["status"] == "active":
                _t["status"] = "pending"
    latest_failure_learning = _latest_failure_learning(workspace)
    failure_learning_is_fresh = isinstance(latest_failure_learning, dict) and isinstance(latest_failure_learning.get('_age_seconds'), int) and latest_failure_learning.get('_age_seconds') <= 3600
    terminal_selfevo_issue = resolve_terminal_selfevo_issue(workspace=workspace, source_task_id='analyze-last-failed-candidate')
    terminal_selfevo_retired = False
    recorded_terminal_selfevo_task = _task_by_id.get('analyze-last-failed-candidate')
    recorded_terminal_selfevo_task_was_already_retired = (
        _task_is_terminal_selfevo_retired(
            recorded_terminal_selfevo_task_before_activation or recorded_terminal_selfevo_task,
            terminal_selfevo_issue,
        )
        or _task_has_recorded_terminal_selfevo_retirement(
            recorded_terminal_selfevo_task_before_activation or recorded_terminal_selfevo_task
        )
    )
    if recorded_terminal_selfevo_task_was_already_retired:
        terminal_selfevo_retired = True
    recorded_feedback_decision_for_repair = recorded_task_plan.get('feedback_decision') if 'recorded_task_plan' in locals() and isinstance(recorded_task_plan, dict) and isinstance(recorded_task_plan.get('feedback_decision'), dict) else {}
    recorded_reward_retirement = (
        isinstance(recorded_feedback_decision_for_repair, dict)
        and recorded_feedback_decision_for_repair.get('current_task_id') == 'record-reward'
        and recorded_feedback_decision_for_repair.get('retire_goal_artifact_pair') is True
    )
    recorded_complete_lane_to_reward = (
        isinstance(recorded_feedback_decision_for_repair, dict)
        and recorded_feedback_decision_for_repair.get('mode') == 'complete_active_lane'
        and recorded_feedback_decision_for_repair.get('current_task_id') == 'materialize-pass-streak-improvement'
        and recorded_feedback_decision_for_repair.get('selected_task_id') == 'record-reward'
        and recorded_feedback_decision_for_repair.get('selection_source') == 'feedback_complete_active_lane'
    )
    recorded_terminal_selfevo_retirement = (
        isinstance(recorded_feedback_decision_for_repair, dict)
        and (
            recorded_current_task_id == 'record-reward'
            or recorded_terminal_selfevo_task_was_already_retired
        )
        and recorded_feedback_decision_for_repair.get('mode') == 'retire_terminal_selfevo_lane'
        and recorded_feedback_decision_for_repair.get('selected_task_id') == 'record-reward'
        and recorded_feedback_decision_for_repair.get('selection_source') == 'feedback_terminal_selfevo_retire'
        and terminal_selfevo_issue is not None
    )
    if recorded_terminal_selfevo_retirement:
        terminal_selfevo_retired = True
        _analyze_task = _task_by_id.get("analyze-last-failed-candidate")
        if _analyze_task is not None:
            _analyze_task["status"] = "done"
            _analyze_task["terminal_reason"] = terminal_selfevo_issue.get("terminal_status") or "terminal_selfevo_issue"
        _reward_task = _task_by_id.get("record-reward")
        if _reward_task is not None:
            _reward_task["status"] = "active"
        else:
            tasks.append({"task_id": "record-reward", "title": "Record cycle reward", "status": "active"})
        for tid, t in _task_by_id.items():
            if tid not in ("analyze-last-failed-candidate", "record-reward") and t.get("status") == "active":
                t["status"] = "pending"
        current_task_id = "record-reward"
        feedback_decision = None
    if terminal_selfevo_issue is not None and not terminal_selfevo_retired and current_task_id == "analyze-last-failed-candidate":
        _analyze_task = _task_by_id.get("analyze-last-failed-candidate")
        if _analyze_task is not None:
            _analyze_task["status"] = "done"
            _analyze_task["terminal_reason"] = terminal_selfevo_issue.get("terminal_status") or "terminal_selfevo_issue"
        _reward_task = _task_by_id.get("record-reward")
        if _reward_task is not None:
            _reward_task["status"] = "active"
        else:
            tasks.append({"task_id": "record-reward", "title": "Record cycle reward", "status": "active"})
        for tid, t in _task_by_id.items():
            if tid not in ("analyze-last-failed-candidate", "record-reward") and t.get("status") == "active":
                t["status"] = "pending"
        current_task_id = "record-reward"
        feedback_decision = {
            "mode": "retire_terminal_selfevo_lane",
            "reason": "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate",
            "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
            "current_task_id": "analyze-last-failed-candidate",
            "current_task_class": _task_action_class("analyze-last-failed-candidate"),
            "selected_task_id": "record-reward",
            "selected_task_class": _task_action_class("record-reward"),
            "selection_source": "feedback_terminal_selfevo_retire",
            "selected_task_title": "Record cycle reward",
            "selected_task_label": "Record cycle reward [task_id=record-reward]",
            "terminal_selfevo_issue": terminal_selfevo_issue,
        }
        terminal_selfevo_retired = True
    elif (
        isinstance(latest_failure_learning, dict)
        and current_task_id != MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        and (current_task_id == "record-reward" or failure_learning_is_fresh)
        and not terminal_selfevo_retired
    ):
        if terminal_selfevo_issue is not None and recorded_complete_lane_to_reward:
            _analyze_task = _task_by_id.get("analyze-last-failed-candidate")
            if _analyze_task is not None:
                _analyze_task["status"] = "done"
                _analyze_task["terminal_reason"] = terminal_selfevo_issue.get("terminal_status") or "terminal_selfevo_issue"
            _reward_task = _task_by_id.get("record-reward")
            if _reward_task is not None:
                _reward_task["status"] = "active"
            else:
                tasks.append({"task_id": "record-reward", "title": "Record cycle reward", "status": "active"})
            for tid, t in _task_by_id.items():
                if tid not in ("analyze-last-failed-candidate", "record-reward") and t.get("status") == "active":
                    t["status"] = "pending"
            current_task_id = "record-reward"
            feedback_decision = {
                "mode": "retire_terminal_selfevo_lane",
                "reason": "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": "analyze-last-failed-candidate",
                "current_task_class": _task_action_class("analyze-last-failed-candidate"),
                "selected_task_id": "record-reward",
                "selected_task_class": _task_action_class("record-reward"),
                "selection_source": "feedback_terminal_selfevo_retire",
                "selected_task_title": "Record cycle reward",
                "selected_task_label": "Record cycle reward [task_id=record-reward]",
                "terminal_selfevo_issue": terminal_selfevo_issue,
            }
            terminal_selfevo_retired = True
        elif terminal_selfevo_issue is None and ((recorded_reward_retirement and failure_learning_is_fresh) or recorded_complete_lane_to_reward):
            repair_source = 'fresh_failure_learning_after_reward_retirement' if recorded_reward_retirement else 'stale_complete_lane_record_reward_repair'
            repair_selection_source = 'feedback_fresh_failure_learning_after_reward_retirement' if recorded_reward_retirement else 'feedback_complete_active_lane_to_failure_learning'
            repair_reason = (
                'fresh failure-learning evidence after a retired record-reward lane must be analyzed before returning to bookkeeping'
                if recorded_reward_retirement
                else 'stale complete-active-lane record-reward authority must revive failure-learning analysis before bookkeeping'
            )
            repair_mode = 'fresh_failure_learning_after_reward_retirement' if recorded_reward_retirement else 'stale_complete_lane_record_reward_repair'
            repair_task = _task_by_id.get("analyze-last-failed-candidate")
            if repair_task is None:
                repair_task = {
                    'task_id': 'analyze-last-failed-candidate',
                    'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
                    'status': 'active',
                    'kind': 'review',
                    'acceptance': 'produce a bounded explanation of the failed candidate and one safer follow-up mutation idea',
                    'selection_source': repair_source,
                    'failed_candidate_id': latest_failure_learning.get('candidate_id'),
                    'failed_commit': latest_failure_learning.get('failed_commit'),
                    'health_reasons': latest_failure_learning.get('health_reasons'),
                }
                tasks.append(repair_task)
            for task in tasks:
                if task.get('task_id') == 'analyze-last-failed-candidate':
                    task['status'] = 'active'
                    task['selection_source'] = repair_source
                    task['failed_candidate_id'] = latest_failure_learning.get('candidate_id')
                    task['failed_commit'] = latest_failure_learning.get('failed_commit')
                    task['health_reasons'] = latest_failure_learning.get('health_reasons')
                elif task.get('status') == 'active':
                    task['status'] = 'pending'
            current_task_id = 'analyze-last-failed-candidate'
            feedback_decision = {
                "mode": repair_mode,
                "reason": repair_reason,
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": "record-reward",
                "current_task_class": _task_action_class("record-reward"),
                "retire_goal_artifact_pair": bool(recorded_reward_retirement),
                "selected_task_id": "analyze-last-failed-candidate",
                "selected_task_class": _task_action_class("analyze-last-failed-candidate"),
                "selection_source": repair_selection_source,
                "selected_task_title": "Analyze the last failed self-evolution candidate before retrying mutation",
                "selected_task_label": "Analyze the last failed self-evolution candidate before retrying mutation [task_id=analyze-last-failed-candidate]",
                "failure_learning": latest_failure_learning,
                "terminal_selfevo_issue": terminal_selfevo_issue,
            }
        elif terminal_selfevo_issue is not None and current_task_id != "subagent-verify-materialized-improvement":
            for task in tasks:
                if task.get("task_id") == "analyze-last-failed-candidate":
                    task["status"] = "done"
                    task["terminal_reason"] = terminal_selfevo_issue.get("terminal_status") or "terminal_selfevo_issue"
                elif task.get("task_id") == "record-reward":
                    task["status"] = "active"
                elif task.get("status") == "active":
                    task["status"] = "pending"
            if "record-reward" not in _task_by_id:
                tasks.append({"task_id": "record-reward", "title": "Record cycle reward", "status": "active"})
            current_task_id = "record-reward"
            feedback_decision = {
                "mode": "retire_terminal_selfevo_lane",
                "reason": "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": "analyze-last-failed-candidate",
                "current_task_class": _task_action_class("analyze-last-failed-candidate"),
                "selected_task_id": "record-reward",
                "selected_task_class": _task_action_class("record-reward"),
                "selection_source": "feedback_terminal_selfevo_retire",
                "selected_task_title": "Record cycle reward",
                "selected_task_label": "Record cycle reward [task_id=record-reward]",
                "terminal_selfevo_issue": terminal_selfevo_issue,
            }
        else:
            repair_task = _task_by_id.get("analyze-last-failed-candidate")
            if repair_task is None:
                repair_task = {
                    'task_id': 'analyze-last-failed-candidate',
                    'title': 'Analyze the last failed self-evolution candidate before retrying mutation',
                    'status': 'active',
                    'kind': 'review',
                    'acceptance': 'produce a bounded explanation of the failed candidate and one safer follow-up mutation idea',
                    'selection_source': 'generated_from_failure_learning',
                    'failed_candidate_id': latest_failure_learning.get('candidate_id'),
                    'failed_commit': latest_failure_learning.get('failed_commit'),
                    'health_reasons': latest_failure_learning.get('health_reasons'),
                }
                tasks.append(repair_task)
            for task in tasks:
                task['status'] = 'pending' if task.get('task_id') != 'analyze-last-failed-candidate' and task.get('status') == 'active' else task.get('status')
            repair_task['status'] = 'active'
            current_task_id = 'analyze-last-failed-candidate'
            feedback_decision = {
                "mode": "fresh_failure_learning_repair",
                "reason": "fresh failure-learning evidence remains the stronger repair lane than stale subagent bookkeeping",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": recorded_current_task_id,
                "current_task_class": _task_action_class(recorded_current_task_id),
                "selected_task_id": "analyze-last-failed-candidate",
                "selected_task_class": _task_action_class("analyze-last-failed-candidate"),
                "selection_source": "feedback_stale_subagent_retire_to_failure_learning" if recorded_current_task_id == "subagent-verify-materialized-improvement" else "generated_from_failure_learning",
                "selected_task_title": "Analyze the last failed self-evolution candidate before retrying mutation",
                "selected_task_label": "Analyze the last failed self-evolution candidate before retrying mutation [task_id=analyze-last-failed-candidate]",
                "failure_learning": latest_failure_learning,
                "terminal_selfevo_issue": terminal_selfevo_issue,
            }
    generated_candidates = _derive_generated_candidates(
        goals_dir=goals_dir,
        result_status=result_status,
        current_task_id=current_task_id,
        failure_learning=latest_failure_learning,
        retire_analyze_last_failed_candidate=terminal_selfevo_issue is not None,
    )
    # HADI Insight -> next-Hypothesis: seed the synthesized candidate from the
    # insight most relevant to the active goal (ranked by goal relevance +
    # reward signal + recency, not merely newest), so an empty backlog keeps
    # producing concrete, goal-directed insight-derived hypotheses.
    _freshest_insight = _select_insight_for_goal(workspace, goal_id)
    if (
        isinstance(feedback_decision, dict)
        and feedback_decision.get("selected_task_id") == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID
        and not any(candidate.get("task_id") == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID for candidate in generated_candidates)
    ):
        generated_candidates.append(
            _synthesized_next_improvement_candidate(
                current_task_id=current_task_id,
                strong_pass_count=int(feedback_decision.get("strong_pass_count") or 0),
                goal_artifact_signature=feedback_decision.get("goal_artifact_signature") if isinstance(feedback_decision.get("goal_artifact_signature"), list) else None,
                status="active",
                insight=_freshest_insight,
            )
        )
    if (
        isinstance(feedback_decision, dict)
        and feedback_decision.get("selected_task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        and not any(candidate.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID for candidate in generated_candidates)
    ):
        generated_candidates.append(
            _synthesized_materialize_improvement_candidate(
                current_task_id=SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
                strong_pass_count=int(feedback_decision.get("strong_pass_count") or 0),
                goal_artifact_signature=feedback_decision.get("goal_artifact_signature") if isinstance(feedback_decision.get("goal_artifact_signature"), list) else None,
                status="active",
                insight=_freshest_insight,
            )
        )
    carried_candidates = [dict(item) for item in recorded_generated_candidates if isinstance(item, dict)] if 'recorded_generated_candidates' in locals() else []
    inferred_candidates = _inferred_generated_candidates_from_tasks(tasks)
    combined_candidates: list[dict[str, Any]] = []
    seen_candidate_ids: set[str] = set()
    for candidate in [*carried_candidates, *inferred_candidates, *generated_candidates]:
        cid = candidate.get("task_id") if isinstance(candidate, dict) else None
        if not cid or cid in seen_candidate_ids:
            continue
        matching_task = _task_by_id.get(cid)
        if isinstance(matching_task, dict) and not _task_is_selectable(matching_task):
            continue
        combined_candidates.append(candidate)
        seen_candidate_ids.add(cid)
    # Build O(1) candidate lookup dict to replace repeated next() linear scans
    # over combined_candidates (lines 2535, 2560, 2657).
    _candidate_by_id: dict[str, dict[str, Any]] = {
        c.get("task_id"): c for c in combined_candidates if c.get("task_id")
    }
    existing_ids = {task.get("task_id") for task in tasks}
    for candidate in combined_candidates:
        if candidate.get("task_id") not in existing_ids:
            tasks.append(candidate)
            existing_ids.add(candidate.get("task_id"))
    if isinstance(feedback_decision, dict) and feedback_decision.get("selected_task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID:
        for task in tasks:
            if task.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID:
                task["status"] = "active"
            elif task.get("status") == "active":
                task["status"] = "pending"
        current_task_id = MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
    if (
        current_task_id == "inspect-pass-streak"
        and (not isinstance(feedback_decision, dict) or not feedback_decision.get("selected_task_id"))
    ):
        followup = _candidate_by_id.get("materialize-pass-streak-improvement")
        if followup is not None:
            feedback_decision = {
                "mode": "promote_review_followup",
                "reason": "active inspect-pass-streak review produced a concrete bounded follow-up candidate",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": current_task_id,
                "current_task_class": _task_action_class(current_task_id),
                "repeat_block_count": 0,
                "repeat_block_failure_class": None,
                "goal_artifact_signature": None,
                "strong_pass_count": None,
                "retire_goal_artifact_pair": False,
                "selected_task_id": followup.get("task_id"),
                "selected_task_class": _task_action_class(followup.get("task_id")),
                "selection_source": "feedback_review_to_execution",
                "selected_task_title": followup.get("title") or followup.get("summary") or followup.get("task_id"),
                "selected_task_label": _render_task_selection(followup),
            }
    should_promote_synthesized_materialization = (
        current_task_id == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID
        and experiment.get("outcome") == "discard"
        and experiment.get("revert_status") == "skipped_no_material_change"
    )
    if should_promote_synthesized_materialization:
        materialize_synthesized = _candidate_by_id.get(MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID)
        if materialize_synthesized is not None and _task_is_selectable(materialize_synthesized):
            readiness_gate = _task_readiness_gate(materialize_synthesized)
            if readiness_gate.get("state") != "ready":
                feedback_decision = {
                    "mode": "readiness_blocked",
                    "reason": "HADI DoR/DoD readiness gate blocked weak synthesized materialization candidate before execution",
                    "current_task_id": SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
                    "current_task_class": _task_action_class(SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID),
                    "selected_task_id": SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
                    "selected_task_class": _task_action_class(SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID),
                    "selection_source": "feedback_readiness_gate_blocked",
                    "blocked_candidate_id": MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
                    "readiness_gate": readiness_gate,
                }
                for task in tasks:
                    if task.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID:
                        task["status"] = "blocked"
                        task["readiness_gate"] = readiness_gate
                    elif task.get("task_id") == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID:
                        task["status"] = "active"
                    elif task.get("status") == "active":
                        task["status"] = "pending"
                current_task_id = SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID
            else:
                for task in tasks:
                    if task.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID:
                        task["status"] = "active"
                    elif task.get("task_id") == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID:
                        task["status"] = "pending"
                    elif task.get("status") == "active":
                        task["status"] = "pending"
                current_task_id = MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
                feedback_decision = {
                    "mode": "materialize_synthesized_improvement",
                    "reason": "synthesized-improvement review reached repeated discard/no-artifact pressure; promote a concrete execution follow-up",
                    "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                    "current_task_id": SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
                    "current_task_class": _task_action_class(SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID),
                    "repeat_block_count": 0,
                    "repeat_block_failure_class": None,
                    "goal_artifact_signature": materialize_synthesized.get("goal_artifact_signature"),
                    "strong_pass_count": materialize_synthesized.get("strong_pass_count"),
                    "retire_goal_artifact_pair": False,
                    "selected_task_id": MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
                    "selected_task_class": _task_action_class(MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID),
                    "selection_source": "feedback_synthesis_materialization",
                    "selected_task_title": materialize_synthesized.get("title") or MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
                    "selected_task_label": _render_task_selection(materialize_synthesized),
                    "readiness_gate": readiness_gate,
                }
    materialization_task_ids = {"materialize-pass-streak-improvement", MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID}
    materialized_artifact_task_id = None
    if materialized_improvement_artifact_path:
        materialized_artifact_payload = _safe_read_json(Path(materialized_improvement_artifact_path))
        if isinstance(materialized_artifact_payload, dict) and materialized_artifact_payload.get("task_id") in materialization_task_ids:
            materialized_artifact_task_id = materialized_artifact_payload.get("task_id")
    if materialized_artifact_task_id in materialization_task_ids:
        artifact_task_record = next((task for task in tasks if task.get("task_id") == materialized_artifact_task_id), None)
        if _task_is_selectable(artifact_task_record):
            current_task_id = materialized_artifact_task_id
    confirmed_synthesized_materialization_completion = (
        current_task_id == "record-reward"
        and materialized_artifact_task_id == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        and isinstance(recorded_feedback_decision_for_repair, dict)
        and recorded_feedback_decision_for_repair.get("mode") == "complete_active_lane"
        and recorded_feedback_decision_for_repair.get("current_task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        and recorded_feedback_decision_for_repair.get("selected_task_id") == "record-reward"
        and recorded_feedback_decision_for_repair.get("selection_source") == "feedback_complete_active_lane"
        and str(recorded_feedback_decision_for_repair.get("artifact_path") or "") == str(materialized_improvement_artifact_path)
    )
    if confirmed_synthesized_materialization_completion:
        current_task_id = MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
    if current_task_id in materialization_task_ids and result_status == "PASS" and materialized_improvement_artifact_path:
        is_synthesized_materialization = current_task_id == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
        completed_materialization_task_id = current_task_id
        # Issue #697: the `repeated_synthesized_materialization_completion`
        # shortcut that used to live here (routing a repeated completion
        # confirmation straight to record-reward, bypassing verify) is
        # removed. It was the root cause of the #697 live gap: it left the
        # verify task's persisted status stuck at "pending" forever (verify
        # was never made `current_task_id`, so should_retire_subagent_lane's
        # writer never ran), which permanently broke the old record-reward
        # restart's `_chain_complete_for_reward_check`. There is no longer any
        # path from a completed materialization straight to record-reward —
        # the unconditional handoff to `next_candidate` below always makes
        # verify current first (decide_next_lane step 6, cycle_feedback.py).
        for task in tasks:
            if task.get("task_id") == completed_materialization_task_id:
                task["status"] = "done"
            elif task.get("task_id") == "inspect-pass-streak" and not is_synthesized_materialization:
                task["status"] = "done"
            elif task.get("task_id") == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID and is_synthesized_materialization:
                task["status"] = "done"
            elif task.get("task_id") == "record-reward":
                task["status"] = "active"
            elif task.get("status") == "active":
                task["status"] = "pending"
        combined_candidates = [candidate for candidate in combined_candidates if candidate.get("task_id") not in {"inspect-pass-streak", "materialize-pass-streak-improvement", MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID}]
        next_candidate = _candidate_by_id.get("subagent-verify-materialized-improvement")
        if next_candidate is None and is_synthesized_materialization:
            next_candidate = {
                "task_id": "subagent-verify-materialized-improvement",
                "title": "Use one bounded subagent-assisted review to verify the materialized improvement artifact",
                "status": "pending",
                "kind": "review",
                "acceptance": "create one bounded subagent request that reviews the HADI materialized improvement artifact and reports a verification recommendation",
                "selection_source": "generated_from_hadi_materialized_improvement_completion",
                "parent_task_id": MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
                "subagent_profile": "bounded_execution",
                "subagent_budget": "standard",
            }
            if not any(task.get("task_id") == next_candidate.get("task_id") for task in tasks):
                tasks.append(dict(next_candidate))
        if next_candidate is not None and (
            not isinstance(latest_failure_learning, dict)
            or (is_synthesized_materialization and next_candidate.get("task_id") == "subagent-verify-materialized-improvement")
        ):
            for task in tasks:
                if task.get("task_id") == next_candidate.get("task_id"):
                    task["status"] = "active"
                elif task.get("task_id") == "record-reward":
                    task["status"] = "pending"
            current_task_id = next_candidate.get("task_id")
            feedback_decision = {
                "mode": "handoff_to_subagent_verification" if is_synthesized_materialization and next_candidate.get("task_id") == "subagent-verify-materialized-improvement" else "handoff_to_next_candidate",
                "reason": "materialized lane completed and handed off to the next bounded candidate",
                "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                "current_task_id": completed_materialization_task_id,
                "current_task_class": _task_action_class(completed_materialization_task_id),
                "selected_task_id": next_candidate.get("task_id"),
                "selected_task_class": _task_action_class(next_candidate.get("task_id")),
                "selection_source": "feedback_post_completion_handoff",
                "selected_task_title": next_candidate.get("title") or next_candidate.get("summary") or next_candidate.get("task_id"),
                "selected_task_label": _render_task_selection(next_candidate),
                "artifact_path": materialized_improvement_artifact_path,
            }
        else:
            completion_target_id = "record-reward"
            completion_target_title = "Record cycle reward"
            completion_selection_source = "feedback_complete_active_lane"
            completion_reason = "materialized improvement artifact written; richer execution lane completed"
            if terminal_selfevo_issue is not None and not is_synthesized_materialization:
                completion_selection_source = "feedback_terminal_selfevo_retire"
                completion_reason = "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate"
            elif isinstance(latest_failure_learning, dict) and not is_synthesized_materialization and not recorded_terminal_selfevo_task_was_already_retired:
                completion_target_id = "analyze-last-failed-candidate"
                completion_target_title = "Analyze the last failed self-evolution candidate before retrying mutation"
                completion_selection_source = "feedback_complete_active_lane_to_failure_learning"
                completion_reason = "materialized improvement artifact written, but fresh failure-learning evidence remains the next non-bookkeeping lane"
            for task in tasks:
                if task.get("task_id") == completion_target_id:
                    task["status"] = "active"
                    if completion_target_id == "analyze-last-failed-candidate":
                        task["selection_source"] = completion_selection_source
                        task["failed_candidate_id"] = latest_failure_learning.get("candidate_id") if isinstance(latest_failure_learning, dict) else None
                        task["failed_commit"] = latest_failure_learning.get("failed_commit") if isinstance(latest_failure_learning, dict) else None
                        task["health_reasons"] = latest_failure_learning.get("health_reasons") if isinstance(latest_failure_learning, dict) else None
                elif task.get("task_id") == "record-reward":
                    task["status"] = "pending" if completion_target_id != "record-reward" else "active"
                elif task.get("status") == "active":
                    task["status"] = "pending"
            if not any(task.get("task_id") == completion_target_id for task in tasks):
                task_payload = {"task_id": completion_target_id, "title": completion_target_title, "status": "active"}
                if completion_target_id == "analyze-last-failed-candidate" and isinstance(latest_failure_learning, dict):
                    task_payload.update({
                        "kind": "review",
                        "acceptance": "produce a bounded explanation of the failed candidate and one safer follow-up mutation idea",
                        "selection_source": completion_selection_source,
                        "failed_candidate_id": latest_failure_learning.get("candidate_id"),
                        "failed_commit": latest_failure_learning.get("failed_commit"),
                        "health_reasons": latest_failure_learning.get("health_reasons"),
                    })
                tasks.append(task_payload)
            current_task_id = completion_target_id
            if terminal_selfevo_issue is not None and not is_synthesized_materialization:
                feedback_decision = {
                    "mode": "retire_terminal_selfevo_lane",
                    "reason": "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate",
                    "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                    "current_task_id": completed_materialization_task_id,
                    "current_task_class": _task_action_class(completed_materialization_task_id),
                    "selected_task_id": "record-reward",
                    "selected_task_class": _task_action_class("record-reward"),
                    "selection_source": completion_selection_source,
                    "selected_task_title": "Record cycle reward",
                    "selected_task_label": "Record cycle reward [task_id=record-reward]",
                    "terminal_selfevo_issue": terminal_selfevo_issue,
                }
            else:
                feedback_decision = {
                    "mode": "complete_active_lane",
                    "reason": completion_reason,
                    "reward_value": reward_signal.get("value") if isinstance(reward_signal, dict) else None,
                    "current_task_id": completed_materialization_task_id,
                    "current_task_class": _task_action_class(completed_materialization_task_id),
                    "selected_task_id": completion_target_id,
                    "selected_task_class": _task_action_class(completion_target_id),
                    "selection_source": completion_selection_source,
                    "selected_task_title": completion_target_title,
                    "selected_task_label": f"{completion_target_title} [task_id={completion_target_id}]",
                    "artifact_path": materialized_improvement_artifact_path,
                }
                if completion_target_id == "analyze-last-failed-candidate" and isinstance(latest_failure_learning, dict):
                    feedback_decision["failure_learning"] = latest_failure_learning
            active_artifact_path = materialized_improvement_artifact_path
    latest_noop = _safe_read_json(workspace / "state" / "self_evolution" / "runtime" / "latest_noop.json") or {}
    subagent_lane_health = _subagent_lane_health(state_root=goals_dir.parent, current_task_id=current_task_id)
    should_retire_subagent_lane = (
        current_task_id == "subagent-verify-materialized-improvement"
        and not (isinstance(feedback_decision, dict) and feedback_decision.get("mode") in {"execute_queued_revert", "handoff_to_subagent_verification"})
        and (
            latest_noop.get("status") == "terminal_noop"
            or subagent_lane_health.get("state") in {"stale", "completed"}
            or (experiment.get("outcome") == "discard" and experiment.get("revert_status") == "skipped_no_material_change")
        )
    )
    if should_retire_subagent_lane:
        subagent_retirement_target_id = "record-reward"
        subagent_retirement_target_title = "Record cycle reward"
        if latest_noop.get("status") == "terminal_noop":
            subagent_retirement_selection_source = "feedback_terminal_noop_retire"
        elif subagent_lane_health.get("state") == "completed":
            subagent_retirement_selection_source = "feedback_completed_subagent_retire"
        else:
            subagent_retirement_selection_source = "feedback_stale_subagent_retire"
        if failure_learning_is_fresh and isinstance(latest_failure_learning, dict):
            subagent_retirement_target_id = "analyze-last-failed-candidate"
            subagent_retirement_target_title = "Analyze the last failed self-evolution candidate before retrying mutation"
            subagent_retirement_selection_source = "feedback_stale_subagent_retire_to_failure_learning"
        for task in tasks:
            if task.get("task_id") == "subagent-verify-materialized-improvement":
                task["status"] = "blocked" if subagent_lane_health.get("state") == "stale" else "done"
                task["terminal_reason"] = "terminal_noop_or_no_material_change"
            elif task.get("task_id") == subagent_retirement_target_id:
                task["status"] = "active"
                if subagent_retirement_target_id == "analyze-last-failed-candidate" and isinstance(latest_failure_learning, dict):
                    task["selection_source"] = subagent_retirement_selection_source
                    task["failed_candidate_id"] = latest_failure_learning.get("candidate_id")
                    task["failed_commit"] = latest_failure_learning.get("failed_commit")
                    task["health_reasons"] = latest_failure_learning.get("health_reasons")
            elif task.get("status") == "active":
                task["status"] = "pending"
        if not any(task.get("task_id") == subagent_retirement_target_id for task in tasks):
            task_payload = {"task_id": subagent_retirement_target_id, "title": subagent_retirement_target_title, "status": "active"}
            if subagent_retirement_target_id == "analyze-last-failed-candidate" and isinstance(latest_failure_learning, dict):
                task_payload.update({
                    "kind": "review",
                    "acceptance": "produce a bounded explanation of the failed candidate and one safer follow-up mutation idea",
                    "selection_source": subagent_retirement_selection_source,
                    "failed_candidate_id": latest_failure_learning.get("candidate_id"),
                    "failed_commit": latest_failure_learning.get("failed_commit"),
                    "health_reasons": latest_failure_learning.get("health_reasons"),
                })
            tasks.append(task_payload)
        current_task_id = subagent_retirement_target_id
        if latest_noop.get("status") == "terminal_noop":
            _retirement_mode = "retire_terminal_noop_lane"
        elif subagent_lane_health.get("state") == "completed":
            _retirement_mode = "retire_completed_subagent_lane"
        else:
            _retirement_mode = "retire_stale_subagent_lane"
        feedback_decision = {
            "mode": _retirement_mode,
            "reason": "subagent verification lane reached a terminal no-op/discard/stale/completed state and must not keep producing PASS-only telemetry",
            "current_task_id": "subagent-verify-materialized-improvement",
            "current_task_class": _task_action_class("subagent-verify-materialized-improvement"),
            "selected_task_id": subagent_retirement_target_id,
            "selected_task_class": _task_action_class(subagent_retirement_target_id),
            "selection_source": subagent_retirement_selection_source,
            "selected_task_title": subagent_retirement_target_title,
            "selected_task_label": f"{subagent_retirement_target_title} [task_id={subagent_retirement_target_id}]",
            "latest_noop": latest_noop if latest_noop else None,
            "subagent_lane_health": subagent_lane_health,
        }
        if subagent_retirement_target_id == "analyze-last-failed-candidate" and isinstance(latest_failure_learning, dict):
            feedback_decision["failure_learning"] = latest_failure_learning
    # Single-pass task count aggregation — O(n) instead of O(3n) with three separate sum() scans
    _done = _active = _pending = 0
    for _t in tasks:
        _s = _t["status"]
        if _s == "done":
            _done += 1
        elif _s == "active":
            _active += 1
        elif _s == "pending":
            _pending += 1
    task_counts = {
        "total": len(tasks),
        "done": _done,
        "active": _active,
        "pending": _pending,
    }
    current_task_title = _task_title_for_id(current_task_id, tasks, combined_candidates)

    # Issue #697 idle backstop: cycles_since_productive_spawn is persisted in
    # this same current.json snapshot (no new state file) and read back next
    # cycle by cycle_feedback._derive_feedback_decision. Reset to 0 only when
    # THIS cycle's live subagent request files include a genuinely new,
    # non-already_done productive request that wasn't already known last
    # cycle; otherwise increment.
    _prior_snapshot_for_backstop = _safe_read_json(goals_dir / "current.json")
    _prior_cycles_since_productive_spawn = 0
    _prior_known_productive_request_ids: set[str] = set()
    _prior_reward_accounted_artifact_path = None
    if isinstance(_prior_snapshot_for_backstop, dict):
        try:
            _prior_cycles_since_productive_spawn = int(_prior_snapshot_for_backstop.get("cycles_since_productive_spawn") or 0)
        except (TypeError, ValueError):
            _prior_cycles_since_productive_spawn = 0
        _prior_known_productive_request_ids = set(_prior_snapshot_for_backstop.get("_productive_request_ids") or [])
        _prior_reward_accounted_artifact_path = _prior_snapshot_for_backstop.get("generation_reward_accounted_artifact_path")
    _current_productive_request_ids = _productive_subagent_request_ids(goals_dir.parent)
    if _current_productive_request_ids - _prior_known_productive_request_ids:
        cycles_since_productive_spawn = 0
    else:
        cycles_since_productive_spawn = _prior_cycles_since_productive_spawn + 1

    # Issue #697: carry forward the reward-accounted-artifact-path marker
    # unless this cycle's feedback_decision just accounted reward for a new
    # generation (decide_next_lane step 8, cycle_feedback._generation_restart_
    # if_ready) — a monotonic, self-clearing fact keyed on artifact_path, never
    # a same-task decision replay R11 could misread as a stall.
    _reward_accounted_artifact_path = _prior_reward_accounted_artifact_path
    if isinstance(feedback_decision, dict) and feedback_decision.get("reward_accounted_artifact_path"):
        _reward_accounted_artifact_path = feedback_decision.get("reward_accounted_artifact_path")

    payload = {
        "schema_version": TASK_PLAN_VERSION,
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "active_goal": goal_id,
        "result_status": result_status,
        "approval_gate_state": approval_gate_state,
        "next_hint": next_hint,
        "blocked_next_step": blocked_next_step,
        "current_task_id": current_task_id,
        "current_task": current_task_title,
        "task_counts": task_counts,
        "tasks": tasks,
        "reward_signal": reward_signal,
        "feedback_decision": feedback_decision,
        "next_cycle_task_id": feedback_decision.get("selected_task_id") if feedback_decision else None,
        "next_cycle_task_class": feedback_decision.get("selected_task_class") if feedback_decision else None,
        "mutation_lane": _derive_mutation_lane(
            current_task_id=current_task_id,
            selected_tasks=feedback_decision.get("selected_tasks") if isinstance(feedback_decision, dict) else None,
            task_selection_source=feedback_decision.get("selection_source") if isinstance(feedback_decision, dict) else None,
        ),
        "budget": experiment["budget"],
        "budget_policy": experiment.get("budget_policy"),
        "budget_used": experiment["budget_used"],
        "experiment": experiment,
        "report_path": str(report_path),
        "history_path": str(history_path),
        "generated_candidates": combined_candidates,
        "failure_learning": _latest_failure_learning(workspace),
        "materialized_improvement_artifact_path": active_artifact_path,
        "cycles_since_productive_spawn": cycles_since_productive_spawn,
        "_productive_request_ids": sorted(_current_productive_request_ids),
        "generation_reward_accounted_artifact_path": _reward_accounted_artifact_path,
    }

    if file_action is not None:
        payload["file_action"] = file_action
    if verification_command is not None:
        payload["verification_command"] = verification_command
    return payload
