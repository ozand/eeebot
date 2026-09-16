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
        })
    for row in lesson_result.get("zero_citation", []):
        candidates.append({
            "kind": "lesson",
            "id": row["lesson_id"],
            "evidence": (
                f"offered {row['offered_in_window']}x, cited 0x in the observed window"
            ),
            "last_activity": row.get("last_cited") or row.get("last_offered"),
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
