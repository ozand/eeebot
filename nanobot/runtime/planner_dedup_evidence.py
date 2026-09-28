"""ADR-035 rest amendment (PR #1964, folded into #1942 B2): `rejected_duplicate`.

"A duplicate is a recorded outcome too. When the chosen increment is
refused by the duplicate check, the cycle records `rejected_duplicate`
with the evidence sha and reason, runs nothing in its place, and does not
count toward `no_plan`. The sha and the reason are an input to the next
session, or it would choose the same increment again."

This module owns that hand-off: :func:`record_rejected_duplicate` persists
the evidence; :func:`consume_pending_evidence` reads it back for the next
planning session's context and clears it -- "the next session" is singular,
so a rejection is surfaced exactly once, not on every subsequent tick.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_STATE_RELPATH = ("planner", "rejected_duplicate.json")
_SCHEMA = "planner-rejected-duplicate-v1"


def _state_path(state_dir: "Path") -> "Path":
    return Path(state_dir).joinpath(*_STATE_RELPATH)


def record_rejected_duplicate(
    state_dir: "Path", cycle_id: str, title: str, evidence_sha: str, reason: str,
) -> None:
    """Record a `rejected_duplicate` outcome: the harness's dedup gate
    refused the plan's own chosen increment. Never a substitution -- the
    caller runs nothing else in this cycle's place.
    """
    from nanobot.runtime.cycle_ledger import append_event

    payload = {
        "schema": _SCHEMA,
        "cycle_id": cycle_id or "",
        "title": title or "",
        "evidence_sha": evidence_sha or "",
        "reason": reason or "",
    }
    path = _state_path(state_dir)
    tmp_name: "str | None" = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as fh:
            tmp_name = fh.name
            fh.write(json.dumps(payload, indent=2, ensure_ascii=False))
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
        tmp_name = None
    except Exception:
        pass
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    append_event(state_dir, {
        "phase": "rejected_duplicate",
        "cycle_id": cycle_id or "",
        "title": title or "",
        "evidence_sha": evidence_sha or "",
        "reason": reason or "",
    })


def consume_pending_evidence(state_dir: "Path") -> "dict[str, Any] | None":
    """Read and clear the pending rejection, if any -- delivered to exactly
    one following planning session."""
    path = _state_path(state_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return None
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    return {
        "title": raw.get("title", ""),
        "evidence_sha": raw.get("evidence_sha", ""),
        "reason": raw.get("reason", ""),
        "cycle_id": raw.get("cycle_id", ""),
    }


def render_dedup_evidence_block(evidence: "dict[str, Any] | None") -> str:
    """Render the consumed evidence for the planner's context, or ``""``
    when there is none -- never fabricate a block for a session with
    nothing to report."""
    if not evidence:
        return ""
    title = evidence.get("title") or "(untitled)"
    sha = evidence.get("evidence_sha") or "(no sha recorded)"
    reason = evidence.get("reason") or "(no reason recorded)"
    return (
        "## Last increment rejected as a duplicate\n"
        f"\"{title}\" was already done by commit {sha} ({reason}). "
        "Do not choose this increment again."
    )
