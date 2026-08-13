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
  and ``method`` must be non-empty strings; ``baseline``/``new_value`` must
  be finite numbers (not bool, not NaN/inf); ``direction`` must be
  ``"lower_is_better"`` or ``"higher_is_better"``; and the measurement must
  show an ACTUAL improvement in the declared direction — ``new_value`` equal
  to ``baseline`` (no change) or a regression against ``direction`` is
  rejected. No trust of prose — every field is type/value-checked, nothing
  is inferred from surrounding text.
- :func:`benchmark_file_exists` / :func:`has_valid_benchmark` are the
  fail-closed existence/validity checks ``usage_evidence.confirm_serves``
  uses before ever confirming (or continuing to trust a prior confirmation
  of) an optimization-claim entry.

Artifacts live at ``<state_dir>/benchmarks/<cycle_id>.json`` — a new
sidecar this module is the sole reader of; the runtime itself never writes
into ``state/benchmarks/`` today (the scorecard only reads it). ``cycle_id``
is treated as untrusted input when building that path: a value containing a
path separator (``/`` or ``\\``) or ``..`` is rejected before ever joining,
so a crafted id can't be used to read an arbitrary on-disk JSON file
(:func:`benchmark_path` still builds the naive path for callers that already
trust their ``cycle_id``; :func:`has_valid_benchmark` and
:func:`benchmark_file_exists` do the rejection themselves before touching
disk).

