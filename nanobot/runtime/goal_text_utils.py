"""Goal-text priority filtering and git-log done-detection heuristics.

Extracted from the now-deleted `cycle_planning.py` (issue #916): this module
holds the three functions that survived the coordinator decommission because
`bridge.py`, `demand.py`, and `llm_proposer.py` import them directly —
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
    Priorities that name a concrete target file must be judged by evidence
    tied to their requested behavior (see ``_priority_done_by_artifact``);
    this helper is the first step — pulling
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
    """Return whether a priority naming a target file is done, by request evidence.

    Issue #748: replaces the word-overlap heuristic as the PRIMARY signal for
    priorities that name a target file (all of ours do) — the word heuristic
    is demoted to a fallback used only when no target file is found (see
    ``_priority_target_file``).

    Returns:
      - ``None`` if no target file path is present in ``entry_text`` or
        ``selfevo_repo_root`` is unavailable — caller must fall back to
        ``_title_already_done_in_git_log``.
      - ``True`` iff the entry's verbatim priority label appears in git log.
      - ``False`` otherwise (file absent, or no commit evidence), and on any
        internal error (fail-open toward "not done", matching this module's
        existing convention — a false "done" actively tells the LLM not to do
        real outstanding work, which is the exact bug this issue fixes).

    Evidence, strongest first (#748 follow-up, live 2026-07-15):

    1. The entry's verbatim ``Priority N — <title prefix>`` label appears in
       the recent git log (integrated cycles auto-commit the proposal title
       verbatim) → ``True`` regardless of anything else.
    2. A target path, basename, or a named function definition is not enough
       to prove the whole request: an artifact can predate, belong to a
       different priority, or implement only part of the request (#1629).
    3. Every other target-bearing request is retained unless the completed
       demand sidecar (checked by the caller first) or verbatim label proves
       it done. There is no honest generic artifact criterion for those forms.
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
        # A target can predate or satisfy only part of this priority. Never
        # infer request completion from its existence or contents (#1629).
        return False
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
    Issue #1629: done-ness for a target-bearing priority is decided by
    completed-demand evidence or a verbatim priority label — never a target
    file's existence or contents.
    The original word-overlap heuristic
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
                # #1629: request evidence first; the word heuristic remains
                # a fallback only when the entry names no target file.
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


def _recent_git_log(repo_root: Path, since: str = "14 days ago") -> "str | None":
    """Return `git log --oneline --since=<since>` output for repo_root, or
    ``None`` on any failure.

    Shared helper: both `_curriculum_level` (MEMORY.md backlog) and
    `_parse_backlog_task_from_goal_text` (goal_text.json priorities) need
    "recent git log text for a repo" to feed the done-detection heuristic (#575).

    ADR-035 keep-work architect addendum (#1942 B2): excludes residual
    auto-commits and per-step checkpoint commits (`commit_markers.
    ARTIFICIAL_COMMIT_GREP_PATTERNS`) -- this IS a "is this already done"
    reader, and a checkpoint's own path-naming subject, or a residual
    commit's, must never satisfy it. NOT switched to ``--first-parent``
    (unlike the two llm_proposer #903/self_dedup readers): this reader
    matches on commit SUBJECT text only (``--oneline``), and the
    keep-work merge commit's own subject is the fixed, generic "merge:
    integrate <cycle_branch>" (point 2) -- a real work commit's subject
    (the one this heuristic is designed to match) lives on the cycle
    branch itself, which ``--first-parent`` would hide entirely. Plain
    history (every commit, branch-internal included) is still what makes
    the verbatim-label match work; the invert-grep above is what keeps
    checkpoint/residual noise out of it.

    ADR-035 keep-work architect resolution, point 4: returns ``None``
    (never ``""``) on a git error/timeout -- an empty result string must
    mean "confirmed no matching commits in the window", not "couldn't
    tell". ``""`` (genuinely empty) and ``None`` (unavailable) both read
    as falsy to every existing caller here, which already treats "no
    evidence" as "not done" (the safe default either way) -- but the two
    cases are no longer conflated at the source.
    """
    import subprocess as _sp

    from nanobot.runtime.commit_markers import ARTIFICIAL_COMMIT_GREP_PATTERNS

    git_cmd = [
        "git", "-c", f"safe.directory={repo_root}",
        "-C", str(repo_root),
        "log", "--oneline", f"--since={since}",
        "--invert-grep", "-i",
        *(f"--grep={p}" for p in ARTIFICIAL_COMMIT_GREP_PATTERNS),
    ]
    try:
        result = _sp.run(git_cmd, capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


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


#: #1785: a trailing goal-vector tag on a proposal title, e.g. "(V1)"/"(V2)".
#: The proposer's own titles carry these (see llm_proposer's task_title
#: schema); a retag (V1 -> V2) on an otherwise-identical title must not mint
#: a new candidate identity.
_VERSION_TAG_PATTERN = r"\(\s*V\d+\s*\)"

#: #1785: the "Priority N — <title>" label a goal_text.json priority carries
#: (same pattern _priority_label_prefix above matches) -- when a fallback
#: proposal's title is lifted verbatim from a numbered priority, the number
#: is a locator, not part of the candidate's identity.
_PRIORITY_PREFIX_PATTERN = r"^Priority\s+\d+\s*[—–-]\s*"


def normalize_candidate_title(title: str) -> str:
    """#1785: fold version-tag and priority-prefix variants of a title into
    one candidate identity.

    The rule, stated once: two proposal titles are the SAME candidate iff,
    after (1) stripping a leading ``Priority N — `` label, (2) stripping a
    trailing ``(V<N>)`` tag, (3) case-folding, and (4) collapsing
    whitespace, they are BYTE-IDENTICAL. This is deliberately an exact
    match on the normalized form, not a fuzzy one — ADR-pending #1785 found
    the previous word-overlap heuristic's fuzziness was exactly why it
    misfired on 45 of 48 real rejections sampled on 2026-09-19 (different
    files, different surfaces, sharing enough long words to clear a 60%
    threshold). Loosening the match again would reintroduce that failure
    mode; this function is intentionally conservative.

    Measured against a real week of self-dedup rejections
    (2026-09-12..2026-09-19, 342 rows): this rule alone collapses 71
    distinct raw ``task_title`` values down to 69 distinct identities (the
    "Priority 43 — Filter fallback candidates dedup (V1)" /
    "Filter fallback candidates dedup (V1)" pair is exactly the case it was
    built for). Most of the week's repetition is NOT a retag at all — the
    same raw title is proposed again verbatim — which is why the treadmill
    fix (:func:`nanobot.runtime.llm_proposer._recently_self_dedup_rejected`)
    does not depend on this function alone; see its own docstring.
    """
    import re as _re

    text = (title or "").strip()
    if not text:
        return ""
    text = _re.sub(_PRIORITY_PREFIX_PATTERN, "", text)
    text = _re.sub(_VERSION_TAG_PATTERN, "", text)
    text = _re.sub(r"\s+", " ", text).strip()
    return text.rstrip(":").strip().lower()
