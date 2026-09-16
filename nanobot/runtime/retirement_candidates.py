"""Bounded retirement-candidate evidence for ADR-021 rule 3 — measurement
only, never the retirement itself.

Rule 3: "A trainer that may only add is a commentator. The authority
granted under rule 1 explicitly includes removing a skill or lesson that
measurement shows is never retrieved, or is retrieved and does not help.
Removal is where the damage lives, so it carries the same evidence burden
as addition and is bounded per run." (#1369 is the precedent this bound
exists for: an automatic, uncapped trim blanked eight skills' trigger
descriptions in one pass, restored by hand in #1595.)

This module combines ``skill_fitness.census`` (skills) and
``lesson_v2.lesson_zero_citation_census`` (lessons) into one bounded,
capped list of never-retrieved candidates. It performs no deletion, calls
no gate, and nothing calls it — the evidence a future, separately-reviewed
retirement proposal would cite, not an actor. Candidates are evidence of
NON-USE over an observation window, never an artifact's own age: rule 3's
own text rejects a clock in favor of usage evidence, and #1369's damage
came from exactly that substitution.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.runtime import lesson_v2, skill_fitness

# #1369's automatic trim cost eight skills their trigger descriptions in a
# single uncapped pass. Bounding well under that scale here.
MAX_RETIREMENT_CANDIDATES_PER_RUN = 5


def retirement_candidates(
    state_dir: Path,
    selfevo_repo: Path,
    *,
    now: datetime | None = None,
    max_per_run: int = MAX_RETIREMENT_CANDIDATES_PER_RUN,
) -> dict[str, Any]:
    """Bounded, capped evidence of never-retrieved skills and lessons.

    Returns at most ``max_per_run`` candidates, longest-unused (or never
    used at all) first, and ``bound_hit: True`` whenever there were more
    candidates than the bound allowed — a caller must check this field
    rather than assume the returned list is exhaustive; the Test Contract
    requires a run that hits the bound to report it, not continue silently.

    Each side's own availability is reported separately
    (``skill_census_ok``/``lesson_census_ok`` with a ``reason`` when not
    ok) rather than collapsed into one flag — a missing skill catalogue
    must not silence real lesson evidence, or the reverse.
    """
    skill_result = skill_fitness.census(state_dir, selfevo_repo, now=now)
    lesson_result = lesson_v2.lesson_zero_citation_census(state_dir, now=now)

    candidates: list[dict[str, Any]] = []
    for row in skill_result.get("zero_read", []):
        candidates.append({
            "kind": "skill",
            "id": row["skill"],
            "evidence": "zero confirmed reads in the observed window",
            "last_activity": row.get("last_read"),
            # #1666 phase 3: the raw numbers a retirement citation needs.
            # reads_in_window is always 0 by construction of skill_fitness's
            # zero_read list -- that IS the retrieval_count. There is no
            # skill-side equivalent of a lesson's offered_in_window yet (the
            # catalogue's presence in every system prompt is not the same as
            # a per-skill exposure count, and nothing instruments the
            # latter) -- exposure stays None rather than a fabricated
            # count, and a retire proposal for a skill is declined for
            # exactly that reason (see propose_retirement).
            "retrieval_count": row.get("reads_in_window", 0),
            "exposure": None,
        })
    for row in lesson_result.get("zero_citation", []):
        candidates.append({
            "kind": "lesson",
            "id": row["lesson_id"],
            "evidence": (
                f"offered {row['offered_in_window']}x, cited 0x in the observed window"
            ),
            "last_activity": row.get("last_cited") or row.get("last_offered"),
            "retrieval_count": 0,  # cited 0x, by construction of zero_citation
            "exposure": row["offered_in_window"],
        })

    # Never-active (last_activity None -> "") sorts first: the strongest
    # non-use evidence is "not once", ahead of "not recently".
    candidates.sort(key=lambda row: row.get("last_activity") or "")

    total = len(candidates)
    return {
        "skill_census_ok": bool(skill_result.get("ok")),
        "skill_census_reason": skill_result.get("reason"),
        "lesson_census_ok": bool(lesson_result.get("ok")),
        "lesson_census_reason": lesson_result.get("reason"),
        "total_candidates": total,
        "max_per_run": max_per_run,
        "bound_hit": total > max_per_run,
        "candidates": candidates[:max_per_run],
    }


def propose_retirement(candidate: dict[str, Any]) -> dict[str, Any]:
    """Turn ONE :func:`retirement_candidates` row into a staged retire
    proposal citing the non-use evidence it rests on (ADR-021 rule 3,
    first checkbox). Pure: no I/O, no gate call, no deletion -- this only
    shapes the citation; :func:`knowledge_curator.stage_retirement_proposal`
    is the sole caller and the only place that validates and records one
    (enforced by ``tests/test_trainer_no_direct_mutation.py``).

    The citation's ``retrieval_count`` is the candidate's confirmed-use
    count (always 0 -- that is what makes it a retirement candidate at
    all) and never padded. ``exposure`` is the denominator: for a lesson,
    ``offered_in_window`` (always >= 1, since the census only lists
    lessons offered at least once); for a skill, ``None`` -- there is no
    per-skill offered/exposure count instrumented yet, so a skill retire
    proposal cites what is actually known (zero confirmed reads) without
    fabricating a denominator it doesn't have. ``offered_or_shown`` is
    true only when a real, positive exposure count backs it; for a skill
    that means the citation itself is invalid (correctly -- #1672's own
    finding is that a bare zero needs a denominator to mean anything, and
    skills don't have one yet).
    """
    kind = str(candidate.get("kind") or "")
    target_id = str(candidate.get("id") or "")
    exposure = candidate.get("exposure")
    return {
        "operation": "retire",
        "kind": kind,
        "target_id": target_id,
        "citation": {
            "kind": kind,
            "target_id": target_id,
            "source": f"retirement_candidates.{kind}_census",
            "retrieval_count": candidate.get("retrieval_count", 0),
            "exposure": exposure,
            "offered_or_shown": isinstance(exposure, int) and exposure > 0,
        },
    }