PROVENANCE WARNING (#813, tracked for hardening in #819): a benchmark
artifact under ``state/benchmarks/`` is a plain JSON file. ``state/`` is off
the instance's COMMIT surface, but it IS writable at runtime by an
instance-authored script (the same class of live reward-hack #789 already
had to repair: a script hardcoding a sidecar path and writing a falsified
field into it). :func:`validate_benchmark` checks *shape*, not *authorship*
— it cannot tell a harness-executed measurement from a forged one. Until
#819 lands real harness-verified provenance (re-execution or a signed/
harness-authored artifact), this module ships FAIL-CLOSED: the
``SELFEVO_BENCHMARK_TRUST`` env switch (default OFF) gates the entire
affirmative path. With it off (the required posture pre-#819), any
optimization claim is permanently unconfirmable, and a forged artifact is
inert — a fully-shaped forged benchmark buys nothing. Do not flip this
switch on until #819's provenance guarantee exists.

Everything here is deterministic (NO LLM call) and fail-closed: an
unreadable/missing file, a disabled trust switch, or an unexpected input
shape all degrade to "no valid benchmark", never to a false pass. The
schema check itself (:func:`validate_benchmark`) never raises; it always
returns a list (empty means valid).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

BENCHMARK_SCHEMA = "benchmark-evidence-v1"

_REQUIRED_STR_FIELDS = ("metric", "method")
_REQUIRED_NUM_FIELDS = ("baseline", "new_value")
_VALID_DIRECTIONS = ("lower_is_better", "higher_is_better")

# #813 HIGH-2: operator-owned trust switch, same SELFEVO_ / default-OFF
# precedent as SELFEVO_DECAY_PROTECT (#809) and SELFEVO_RUNTIME_SLICE
# (#812) — read from the environment, not a file in the instance repo,
# so the instance cannot flip its own trust on. MUST stay OFF until #819
# (harness-verified benchmark provenance) lands; see the module docstring.
TRUST_ENV = "SELFEVO_BENCHMARK_TRUST"
_TRUTHY = {"1", "true", "yes", "on"}


def benchmark_trust_enabled() -> bool:
    """True iff the operator has explicitly opted into trusting on-disk
    benchmark artifacts (:data:`TRUST_ENV`, default OFF). Fail-closed on any
    unexpected environment error."""
    try:
        return os.environ.get(TRUST_ENV, "0").strip().lower() in _TRUTHY
    except Exception:
        return False


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
    - ``baseline``: finite number (the before value)
    - ``new_value``: finite number (the after value)
    - ``method``: non-empty string (how it was measured)
    - ``direction``: ``"lower_is_better"`` or ``"higher_is_better"``

    Beyond shape, the measurement itself must show a genuine improvement:
    ``new_value == baseline`` (no change) is rejected, and a regression
    against the declared ``direction`` is rejected — a benchmark proving
    "slower/equal" is not a valid optimization claim.

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

        direction_raw = obj.get("direction")
        direction: str | None = None
        if not isinstance(direction_raw, str) or direction_raw.strip() not in _VALID_DIRECTIONS:
            violations.append(f"direction must be one of {_VALID_DIRECTIONS}")
        else:
            direction = direction_raw.strip()

        numeric: dict[str, float] = {}
        for field in _REQUIRED_NUM_FIELDS:
            value = obj.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                violations.append(f"{field} must be a number")
                continue
            if not math.isfinite(value):
                violations.append(f"{field} must be a finite number (not NaN/inf)")
                continue
            numeric[field] = value

        if "baseline" in numeric and "new_value" in numeric:
            baseline = numeric["baseline"]
            new_value = numeric["new_value"]
            if new_value == baseline:
                violations.append(
                    "new_value must differ from baseline — a no-change "
                    "measurement is not an optimization"
                )
            elif direction == "lower_is_better" and not (new_value < baseline):
                violations.append(
                    "new_value must be lower than baseline for "
                    "direction=lower_is_better (regression, not an improvement)"
                )
            elif direction == "higher_is_better" and not (new_value > baseline):
                violations.append(
                    "new_value must be higher than baseline for "
                    "direction=higher_is_better (regression, not an improvement)"
                )
        return violations
    except Exception:
        return ["benchmark validation raised unexpectedly"]


def _is_safe_cycle_id(cycle_id: Any) -> str:
    """Normalize ``cycle_id`` and return it iff safe to join into a path;
    ``""`` otherwise. #813 MED-2: a ``cycle_id`` containing a path separator
    (``/`` or ``\\``) or ``..`` is rejected BEFORE ever being joined — a
    crafted id must never let a caller escape ``<state_dir>/benchmarks/``."""
    try:
        c = str(cycle_id or "").strip()
        if not c or "/" in c or "\\" in c or ".." in c:
            return ""
        return c
    except Exception:
        return ""


def benchmark_path(state_dir: Path, cycle_id: str) -> Path:
    """Path of the benchmark artifact for ``cycle_id``:
    ``<state_dir>/benchmarks/<cycle_id>.json``. Callers that have not
    already validated ``cycle_id`` should prefer :func:`benchmark_file_exists`
    / :func:`has_valid_benchmark`, which reject unsafe ids themselves."""
    return Path(state_dir) / "benchmarks" / f"{cycle_id}.json"


def benchmark_file_exists(state_dir: Path, cycle_id: str) -> bool:
    """True iff a benchmark artifact file exists for ``cycle_id`` —
    regardless of trust/validity. Used by the ``confirm_serves`` gate
    (#813) to choose between the ``benchmark_missing`` and
    ``benchmark_untrusted`` reason codes. Rejects unsafe ``cycle_id`` values
    the same way :func:`has_valid_benchmark` does (MED-2)."""
    try:
        safe_id = _is_safe_cycle_id(cycle_id)
        if not safe_id:
            return False
        return benchmark_path(state_dir, safe_id).is_file()
    except Exception:
        return False


def has_valid_benchmark(state_dir: Path, cycle_id: str) -> bool:
    """True ONLY IF: the operator trust switch (:func:`benchmark_trust_enabled`)
    is ON, AND a benchmark artifact file exists for ``cycle_id``, AND
    :func:`validate_benchmark` finds it clean.

    Fail-closed on every axis: an unsafe/empty ``cycle_id``, the trust
    switch being off (the default — see the module docstring's PROVENANCE
    WARNING), a missing file, an unreadable/corrupt file, or a file that
    fails schema/measurement validation all read as ``False``. An
    optimization claim gets no benefit of the doubt.
    """
    try:
        safe_id = _is_safe_cycle_id(cycle_id)
        if not safe_id:
            return False
        if not benchmark_trust_enabled():
            return False
        path = benchmark_path(state_dir, safe_id)
        if not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return not validate_benchmark(data)
    except Exception:
        return False
