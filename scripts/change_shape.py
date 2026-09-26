"""Deterministic, reporting-only classification of integrated change shape."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

SHAPE_CLASSES = frozenset({"feature", "maintenance", "documentation", "testing", "performance", "knowledge", "unclassified"})
_CONVENTIONAL = re.compile(r"^([a-zA-Z][\w+.-]*)(?:\([^)]*\))?!?:\s*(.*)$")


def classify_subject(subject: str | None) -> str:
    """Classify a conventional commit subject without model calls or side effects."""
    text = str(subject or "").strip()
    match = _CONVENTIONAL.match(text)
    if not match:
        if text.lower().startswith("revert "):
            return "maintenance"
        return "unclassified"
    kind, body = match.group(1).lower(), match.group(2).strip()
    verb_match = re.match(r"([\w-]+)", body)
    verb = verb_match.group(1).lower() if verb_match else ""
    lower = body.lower()
    if any(term in lower for term in ("knowledge", "lesson", "memory", "curator", "hypothesis", "reflector")):
        return "knowledge"
    if kind in {"docs", "doc"}:
        return "documentation"
    if kind in {"test", "tests"}:
        return "testing"
    if kind in {"perf", "performance"} or verb in {"optimize", "optimise", "speed", "accelerate", "benchmark"}:
        return "performance"
    if kind in {"feat", "feature"} or verb in {"add", "implement", "create", "introduce", "enable", "support", "extend", "allow", "make", "build", "write"}:
        return "feature"
    if kind in {"fix", "bugfix", "revert", "refactor", "style", "chore", "build", "ci", "autoevolve", "auto", "gate", "deploy", "release", "security", "ops"} or verb in {"fix", "repair", "restore", "correct", "resolve", "handle", "prevent", "avoid", "refactor", "clean", "remove", "drop", "retire", "update", "sync", "simplify", "isolate", "formalize", "extract", "enhance", "expose"}:
        return "maintenance"
    return "unclassified"


def classify_commit_subjects(subjects: list[str]) -> dict[str, int]:
    counts = Counter(classify_subject(subject) for subject in subjects)
    return {shape: counts.get(shape, 0) for shape in sorted(SHAPE_CLASSES)}


def distribution_from_rows(rows: list[dict[str, Any]], *, now: Any = None, days: int = 7) -> dict[str, Any]:
    """Return a named-window distribution from terminal outcome rows.

    Only integrated (`outcome == success`) rows participate, and subjects are
    expected on the integration row as `change_subject`. Missing subjects are
    explicit `unclassified` rather than silently dropped.
    """
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    selected = []
    for row in rows:
        if row.get("phase") != "outcome" or row.get("outcome") != "success":
            continue
        raw = str(row.get("ts") or "").replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(raw)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if start <= stamp <= end:
            selected.append(row)
    counts = Counter(
        str(row.get("change_shape"))
        if row.get("change_shape") in SHAPE_CLASSES
        else "unclassified"
        for row in selected
    )
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "integrated_cycles": len(selected),
        "distribution": {shape: counts.get(shape, 0) for shape in sorted(SHAPE_CLASSES)},
    }


def classify_integrated_commits(repo_root: Any, *, limit: int = 2288) -> dict[str, Any]:
    """Classify recent non-merge commit subjects from git history.

    Small item (ADR-035 Test Contract, #1962): excludes residual/checkpoint
    commits via the SAME shared ``--invert-grep`` pattern list every other
    "is this already done" git-log reader uses
    (``nanobot.runtime.commit_markers.ARTIFICIAL_COMMIT_GREP_PATTERNS``) --
    without it, a branch's own checkpoint commits (their subjects name the
    paths they touched, the same shape as real work) inflate this report's
    commit count and distribution as if they were real, classified work.
    """
    import subprocess

    from nanobot.runtime.commit_markers import ARTIFICIAL_COMMIT_GREP_PATTERNS
    try:
        completed = subprocess.run(
            [
                "git", "log", "--format=%s", "--no-merges",
                "--invert-grep", "-i",
                *(f"--grep={p}" for p in ARTIFICIAL_COMMIT_GREP_PATTERNS),
                f"-n{limit}",
            ],
            cwd=str(repo_root), capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unavailable", "reason": f"git history probe failed: {type(exc).__name__}", "commit_count": 0, "distribution": {shape: 0 for shape in sorted(SHAPE_CLASSES)}}
    subjects = [line for line in completed.stdout.splitlines() if line.strip()]
    counts = classify_commit_subjects(subjects)
    return {"status": "present", "window": f"latest {len(subjects)} non-merge commits", "commit_count": len(subjects), "distribution": counts}


def integrated_shape_from_subject(subject: str | None) -> dict[str, Any]:
    """Return the additive ledger field for one integrated commit subject."""
    return {"change_shape": classify_subject(subject), "change_subject": str(subject or "").strip()[:200]}
