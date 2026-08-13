"""Benchmark-evidence gate — optimization claims require a before/after
measurement (#813).

The #761 usage-evidence layer proved a *behavior-preserving* change was
exercised post-integration; it has no opinion on whether an *optimization*
claim ("this is faster/cheaper/smaller") is actually true. A claim like that
needs a measurement, not a harness-observed touch — the instance could
otherwise declare `serves: "optimization latency"` on any ordinary change and
ride the existing harness-signal confirmation path to a false "verified
faster" credit.

This module adds the missing measurement gate, enforced at CONFIRMATION time
(the simplest integration point — no bridge/gate change needed):

- :func:`is_optimization_claim` recognizes the EXPLICIT, structured signal —
  a ``serves`` value normalized and checked with ``startswith("optimization")``
  — never inferred from free-text rationale or summary prose. Steering the
  proposer to actually emit this form is #815; this module only makes the
  signal valid (``llm_proposer._SERVES_PREFIXES``) and enforced.
- :func:`validate_benchmark` schema-checks a benchmark artifact: ``metric``
  and ``method`` must be non-empty strings, ``baseline`` and ``new_value``
  must be numbers. No trust of prose — every field is type-checked, nothing
  is inferred from surrounding text.
- :func:`has_valid_benchmark` is the fail-closed existence+validity check
  ``usage_evidence.confirm_serves`` calls before ever confirming an
  optimization-claim entry: a missing, corrupt, or schema-invalid artifact
  means NO valid benchmark, full stop.

Artifacts live at ``<state_dir>/benchmarks/<cycle_id>.json`` — a new
sidecar this module is the sole writer/reader of; the runtime itself never
writes into ``state/benchmarks/`` today (the scorecard only reads it), so
this module owns exactly one new file per benchmarked cycle and nothing
else.

Everything here is deterministic (NO LLM call) and fail-closed on the
existence check (:func:`has_valid_benchmark`) — an unreadable/missing file
degrades to "no valid benchmark", never to a false pass. The schema check
itself (:func:`validate_benchmark`) never raises; it always returns a list
(empty means valid).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BENCHMARK_SCHEMA = "benchmark-evidence-v1"

_REQUIRED_STR_FIELDS = ("metric", "method")
_REQUIRED_NUM_FIELDS = ("baseline", "new_value")


def is_optimization_claim(serves: Any) -> bool:
    """True iff ``serves`` declares an optimization claim — the explicit,
    structured signal (#813): normalized (stripped, lower-cased) and checked
    with ``startswith("optimization")``. Never inferred from rationale or
    summary text. Fail-open to ``False`` on any unexpected input shape."""
    try:
        return str(serves or "").strip().lower().startswith("optimization")
    except Exception:
        return False


def validate_benchmark(obj: Any) -> list[str]:
    """Schema-validate a benchmark artifact; return a list of violations
    (empty list == valid). Required fields, no trust of prose:

    - ``metric``: non-empty string (what was measured)
    - ``baseline``: number (the before value)
    - ``new_value``: number (the after value)
    - ``method``: non-empty string (how it was measured)

    Never raises — an unexpected shape (e.g. ``obj`` not even a dict)
    produces a violation list rather than an exception.
    """
    violations: list[str] = []
    try:
        if not isinstance(obj, dict):
            return ["benchmark is not a JSON object"]
        for field in _REQUIRED_STR_FIELDS:
            value = obj.get(field)
            if not isinstance(value, str) or not value.strip():
                violations.append(f"{field} must be a non-empty string")
        for field in _REQUIRED_NUM_FIELDS:
            value = obj.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                violations.append(f"{field} must be a number")
        return violations
    except Exception:
        return ["benchmark validation raised unexpectedly"]


def benchmark_path(state_dir: Path, cycle_id: str) -> Path:
    """Path of the benchmark artifact for ``cycle_id``:
    ``<state_dir>/benchmarks/<cycle_id>.json``."""
    return Path(state_dir) / "benchmarks" / f"{cycle_id}.json"


def has_valid_benchmark(state_dir: Path, cycle_id: str) -> bool:
    """True iff a schema-valid benchmark artifact exists for ``cycle_id``.

    Fail-closed: an empty ``cycle_id``, a missing file, an unreadable/corrupt
    file, or a file that fails :func:`validate_benchmark` all read as
    ``False`` — an optimization claim gets no benefit of the doubt.
    """
    try:
        cycle_id = str(cycle_id or "").strip()
        if not cycle_id:
            return False
        path = benchmark_path(state_dir, cycle_id)
        if not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return not validate_benchmark(data)
    except Exception:
        return False
