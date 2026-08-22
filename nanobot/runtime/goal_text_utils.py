"""Goal-text priority filtering and git-log done-detection heuristics.

Extracted from the now-deleted `cycle_planning.py` (issue #916): this module
holds the three functions that survived the coordinator decommission because
`bridge.py` and `llm_proposer.py` import them directly —
`filter_completed_priorities_from_goal_text`, `_recent_git_log`, and
`_title_already_done_in_git_log` — plus their minimal private closure
(`_priority_done_by_artifact`, `_priority_target_file`,
`_priority_label_prefix`, and the two pattern constants they use). No
behavior change from the move; only import paths change.
"""

from __future__ import annotations

from pathlib import Path

_TARGET_FILE_PATTERN = r"(?:scripts|surfaces|memory|lessons|docs|tests)/[A-Za-z0-9_./-]+\.\w+"

_PRIORITY_LABEL_PATTERN = r"Priority\s+\d+\s*[—–-]\s*[^:.(]{1,40}"


def _priority_label_prefix(entry_text: str) -> str | None:
    """Extract the verbatim ``Priority N — <title prefix>`` label (#748
    follow-up, live 2026-07-15): integrated cycles auto-commit with the
    proposal title verbatim ("selfevo: auto-commit ... — Priority 11 — Loop
    health in dashboard: ..."), so this label appearing in the recent git
    log is the strongest available done-evidence — number AND title words
    must both match, immune to the shared-target-file blind spot in
    ``_priority_done_by_artifact``. Returns None when the entry carries no
    such label or the captured prefix is too short to be distinctive."""
    import re as _re

    m = _re.search(_PRIORITY_LABEL_PATTERN, entry_text)
    if not m:
        return None
    prefix = _re.sub(r"\s+", " ", m.group(0)).strip()
    return prefix if len(prefix) >= 18 else None


def _priority_target_file(entry_text: str) -> str | None:
    """Extract the first repo-relative target file path named in a priority entry.

    Issue #748: the #575/#712 done-detection heuristic (word-overlap against a
    single git-log line) produces false positives on the autonomous loop's
    narrow, repetitive commit vocabulary (e.g. "Loop health in dashboard"
    spuriously matched an unrelated "loop health report script" commit).
    Priorities that name a concrete target file can be judged far more
    precisely by artifact existence + evidence (see
    ``_priority_done_by_artifact``); this helper is the first step — pulling
    that path out of the FULL priority entry text (title + description), not
    just the short title, since the file path usually only appears in the
    description ("write scripts/foo.py that ..."). Matches paths rooted under
    the conventional eeebot-self-evolving directories our priorities target
    (scripts/surfaces/memory/lessons/docs/tests). Returns None if no such path
    is present — callers must then fall back to the word heuristic.
    """
    import re as _re

    match = _re.search(_TARGET_FILE_PATTERN, entry_text)
    return match.group(0) if match else None


def _priority_done_by_artifact(
    entry_text: str, selfevo_repo_root: Path | None, git_log: str
) -> bool | None:
    """Return whether a priority naming a target file is done, by artifact + evidence.

    Issue #748: replaces the word-overlap heuristic as the PRIMARY signal for
    priorities that name a target file (all of ours do) — the word heuristic
    is demoted to a fallback used only when no target file is found (see
    ``_priority_target_file``).

    Returns:
      - ``None`` if no target file path is present in ``entry_text`` or
        ``selfevo_repo_root`` is unavailable — caller must fall back to
        ``_title_already_done_in_git_log``.
      - ``True`` iff the target file exists in ``selfevo_repo_root`` AND some
        line of ``git_log`` contains the file's exact basename as a
        case-insensitive substring.
      - ``False`` otherwise (file absent, or no commit evidence), and on any
        internal error (fail-open toward "not done", matching this module's
        existing convention — a false "done" actively tells the LLM not to do
        real outstanding work, which is the exact bug this issue fixes).

    Evidence, strongest first (#748 follow-up, live 2026-07-15):

    1. The entry's verbatim ``Priority N — <title prefix>`` label appears in
       the recent git log (integrated cycles auto-commit the proposal title
       verbatim) → ``True`` regardless of anything else.
    2. Target file existence + exact-basename-in-log — but ONLY for
       creation-type entries. For "extend"-type entries this evidence is
       structurally blind: the residual risk documented in earlier revisions
       fired live on 2026-07-15 (Priority 14 "extend scripts/
       eeebot_dashboard.py" read as done because the file pre-existed from
       Priority 7 and its basename appeared in Priority 11's commits —
       the R30 wake-up never happened). An extend entry with no label
       evidence is NOT done.
    """
    try:
        target = _priority_target_file(entry_text)
        if target is None:
            return None
        if selfevo_repo_root is None or not selfevo_repo_root.is_dir():
            return None
        label = _priority_label_prefix(entry_text)
        if label and git_log and label.lower() in git_log.lower():
            return True
        basename = target.rsplit("/", 1)[-1]
        if not basename:
            return None
        file_path = selfevo_repo_root / target
        if not file_path.exists():
            return False
        if not git_log:
            return False
        if basename.lower() not in git_log.lower():
            return False
        # Basename evidence is conclusive only when this entry CREATED the
        # file; a modify-existing entry's target pre-exists by definition, so
        # its existence proves nothing about THIS entry's work (#748
        # follow-up). Modify verbs beyond "extend": live evidence 2026-07-18
        # — P16 phrased "add ONE function ... to scripts/eeebot_dashboard.py"
        # slipped past the extend-only check and was falsely filtered as
        # done (its R30 wake-up never fired).
        import re as _re

        if _re.search(
            r"\bextend\b|\bupdate\b|\badd\b[^.]{0,80}?\bto\b", entry_text, _re.IGNORECASE
        ):
            return False
        return True
    except Exception:
        return False


