"""Cycle stop-guards — bounded revisions and smoke repair outcomes.

Implements R12 of ``docs/specs/self-evolving-runtime/spec.md`` as small,
pure functions so the behaviour is unit-testable without importing the heavy
coordinator runtime. The subagent bridge wires :func:`revision_outcome` into
its smoke-gate repair loop.
"""
from __future__ import annotations

from typing import Any

# R12: a failed gate may be revised at most this many times before "blocked".
REVISION_CAP_DEFAULT = 3


def revision_outcome(
    *,
    revisions: int,
    smoke_passed: bool,
    cap: int = REVISION_CAP_DEFAULT,
    last_smoke_output: str | None = None,
) -> dict[str, Any]:
    """Summarise a smoke-gate repair loop for the bridge result artifact (R12).

    Returns a record whose ``outcome`` is ``"blocked"`` once the revision cap is
    reached without passing — revisions are never unbounded.

    last_smoke_output: the most recent smoke-test output (stdout+stderr, already
    tail-truncated by the caller), persisted verbatim (re-truncated to 2000 chars
    here as a safety net) so a failed gate is diagnosable from the result
    artifact alone without a manual worktree re-run (#668).
    """
    cap = max(0, int(cap))
    revisions = max(0, int(revisions))
    capped = (not smoke_passed) and revisions >= cap
    if smoke_passed:
        outcome = "passed"
    elif capped:
        outcome = "blocked"
    else:
        outcome = "unresolved"
    record: dict[str, Any] = {
        "gate": "smoke",
        "count": revisions,
        "max": cap,
        "smoke_passed": bool(smoke_passed),
        "capped": capped,
        "outcome": outcome,
    }
    if last_smoke_output is not None:
        record["last_smoke_output"] = last_smoke_output[-2000:]
    return record
