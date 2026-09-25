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

#: Secret-shaped/lockfile filenames a checkpoint commit must never stage --
#: mirrors ``nanobot.runtime.bridge._BLOCKED_FILE_PATTERNS`` (duplicated, not
#: imported: that name is AST-scanned by ``tests/test_mutation_surfaces.py``
#: as a bridge.py module-level constant, and ``nanobot.agent.subagent`` --
#: which bridge.py itself imports -- cannot import back from
#: ``nanobot.runtime.bridge`` without a cycle).
CHECKPOINT_BLOCKED_FILE_PATTERNS: tuple[str, ...] = (
    ".env", ".git", ".npmrc", "package-lock", "yarn.lock", "id_rsa", "private_key",
)


def is_artificial_commit_subject(subject: str) -> bool:
    """True if *subject* (a commit's first line) is a self-authored
    bookkeeping/checkpoint commit, never a reader's evidence of real work."""
    subj_lower = (subject or "").strip().lower()
    return subj_lower.startswith(ARTIFICIAL_COMMIT_SUBJECT_PREFIXES)


def is_blocked_checkpoint_filename(path: str) -> bool:
    """True if *path* matches a secret-shaped/lockfile pattern that a
    checkpoint commit must exclude. A simplified substring check, not
    ``bridge._is_blocked_filename``'s full basename/stem logic -- this is a
    defense-in-depth net for the checkpoint writer only; ``denied_paths`` at
    the tool level (``WriteFileTool``/``EditFileTool``) is the primary
    defense against writing fitness sidecars at all."""
    lowered = (path or "").lower()
    return any(pat in lowered for pat in CHECKPOINT_BLOCKED_FILE_PATTERNS)