def filter_completed_priorities_from_goal_text(
    raw_text: str, selfevo_repo_root: Path | None, *, state_dir: Path | None = None
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
    `_parse_backlog_task_from_goal_text` to enumerate priority entries.
    Issue #748: done-ness is now decided primarily by
    `_priority_done_by_artifact` (target-file existence + exact-basename
    commit evidence), since the original word-overlap heuristic
    (`_title_already_done_in_git_log`) produced confirmed false positives on
    short titles against the autonomous loop's narrow, repetitive commit
    vocabulary (e.g. "Loop health in dashboard" spuriously matched an
    unrelated "loop health report script" commit — see issue #748 evidence).
    The word heuristic remains as a fallback for entries that name no target
    file. Fail-open (matching this module's existing convention): returns
    `raw_text` unchanged if `selfevo_repo_root` is None/not a directory, the
    marker/regex don't match, or on any exception.

    Issue #773: when `state_dir` is given, the completed-demand sidecar
    (`<state_dir>/demand/completed.json`, folded from the ledger chain
    `proposed`-row-with-`demand_id` → same-cycle terminal `outcome: success`
    by `demand._fold_completed`) is checked FIRST: a priority entry whose
    derived demand id (the same `_make_item("priority", "Priority N — Title",
    instructions)` hash `demand._priority_items` computes) is in the sidecar
    is done, regardless of what the git-log heuristics say. This is the only
    done-signal that works for demand-mode integrations, where the model
    refines the proposal title and the auto-commit therefore carries no
    verbatim `Priority N —` label (live P14 evidence, 2026-07-15/16).
    `demand` is imported lazily — it imports this module, so a module-level
    import here would be a cycle. Without `state_dir` behavior is unchanged.
    """
    try:
        if not isinstance(raw_text, str):
            return raw_text

        completed_ids: set[str] = set()
        if state_dir is not None:
            try:
                from nanobot.runtime import demand as _demand

                completed_ids = _demand.completed_demand_ids(Path(state_dir))
            except Exception:
                completed_ids = set()

        repo_ok = selfevo_repo_root is not None and selfevo_repo_root.is_dir()
        if not repo_ok and not completed_ids:
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

        git_log = _recent_git_log(selfevo_repo_root) if repo_ok else ""
        if not git_log and not completed_ids:
            return raw_text

        kept_entries: list[str] = []
        done_titles: list[str] = []
        for m in matches:
            num, title = m.group(1), m.group(2).strip()
            entry_text = m.group(0)
            done: bool | None = False
            # Issue #773: ledger-chain done-truth first — a priority whose
            # derived demand id is in the completed sidecar is done, no
            # text evidence needed (demand-mode integrations carry none).
            if completed_ids:
                try:
                    from nanobot.runtime import demand as _demand

                    derived = _demand._make_item(
                        "priority", f"Priority {num} — {title}", m.group(3).strip()
                    )
                    done = derived["id"] in completed_ids
                except Exception:
                    done = False
            if not done and git_log:
                # Issue #748: artifact+evidence first (precise), word
                # heuristic only as fallback when the entry names no target
                # file — see _priority_done_by_artifact's docstring.
                done = _priority_done_by_artifact(entry_text, selfevo_repo_root, git_log)
                if done is None:
                    done = _title_already_done_in_git_log(title, git_log)
            if done:
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
