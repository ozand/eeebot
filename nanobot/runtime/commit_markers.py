"""Commit-subject/trailer markers for commits the harness writes about its
own process, never about real work -- residual auto-commits (#666/#1906/
#1910) and, per ADR-035's keep-work amendment (#1942 B2), per-completed-step
checkpoint commits.

Centralized so every "is this already done" git-log reader (self_dedup's
:func:`nanobot.runtime.llm_proposer._latest_non_residual_commit_for_path`,
the recent-activity/do-not-repeat window in
:func:`nanobot.runtime.bridge._recent_commits_with_paths`/
``_recent_activity_context``) stays in sync. Before this module existed each
reader copied the residual pattern list by hand; a reader that copied it
before #1942 B2 would silently miss the new checkpoint markers, and a
checkpoint commit's own subject (which names the paths it touched, the same
shape as real work) would read as a false self_dedup/novelty-pressure match
-- the #1785 class of defect. One shared list means adding a marker here
covers every enforcement point at once.
"""
from __future__ import annotations

#: Regex ``--grep`` patterns (case-insensitive, anchored) for
#: ``git log --invert-grep``, matching either the trailer (subject+body) or
#: a literal subject prefix.
ARTIFICIAL_COMMIT_GREP_PATTERNS: tuple[str, ...] = (
    "^Selfevo-Residual: true",
    "^selfevo: auto-commit uncommitted subagent work",
    "^selfevo: auto-commit residual state",
    "^Selfevo-Checkpoint: true",
    "^selfevo: checkpoint",
)

#: Plain lowercase subject-line prefixes for readers that already hold just
#: the subject text (no trailer visibility, e.g. ``%s``-only git output) and
#: filter with ``str.startswith()``.
ARTIFICIAL_COMMIT_SUBJECT_PREFIXES: tuple[str, ...] = (
    "selfevo: auto-commit residual state",
    "selfevo: auto-commit uncommitted subagent work",
    "selfevo: checkpoint",
)

#: The checkpoint commit's own subject prefix and trailer (ADR-035 keep-work,
#: #1942 B2) -- written by ``nanobot.agent.subagent``'s per-step checkpoint
#: and read back by every exclusion site above.
CHECKPOINT_SUBJECT_PREFIX = "selfevo: checkpoint"
CHECKPOINT_TRAILER = "Selfevo-Checkpoint: true"

#: Secret-shaped/lockfile filenames no self-authored commit may ever stage.
#: The single source: ``nanobot.runtime.bridge`` imports this as
#: ``_BLOCKED_FILE_PATTERNS`` (its residual auto-commit and
#: ``_is_blocked_filename``'s mutation-surface gate both read it),
#: ``nanobot.agent.subagent``'s checkpoint writer reads it directly. Living
#: here rather than in bridge.py is what makes the import safe in both
#: directions: bridge.py already imports ``nanobot.agent.subagent``
#: (``SubagentManager``), so subagent.py importing bridge.py back would be a
#: cycle -- this module has no imports of either, so both can import IT.
BLOCKED_FILE_PATTERNS: tuple[str, ...] = (
    ".env", ".git", ".npmrc", "package-lock", "yarn.lock", "id_rsa", "private_key",
)


def is_artificial_commit_subject(subject: str) -> bool:
    """True if *subject* (a commit's first line) is a self-authored
    bookkeeping/checkpoint commit, never a reader's evidence of real work."""
    subj_lower = (subject or "").strip().lower()
    return subj_lower.startswith(ARTIFICIAL_COMMIT_SUBJECT_PREFIXES)


def is_blocked_checkpoint_filename(path: str) -> bool:
    """True if *path* matches a secret-shaped/lockfile pattern
    (:data:`BLOCKED_FILE_PATTERNS`) that a checkpoint commit must exclude.
    A simplified substring check, not ``bridge._is_blocked_filename``'s
    full basename/stem logic -- this is a defense-in-depth net for the
    checkpoint writer only; ``denied_paths`` at the tool level
    (``WriteFileTool``/``EditFileTool``) is the primary defense against
    writing fitness sidecars at all. The PATTERN LIST is shared with
    ``bridge._is_blocked_filename`` (see :data:`BLOCKED_FILE_PATTERNS`);
    only the matching algorithm around it differs."""
    lowered = (path or "").lower()
    return any(pat in lowered for pat in BLOCKED_FILE_PATTERNS)


#: ADR-035 keep-work, architect resolution on merge-commit provenance
#: (#1942 B2): the merge commit's SUBJECT stays exactly
#: ``"merge: integrate <cycle_branch>"`` -- unchanged, since
#: ``scripts/revert_cycles.py``'s ``MERGE_SUBJECT_RX``, ``bridge.py``'s own
#: ``merge:``-prefix novelty filter, ``scripts/measure_package_1903.py``,
#: and both dashboard generators all match it verbatim. Provenance data
#: goes in the merge commit's BODY as trailers instead. This key (the
#: cycle id) is settled; a second trailer (task text or demand id) is
#: still pending an architect privacy decision and is NOT written yet.
MERGE_TRAILER_CYCLE_KEY = "Selfevo-Cycle"

#: A trailer value's own hard cap -- generous enough for any real title,
#: small enough that a merge commit body never grows unbounded.
_TRAILER_VALUE_MAX_LEN = 120


def sanitize_trailer_value(value: str, max_len: int = _TRAILER_VALUE_MAX_LEN) -> str:
    """Collapse *value* into a single git-trailer-safe line.

    A trailer's value must never contain a literal newline -- ``git
    interpret-trailers`` (and every plain-text reader of a commit body)
    treats a blank/non-"Key: Value" line as the end of the trailer block,
    so an embedded newline would silently truncate or corrupt the value.
    Collapses ALL whitespace runs (including newlines) to a single space,
    strips the result, and truncates to *max_len*. A colon inside the
    value is safe once there is no newline around it -- it can no longer
    be mistaken for the start of a new trailer line.
    """
    import re as _re

    collapsed = _re.sub(r"\s+", " ", str(value or "")).strip()
    return collapsed[:max_len]


#: Sentinel distinguishing "git errored/timed out" from "ran cleanly and
#: found nothing" -- ADR-035 keep-work architect resolution (#1942 B2),
#: point 4: 0/False/None must never mean "unknown", or a transient git
#: failure would read as "confirmed zero" (edit-budget: falsely under
#: budget) or "confirmed not done" (self_dedup: silently loses evidence).
#: Callers must check for this exact object before treating a falsy
#: result as a real answer.
GIT_UNAVAILABLE = "git-unavailable"
