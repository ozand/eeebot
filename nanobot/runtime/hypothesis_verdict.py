"""Harness-computed hypothesis verdict (#878 — closing the RSI scientific loop).

``hypothesis_backlog.reconcile`` already answers WHETHER a hypothesis's
serving cycle integrated (``status: "answered"``). It never asked whether the
hypothesis turned out to be TRUE — a hypothesis experiment that "succeeded"
(the cycle integrated) and one that later proves worthless look identical in
``lifecycle.json`` today. This module adds the missing half: a VERDICT
(``supported`` / ``refuted`` / ``inconclusive``) derived exclusively from
MEASURED, harness-trusted sidecars — never from anything the instance wrote
as prose or a claimed number.

Trust boundary (read before touching this module): both inputs this module
reads are members of ``scorecard.FITNESS_SIDECARS`` (#789 spawn-boundary
tamper detection) — ``heldout/microbench.json`` (#822, a causal, isolated
before/after measurement the harness itself runs in a sandboxed subprocess)
and ``demand/completed.json`` (#773/#761, whose ``confirmed`` field is set
ONLY by ``usage_evidence.confirm_serves`` from an independently-observed
usage signal — never from a claim). Neither ``hypotheses/lifecycle.json``
nor ``hypotheses/backlog.json``/``research/hypotheses.json`` (the INSTANCE-
writable state this whole feature is built on top of) is ever read for the
number itself — a hypothesis entry's own ``evidence``/``acceptance`` text is
informational only (``acceptance_text`` below is accepted purely for future
context enrichment and never influences the classification).

A verdict here is STEERING, never a verification gate (see the module docs
of ``benchmark_evidence``/``heldout.microbench`` for the equivalent
distinction on the gate side): a ``refuted`` verdict only blocks a future
re-proposal of the same title (``llm_proposer._refuted_hypothesis_titles`,
permanent, like #834's built-subject novelty guard); a ``supported`` verdict
only injects a citable priority-review candidate
(``hypothesis_backlog.supported_hypotheses`` -> ``goal_review``'s evidence
input), which still has to pass ``goal_review.validate_priority`` and the
full cycle gate before it can ever integrate. A forged/tampered sidecar
therefore costs at worst a wasted retry or a rejected priority candidate —
it can never fabricate an integration by itself.

Sources tried, in order (first match wins):

1. **microbench** (``heldout.microbench.load_microbench_entry``) — a causal,
   isolated single-cycle measurement. ``improvement_pct >= 5.0`` ->
   ``supported``; otherwise -> ``refuted``. This source, when present, is
   authoritative and short-circuits the other two.
2. **confirmed usage** (``demand/completed.json``) — the cycle's own
   completed entry (matched by ``cycle_id``) touched a ``scripts/`` artifact:
   ``confirmed is True`` -> ``supported``; ``confirmed`` still false/absent
   after :data:`CONFIRM_WINDOW_DAYS` days since completion -> ``refuted``
   (it had a fair window to be used and was not); still within the window ->
   falls through to ``inconclusive`` (too early to call).
3. **none** — no measured signal at all -> ``inconclusive``.

Crude and simple, deliberately: this is a steering signal, not a scored
metric, symmetric with the #822/#819 "only these allowlisted sidecars are
trusted" design. Fail-open throughout: any error, missing file, or
malformed entry degrades to ``("inconclusive", {"source": "none", ...})``,
never raises.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# #822 microbench: the minimum measured improvement (percent, lower baseline
# time minus candidate time over baseline) to call a hypothesis "supported"
# rather than "refuted". Crude, fixed threshold — this is a steering signal,
# not a promotion gate.
MICROBENCH_SUPPORTED_THRESHOLD_PCT = 5.0

# #773/#761 confirmed-usage: how long a completed entry gets to accumulate a
# harness usage signal before "still unconfirmed" is read as "refuted"
# rather than "too early to tell". Mirrors the existing decay-window
# convention (``demand._DECAY_DAYS`` / ``scorecard._DECAY_DAYS`` /
# ``goal_review._DECAY_DAYS`` are all 14) so this module introduces no new
# staleness constant, just reuses the established number.
CONFIRM_WINDOW_DAYS = 14

VERDICTS = ("supported", "refuted", "inconclusive")


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _parse_ts(value: Any) -> "datetime | None":
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _microbench_verdict(state_dir: Path, cycle_id: str) -> "tuple[str, dict[str, Any]] | None":
    """Try the microbench source; ``None`` if no well-formed entry exists for
    ``cycle_id`` (caller falls through to the next source)."""
    try:
        from nanobot.runtime.heldout.microbench import load_microbench_entry

        entry = load_microbench_entry(state_dir, cycle_id)
    except Exception:
        entry = None
    if not isinstance(entry, dict):
        return None
    improvement = entry.get("improvement_pct")
    if not _is_finite_number(improvement):
        return None
    verdict = "supported" if improvement >= MICROBENCH_SUPPORTED_THRESHOLD_PCT else "refuted"
    return verdict, {
        "source": "microbench",
        "cycle_id": str(cycle_id),
        "metric": "improvement_pct",
        "value": improvement,
        "threshold": MICROBENCH_SUPPORTED_THRESHOLD_PCT,
    }


def _confirmed_usage_verdict(
    state_dir: Path, cycle_id: str, *, now: "datetime | None" = None
) -> "tuple[str, dict[str, Any]] | None":
    """Try the confirmed-usage source over ``demand/completed.json``
    entries matching ``cycle_id``. ``None`` if the cycle built no
    ``scripts/`` artifact at all (caller falls through to "inconclusive")."""
    try:
        now = now or datetime.now(timezone.utc)
        completed = _read_json(Path(state_dir) / "demand" / "completed.json", None)
        entries = completed.get("entries") if isinstance(completed, dict) else None
        if not isinstance(entries, dict):
            return None
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("cycle_id") or "") != str(cycle_id):
                continue
            files = entry.get("files_changed")
            if not isinstance(files, list):
                continue
            script_files = [str(f).strip() for f in files if str(f or "").strip().startswith("scripts/")]
            if not script_files:
                continue
            if entry.get("confirmed") is True:
                return "supported", {
                    "source": "confirmed_usage",
                    "cycle_id": str(cycle_id),
                    "artifact": script_files[0],
                    "signal": str(entry.get("signal") or ""),
                }
            completed_ts = _parse_ts(entry.get("ts"))
            if completed_ts is not None:
                age_days = (now - completed_ts).total_seconds() / 86400.0
                if age_days >= CONFIRM_WINDOW_DAYS:
                    return "refuted", {
                        "source": "confirmed_usage",
                        "cycle_id": str(cycle_id),
                        "artifact": script_files[0],
                        "age_days": round(age_days, 1),
                        "window_days": CONFIRM_WINDOW_DAYS,
                    }
            # A scripts/ artifact exists but is still within its confirm
            # window (or has no parseable ts) — too early to call either
            # way; fall through to the caller's "inconclusive" default.
            return None
        return None
    except Exception:
        return None


def classify_hypothesis_verdict(
    state_dir: Path, cycle_id: str, acceptance_text: str = "", *, now: "datetime | None" = None
) -> "tuple[str, dict[str, Any]]":
    """Return ``(verdict, evidence)`` for the cycle that answered a
    hypothesis, ``verdict`` in :data:`VERDICTS`.

    ``acceptance_text`` is accepted for interface symmetry with the
    hypothesis's own recorded acceptance criteria (context for a future
    caller/log line) but is NEVER read for the classification itself — see
    the module docstring's trust-boundary note. ``now`` is injectable for
    tests; defaults to the current UTC time.

    Fail-open: any error, or no measured signal at all, degrades to
    ``("inconclusive", {"source": "none", ...})`` — never raises.
    """
    # ``acceptance_text`` is intentionally never inspected below — see the
    # module docstring's trust-boundary note.
    state_dir = Path(state_dir)
    safe_cycle_id = str(cycle_id or "").strip()
    if not safe_cycle_id:
        return "inconclusive", {"source": "none", "cycle_id": ""}
    try:
        result = _microbench_verdict(state_dir, safe_cycle_id)
        if result is not None:
            return result
        result = _confirmed_usage_verdict(state_dir, safe_cycle_id, now=now)
        if result is not None:
            return result
        return "inconclusive", {"source": "none", "cycle_id": safe_cycle_id}
    except Exception:
        return "inconclusive", {"source": "none", "cycle_id": safe_cycle_id}
