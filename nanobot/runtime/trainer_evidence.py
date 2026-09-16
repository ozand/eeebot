"""Trainer evidence-citation gate — ADR-021 rule 4 (#1666 phase 3 follow-on).

"'This helped' is a claim and needs a row. A model's impression that a
skill was useful is not evidence. A proposal to add, change or retire
cites retrieval counts and outcomes, exactly as ADR-015 requires of a
narrated claim and ADR-019 of an optimization." This is the third rule of
that shape in this codebase — this module mirrors
:mod:`benchmark_evidence` (#813/#1667) directly: schema-validate the
CITATION'S SHAPE, trust no prose, and require an explicit statement when no
source is available rather than silently defaulting to "unevidenced but
unblocked".

Scope: this validates a citation for a proposed skill/lesson CHANGE or
RETIREMENT — the case #1672's censuses already measure. An "add" proposal
cites a different kind of rationale (a recurring gap, not retrieval history
for something that does not exist yet) and is out of scope here.

This module does NOT build the trainer/proposal surface itself — that is
ADR-021 phase 2, blocked on #1664 (the reflector has written nothing since
2026-09-12), and is not this module's job. It does NOT build new retrieval
instrumentation either — rule 1 already covers that, and #1668/#1672
already provide the counts this validator requires a citation to name.
Nothing calls this module yet; it exists so a future proposal surface has
a validator ready the moment it exists, exactly as ``validate_benchmark``
existed as a schema gate before #815 steered the proposer to emit the
signal it validates.

A citation must name:

- ``kind``: ``"skill"`` or ``"lesson"``.
- ``target_id``: the skill directory name or lesson id being cited about.
- ``source``: which measured artifact the count came from (e.g.
  ``"skill_fitness.census"`` or ``"lesson_v2.lesson_zero_citation_census"``).
- ``retrieval_count``: an integer count of confirmed reads (skill) or
  citations (lesson) in the observed window, OR ``None`` with
  ``no_evidence_reason`` set.
- ``no_evidence_reason``: required non-empty string when
  ``retrieval_count`` is ``None`` — mirrors ``benchmark_evidence``'s
  ``alternative: "none available"`` + ``alternative_reason`` pair. Absence
  of evidence must be stated explicitly, never defaulted silently.
- ``offered_or_shown``: required truthy when ``retrieval_count == 0`` —
  #1672's own finding, preserved here as a hard validation rule: "never
  offered is not evidence of not useful", only "offered and ignored" is. A
  zero count with no denominator is not valid citation shape; it is
  missing data mislabeled as a confirmed zero.

Everything here is deterministic (NO LLM call) and never raises;
:func:`validate_trainer_citation` always returns a list (empty means
valid), exactly like :func:`benchmark_evidence.validate_benchmark`.
"""
from __future__ import annotations

from typing import Any

_VALID_KINDS = ("skill", "lesson")


def validate_trainer_citation(obj: Any) -> list[str]:
    """Schema-validate a trainer evidence citation; return a list of
    violations (empty list == valid). See the module docstring for the
    required shape. Never raises — an unexpected input shape (e.g. ``obj``
    not even a dict) produces a violation list rather than an exception.
    """
    violations: list[str] = []
    try:
        if not isinstance(obj, dict):
            return ["citation is not a JSON object"]

        kind = obj.get("kind")
        if kind not in _VALID_KINDS:
            violations.append(f"kind must be one of {_VALID_KINDS}")

        target_id = obj.get("target_id")
        if not isinstance(target_id, str) or not target_id.strip():
            violations.append("target_id must be a non-empty string")

        source = obj.get("source")
        if not isinstance(source, str) or not source.strip():
            violations.append(
                "source must name the measured artifact this count came from"
            )

        retrieval_count = obj.get("retrieval_count")
        no_evidence_reason = obj.get("no_evidence_reason")

        if retrieval_count is None:
            if not isinstance(no_evidence_reason, str) or not no_evidence_reason.strip():
                violations.append(
                    "retrieval_count is None but no_evidence_reason is missing -- "
                    "absence of evidence must be stated explicitly, never defaulted"
                )
        elif isinstance(retrieval_count, bool) or not isinstance(retrieval_count, int):
            violations.append("retrieval_count must be an integer or None")
        elif retrieval_count < 0:
            violations.append("retrieval_count must not be negative")
        elif retrieval_count == 0 and not obj.get("offered_or_shown"):
            # #1672: a zero count needs a denominator. "Never offered" is
            # not evidence of "not useful" -- only "offered and ignored" is.
            violations.append(
                "retrieval_count is 0 with no offered_or_shown evidence -- "
                "never offered is not evidence of not useful (#1672)"
            )

        return violations
    except Exception:
        return ["trainer citation validation raised unexpectedly"]
