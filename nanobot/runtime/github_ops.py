"""Compatibility re-exports for the canonical self-evolution operations.

The implementation lives in :mod:`nanobot.runtime.autoevolve`; this module
preserves the public import path used by deployed callers without maintaining
a second divergent copy. Git staging intentionally uses ``git add -A`` in the
canonical implementation so a legitimate new file is included in a cycle.
"""
from __future__ import annotations

from nanobot.runtime.autoevolve import (
    close_selfevo_issue_if_open,
    commit_and_push_self_evolution,
    ensure_selfevo_issue,
    ensure_selfevo_pr,
    merge_selfevo_pr,
)

__all__ = [
    "close_selfevo_issue_if_open",
    "commit_and_push_self_evolution",
    "ensure_selfevo_issue",
    "ensure_selfevo_pr",
    "merge_selfevo_pr",
]
