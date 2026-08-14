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

PROVENANCE WARNING (#813) / #819 UPDATE: a benchmark artifact under
``state/benchmarks/`` is a plain JSON file. ``state/`` is off the instance's
COMMIT surface, but it IS writable at runtime by an instance-authored script
(the same class of live reward-hack #789 already had to repair: a script
hardcoding a sidecar path and writing a falsified field into it).
:func:`validate_benchmark` checks *shape*, not *authorship* — it cannot tell
a harness-executed measurement from a forged one, and the artifact's own
``baseline``/``new_value`` numbers are, on their own, worthless as proof.

#819 closes that gap WITHOUT re-execution or a signing scheme: instead of
trusting the artifact's numbers, :func:`verify_benchmark` corroborates the
claimed improvement against the harness's OWN scorecard history
(``state/scorecard/history.jsonl`` — a ``FITNESS_SIDECARS``-protected sidecar
the scorecard module itself writes every recompute). Only a small allowlist
of harness-observed V1 metrics (:data:`_HARNESS_METRICS`) is even eligible;
for one of those, the artifact is a mere pointer ("this cycle claims metric
X improved") and the harness's before/after snapshots around the
integration timestamp decide whether that is true. An instance can still
write a fully-shaped, internally-consistent forged artifact, but it buys
nothing: the harness history either shows the named metric improved or it
doesn't, and the instance cannot rewrite its own scorecard history (that
would itself trip the #789 spawn-boundary tamper detector). This makes it
SAFE for an operator to enable the ``SELFEVO_BENCHMARK_TRUST`` switch — the
switch still defaults OFF (enabling it remains an explicit operator step,
not something #819 flips on its own), and with it off the entire affirmative
path stays dormant exactly as before.

Everything here is deterministic (NO LLM call) and fail-closed: an
unreadable/missing file, a disabled trust switch, an unregistered/mismatched
metric, missing or uncorroborating history, or an unexpected input shape
all degrade to "not verified", never to a false pass. The schema check
itself (:func:`validate_benchmark`) never raises; it always returns a list
(empty means valid).
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BENCHMARK_SCHEMA = "benchmark-evidence-v1"

_REQUIRED_STR_FIELDS = ("metric", "method")
_REQUIRED_NUM_FIELDS = ("baseline", "new_value")
_VALID_DIRECTIONS = ("lower_is_better", "higher_is_better")

# #819: the ONLY metric names a benchmark artifact can ever verify against —
# each maps to (scorecard snapshot section, canonical improvement direction).
# A benchmark whose ``metric`` is not a key here can NEVER verify, no matter
# how well-formed the artifact: the instance cannot invent a new metric name
# and have it trusted. Values are read straight off a scorecard history
# snapshot as ``snapshot[section][metric]`` (scorecard.py's
# ``_metric_value`` does the same lookup, scorecard.py:774-778). Where each
# is actually computed: ``tokens_per_integration`` — scorecard.py:482-523
# (``_cost_section``); ``compile_clean_ratio`` — scorecard.py:553-606
# (``_quality_section``); ``repeat_failure_rate`` — scorecard.py:361-476
# (``_loop_section``); ``heldout_gap`` — scorecard.py:710-744
# (``_heldout_section``). Directions mirror scorecard._TARGETS
# (scorecard.py:116-201) for the same metrics.
_HARNESS_METRICS: dict[str, tuple[str, str]] = {
    "tokens_per_integration": ("cost", "lower_is_better"),
    "compile_clean_ratio": ("quality", "higher_is_better"),
    "repeat_failure_rate": ("loop", "lower_is_better"),
    "heldout_gap": ("heldout", "lower_is_better"),
}

# Corroboration tolerance: the harness-observed before/after must differ by
# more than this — relative to the "before" magnitude, floored by a small
# absolute epsilon for near-zero values — in the claimed-improving direction.
# Guards against float noise/rounding being read as a genuine improvement.
_IMPROVEMENT_REL_TOL = 0.01  # 1%
_IMPROVEMENT_ABS_TOL = 1e-6

# Bounded read of the harness's own scorecard history (larger than
# scorecard.py's own 400-line trend window, #814/#767's _MAX_HISTORY_LINES,
# because an optimization claim may need to look back further than 7 days
# to find its pre-integration snapshot).
_MAX_HISTORY_LINES = 3000

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
    regardless of trust/validity/verification. Used by the
    ``confirm_serves`` gate (#813, extended #819) to choose between the
    ``benchmark_missing``, ``benchmark_untrusted``, and ``benchmark_unverified``
    reason codes. Rejects unsafe ``cycle_id`` values the same way
    :func:`has_valid_benchmark` does (MED-2)."""
    try:
        safe_id = _is_safe_cycle_id(cycle_id)
        if not safe_id:
            return False
        return benchmark_path(state_dir, safe_id).is_file()
    except Exception:
        return False


def _parse_ts(value: Any) -> "datetime | None":
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _history_snapshots(state_dir: Path) -> list[dict[str, Any]]:
    """Bounded read of the harness's own scorecard history
    (``<state_dir>/scorecard/history.jsonl``) — the TRUST ROOT
    :func:`verify_benchmark` corroborates a claimed improvement against,
    never the artifact's own numbers. Written exclusively by
    ``scorecard.compute_scorecard`` (one JSON line per recompute); each line
    is a full snapshot dict with a top-level ``computed_at_utc`` and
    per-section metric values (``snapshot[section][metric]``). Bounded to
    the newest :data:`_MAX_HISTORY_LINES` lines. Fail-open to ``[]`` on any
    error — a missing/unreadable file or corrupt individual lines (skipped,
    not fatal) never raise into the caller."""
    out: list[dict[str, Any]] = []
    try:
        path = Path(state_dir) / "scorecard" / "history.jsonl"
        if not path.is_file():
            return out
        lines = path.read_text(encoding="utf-8").splitlines()[-_MAX_HISTORY_LINES:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out
    except Exception:
        return out


def _snapshot_metric(snapshot: dict[str, Any], section: str, metric: str) -> float | None:
    try:
        sec = snapshot.get(section)
        if not isinstance(sec, dict):
            return None
        value = sec.get(metric)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value):
            return None
        return float(value)
    except Exception:
        return None


def _harness_improved(before: float, after: float, direction: str) -> bool:
    """True iff ``after`` improved on ``before`` in ``direction``, beyond a
    small tolerance (:data:`_IMPROVEMENT_REL_TOL` / :data:`_IMPROVEMENT_ABS_TOL`)
    — guards against float noise/rounding reading as a genuine improvement."""
    tolerance = max(_IMPROVEMENT_ABS_TOL, abs(before) * _IMPROVEMENT_REL_TOL)
    if direction == "lower_is_better":
        return (before - after) > tolerance
    if direction == "higher_is_better":
        return (after - before) > tolerance
    return False


def verify_benchmark(state_dir: Path, cycle_id: str, integration_ts: Any) -> bool:
    """The #819 forge-proof verification: True ONLY IF, in order —

    1. the operator trust switch is on (:func:`benchmark_trust_enabled`);
    2. a safe ``cycle_id`` names an existing artifact that passes
       :func:`validate_benchmark` (shape + claimed-improvement check);
    3. the artifact's ``metric`` is in the harness-verifiable allowlist
       (:data:`_HARNESS_METRICS`) — an unregistered metric name can NEVER
       verify, no matter how well-formed the artifact;
    4. the artifact's ``direction`` matches that metric's CANONICAL
       direction (a lie about direction is rejected outright, even if it
       happens to match the artifact's own baseline/new_value ordering);
    5. the harness's OWN scorecard history
       (:func:`_history_snapshots`, ``state/scorecard/history.jsonl``) shows
       the SAME metric actually moved in the canonical improvement
       direction, beyond a small tolerance, between the snapshot at-or-before
       ``integration_ts`` (the "before") and the LATEST snapshot in history
       (the "after") — and that "after" snapshot must itself be newer than
       ``integration_ts`` (otherwise nothing has been observed since the
       claimed change landed yet).

    The artifact's own ``baseline``/``new_value`` numbers are NEVER read
    here beyond :func:`validate_benchmark`'s shape check — they are not part
    of the pass/fail decision. This is the non-forgeability invariant: an
    instance can write a fully-shaped, internally-consistent benchmark
    artifact with fabricated numbers, or write
    ``confirmed=true, signal="benchmark"`` directly into
    ``demand/completed.json``, and it buys nothing — this function (and the
    ``usage_evidence.confirm_serves`` Pass 2 caller, which re-runs it on
    EVERY optimization-claim entry regardless of stored state) always
    re-derives the verdict from the harness's own history.

    Fail-closed on every axis: an unsafe/empty ``cycle_id``, the trust
    switch being off, a missing/unreadable/corrupt/schema-invalid artifact,
    an unregistered or direction-mismatched metric, an unparseable
    ``integration_ts``, missing/unreadable history, no snapshot at-or-before
    ``integration_ts``, no snapshot after it yet, or no corroborated
    improvement all read as ``False``. Never raises.
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
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if validate_benchmark(artifact):
            return False  # shape/claimed-improvement check failed

        metric = str(artifact.get("metric") or "").strip()
        spec = _HARNESS_METRICS.get(metric)
        if spec is None:
            return False  # unregistered metric — can never verify
        section, canonical_direction = spec

        direction = str(artifact.get("direction") or "").strip()
        if direction != canonical_direction:
            return False  # lied about (or mismatched) direction

        ts = _parse_ts(integration_ts)
        if ts is None:
            return False

        history = _history_snapshots(state_dir)
        if not history:
            return False

        before = before_ts = after = after_ts = None
        for snapshot in history:
            if not isinstance(snapshot, dict):
                continue
            snap_ts = _parse_ts(snapshot.get("computed_at_utc"))
            if snap_ts is None:
                continue
            value = _snapshot_metric(snapshot, section, metric)
            if value is None:
                continue
            if snap_ts <= ts and (before_ts is None or snap_ts > before_ts):
                before, before_ts = value, snap_ts
            if after_ts is None or snap_ts > after_ts:
                after, after_ts = value, snap_ts

        if before is None or after is None:
            return False  # no corroborating before and/or after snapshot
        if after_ts <= ts:
            return False  # nothing observed since the claimed change landed

        return _harness_improved(before, after, canonical_direction)
    except Exception:
        return False


def has_valid_benchmark(state_dir: Path, cycle_id: str, integration_ts: Any = None) -> bool:
    """#819: now delegates entirely to :func:`verify_benchmark` — the
    shape+trust-switch check alone (pre-#819 behavior) is no longer
    sufficient; the harness's own scorecard history must also corroborate
    the claimed improvement around ``integration_ts``. Kept as the stable
    public gate name for callers (``usage_evidence.confirm_serves``); see
    :func:`verify_benchmark` for the full contract and fail-closed axes.
    ``integration_ts`` defaults to ``None`` for source compatibility, but a
    ``None``/unparseable value always fails closed (there is no ts to
    corroborate a "before" against).
    """
    return verify_benchmark(state_dir, cycle_id, integration_ts)
