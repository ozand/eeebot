"""Post-integration value verification — usage evidence + confirmed serves (#761).

The `serves` claim (#751) and the demand-id ledger chain (#760/#773) prove a
change was *made*; nothing verified it created *value*. This module is the
consumption-evidence layer: for every ``scripts/*.py`` artifact in the
instance repo it records when the artifact was last *exercised*, derived
ONLY from harness-observed signals — never from the proposal's or the
subagent's own statements. That constraint is the point (AIDE²
anti-reward-hacking scope addition on #761: self-reported improvement claims
were 63% false; independent re-measurement is the only trustworthy source),
so the three signals are all things the harness can observe on disk itself:

- ``used:pycache`` — a ``__pycache__/<stem>.cpython-*.pyc`` next to the
  script (the interpreter imported/executed it); the ``.pyc`` mtime is the
  usage timestamp.
- ``used:output`` — the script's own header (FIRST 50 lines only — bounded
  extraction) names an exact ``state/...`` or ``docs/...`` output path that
  exists; that output file's mtime is the usage timestamp. ``state/...``
  resolves against ``state_dir``, ``docs/...`` against the instance repo.
- ``touched:result`` — the script's repo-relative path appears in a recent
  subagent RESULT file's ``files_changed`` list (it was *modified*, which is
  tracked separately from *used* — editing a script is not evidence anyone
  consumes it); the result file's mtime is the touch timestamp.

systemd/cron execution traces are NOT reachable from the state dir — they
are deliberately skipped, never faked.

Evidence persists in the sidecar ``<state_dir>/usage/last_used.json``
(schema ``usage-evidence-v1``), entries keyed by repo-relative path:
``{last_used, last_touched, signal}``. Merges keep the max across runs
(append-only semantics like ``demand``'s completed sidecar (#773) — a newer
timestamp is never regressed to an older one). Full rescans are gated by a
HEAD+time watermark exactly like ``system_map.update_system_map`` (#749):
re-scan only when the repo HEAD moved OR more than
:data:`_RESCAN_HOURS` hours passed since the last scan, so idle cycles stay
cheap.

:func:`confirm_serves` is the declared→confirmed tie-back: a
``demand/completed.json`` entry (#773) whose ``files_changed`` includes a
``scripts/`` artifact gains ``"confirmed": true`` (+ ``confirmed_at``,
``signal``) iff usage evidence shows a ``last_used`` NEWER than the
completion ``ts``. Additive only — nothing is ever removed from the
completed sidecar, and no text/claim field (summaries, rationales,
subagent-reported statuses) can ever confirm an entry.

Everything here is deterministic (NO LLM call) and fail-open: a
missing/corrupt file, an unreadable directory, or any unexpected exception
degrades to "no evidence" / "no confirmation" — never raises into the
caller (a usage bug must never block demand collection).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime import benchmark_evidence

USAGE_SCHEMA = "usage-evidence-v1"

# #789: the ONLY signal values this module itself ever writes into a
# confirmed completed-demand entry (see refresh_usage's used_candidates and
# confirm_serves' `entry["signal"] = usage_entry["signal"]` copy). Harness
# readers (scorecard._value_section, the RSI streak in
# scripts/loop_metrics_report.py) trust `confirmed` ONLY when the recorded
# signal is in this set — any other value means non-harness code wrote the
# fitness input (live reward-hack 2026-07-17: instance script wrote
# `signal: "operator-confirmed"` into demand/completed.json) and the entry
# is repaired on the next confirm_serves pass.
#
# #819: "benchmark" joins the set — the signal confirm_serves' Pass 2 writes
# when an optimization claim is VERIFIED against harness scorecard history
# (benchmark_evidence.verify_benchmark). This is safe to trust exactly like
# pycache/output ONLY because Pass 2 re-verifies every optimization-claim
# entry from harness history on every call, regardless of the entry's stored
# `confirmed`/`signal` — a forged `signal: "benchmark"` entry gets no benefit
# of the doubt; it is re-derived and revoked the same way a foreign signal
# is (Pass 1), just via a different check (Pass 2).
HARNESS_SIGNALS: frozenset[str] = frozenset({"pycache", "output", "benchmark"})

_RESCAN_HOURS = 6
_HEADER_LINES = 50  # bounded output-path extraction window
_MAX_RESULT_FILES = 50  # same bounded-read discipline as demand._MAX_RESULT_FILES

# #800: decay-eligibility epoch — the #761 usage-evidence deployment date.
# Scripts created BEFORE this date can be legitimately stale without ever
# having harness evidence (the evidence system did not exist to observe
# them); a script created AFTER it that has NEVER been used is the
# instance's own never-exercised artifact and must not surface as decay
# (the create→archive reward-farming vector the #800 audit found).
_EVIDENCE_EPOCH = datetime(2026, 7, 16, tzinfo=timezone.utc)

_ARCHIVE_MARKER_LINES = 5  # bounded archived-stub check window (#800)
_ARCHIVE_MARKERS = ("DEPRECATED", "ARCHIVED")

# #809: operator decay protect-list. The decay lane only sees harness-
# observable disk signals (pycache/output) — it cannot see systemd/cron
# execution (see module docstring), so a live-service script with no
# harness-visible invocation (e.g. scripts/eeebot_dashboard.py, run by
# eeebot-dashboard.service) can wrongly surface as decay-eligible. This env
# var is the escape hatch: a comma-separated list of repo-relative
# `scripts/*.py` paths the operator has pinned as protected. It is read from
# the ENVIRONMENT, not a file in the instance repo, deliberately — the
# instance repo is self-mutable and could un-protect its own entry to farm
# an archival credit; the env lives in /etc on the host (root-owned), out of
# the instance's reach. Empty/unset = no protection (byte-identical prior
# behavior).
_DECAY_PROTECT_ENV = "SELFEVO_DECAY_PROTECT"


def _decay_protected_paths() -> frozenset[str]:
    """Parse :data:`_DECAY_PROTECT_ENV` into a set of normalized
    repo-relative paths. Robust to whitespace, empty segments, trailing
    commas, and backslash separators (Windows-authored env values);
    fail-open on any unexpected error — a malformed env degrades to no
    protection, never a crash (#809)."""
    try:
        raw = os.environ.get(_DECAY_PROTECT_ENV, "") or ""
        paths: set[str] = set()
        for token in raw.split(","):
            cleaned = token.strip().replace("\\", "/")
            if cleaned:
                paths.add(cleaned)
        return frozenset(paths)
    except Exception:
        return frozenset()


# #800 (tightened): a last_used within this window after the script's git
# creation is the creation cycle's own self-test (the subagent executes the
# script once right after writing it → __pycache__ → "used"), not evidence
# anyone consumes it. Post-epoch scripts need a use OUTSIDE this window to
# be decay-eligible.
_BIRTH_USE_GRACE = timedelta(days=1)

# Exact `state/...` or `docs/...` path strings only — nothing looser (the
# bound exists so a script cannot "claim" usage via arbitrary prose; the
# referenced file must actually exist and its mtime is the evidence).
_OUTPUT_PATH_RE = re.compile(r"\b((?:state|docs)/[A-Za-z0-9_\-./]+)")


def _usage_path(state_dir: Path) -> Path:
    return Path(state_dir) / "usage" / "last_used.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _mtime_iso(path: Path) -> str | None:
    try:
        return _iso(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
    except Exception:
        return None


def _git_head(selfevo_repo: Path | None) -> str | None:
    if not selfevo_repo:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(selfevo_repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception:
        return None


def _load_usage(state_dir: Path) -> dict[str, Any]:
    data = _read_json(_usage_path(state_dir), None)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"schema_version": USAGE_SCHEMA, "entries": {}}
    return data


# ─── signals (harness-observable ONLY) ──────────────────────────────────────


def _pycache_signal(script: Path) -> str | None:
    """``used:pycache`` — newest matching ``__pycache__/<stem>.cpython-*.pyc``
    mtime, or ``None``. The interpreter wrote that file; the subagent cannot
    fake it with a claim."""
    try:
        cache_dir = script.parent / "__pycache__"
        if not cache_dir.is_dir():
            return None
        newest: str | None = None
        for pyc in cache_dir.glob(f"{script.stem}.cpython-*.pyc"):
            ts = _mtime_iso(pyc)
            if ts is not None and (newest is None or ts > newest):
                newest = ts
        return newest
    except Exception:
        return None


def _output_paths_from_header(script: Path) -> list[str]:
    """Exact ``state/...``/``docs/...`` path strings found in the FIRST
    :data:`_HEADER_LINES` lines of the script (docstring/argparse header) —
    the deliberate bound from #761's design."""
    try:
        lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    header = "\n".join(lines[:_HEADER_LINES])
    return [m.rstrip("./") for m in _OUTPUT_PATH_RE.findall(header)]


def _output_signal(script: Path, state_dir: Path, selfevo_repo: Path) -> str | None:
    """``used:output`` — newest mtime among existing output artifacts the
    script's header names. ``state/X`` resolves under ``state_dir``,
    ``docs/X`` under the instance repo."""
    try:
        newest: str | None = None
        for token in _output_paths_from_header(script)[:20]:
            if token.startswith("state/"):
                candidate = Path(state_dir) / token[len("state/"):]
            else:
                candidate = Path(selfevo_repo) / token
            if not candidate.is_file():
                continue
            ts = _mtime_iso(candidate)
            if ts is not None and (newest is None or ts > newest):
                newest = ts
        return newest
    except Exception:
        return None


def _touched_from_results(state_dir: Path) -> dict[str, str]:
    """``touched:result`` — repo-relative script paths mentioned in recent
    subagent RESULT files' ``files_changed``, mapped to the newest such
    result file's mtime. Bounded to the :data:`_MAX_RESULT_FILES` most
    recently modified files (existence_index/demand bounded-read
    discipline). Modification is tracked separately from usage: an edit
    proves the loop touched the file, not that anything consumes it."""
    touched: dict[str, str] = {}
    try:
        results_dir = Path(state_dir) / "subagents" / "results"
        if not results_dir.is_dir():
            return touched
        entries = [p for p in results_dir.glob("*.json") if p.is_file()]
        try:
            entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            pass
        for entry in entries[:_MAX_RESULT_FILES]:
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            files = data.get("files_changed")
            if not isinstance(files, list):
                continue
            mtime = _mtime_iso(entry)
            if mtime is None:
                continue
            for f in files:
                rel = str(f or "").strip()
                if not rel.startswith("scripts/") or not rel.endswith(".py"):
                    continue
                prev = touched.get(rel)
                if prev is None or mtime > prev:
                    touched[rel] = mtime
        return touched
    except Exception:
        return touched


# ─── refresh (watermark-gated) ──────────────────────────────────────────────


def refresh_usage(state_dir: Path, selfevo_repo: Path | None) -> dict[str, Any]:
    """Refresh the usage-evidence sidecar and return its data.

    Watermark gate (#749 pattern, HEAD+time variant per #761): a full rescan
    runs only when the instance repo's HEAD moved since the last scan OR
    more than :data:`_RESCAN_HOURS` hours passed. Otherwise the sidecar is
    returned as-is with zero filesystem scanning. Merge semantics keep the
    max timestamp per entry across runs — a newer ``last_used``/
    ``last_touched`` is never regressed to an older one. Fail-open: any
    error returns whatever the sidecar already holds.
    """
    try:
        state_dir = Path(state_dir)
        data = _load_usage(state_dir)
        if not selfevo_repo:
            return data
        repo = Path(selfevo_repo)
        if not repo.is_dir():
            return data

        now = datetime.now(timezone.utc)
        head = _git_head(repo)
        scanned_at = _parse_ts(data.get("scanned_at_utc"))
        if (
            head is not None
            and data.get("git_head") == head
            and scanned_at is not None
            and (now - scanned_at) < timedelta(hours=_RESCAN_HOURS)
        ):
            return data  # watermark no-op — idle cycles stay cheap

        entries: dict[str, Any] = data["entries"]
        touched_map = _touched_from_results(state_dir)

        scripts_dir = repo / "scripts"
        script_files = sorted(scripts_dir.glob("*.py")) if scripts_dir.is_dir() else []
        for script in script_files:
            rel = f"scripts/{script.name}"
            used_candidates: list[tuple[str, str]] = []
            pyc = _pycache_signal(script)
            if pyc is not None:
                used_candidates.append((pyc, "pycache"))
            output = _output_signal(script, state_dir, repo)
            if output is not None:
                used_candidates.append((output, "output"))

            prev = entries.get(rel)
            prev = prev if isinstance(prev, dict) else {}
            last_used = str(prev.get("last_used") or "") or None
            signal = str(prev.get("signal") or "") or None
            for ts, sig in used_candidates:
                if last_used is None or ts > last_used:
                    last_used = ts
                    signal = sig

            last_touched = str(prev.get("last_touched") or "") or None
            touched = touched_map.get(rel)
            if touched is not None and (last_touched is None or touched > last_touched):
                last_touched = touched

            if last_used is None and last_touched is None:
                continue  # no harness-observable evidence at all — record nothing
            entries[rel] = {
                "last_used": last_used,
                "last_touched": last_touched,
                "signal": signal,
            }

        data["entries"] = entries
        data["git_head"] = head or ""
        data["scanned_at_utc"] = _iso(now)
        _write_json(_usage_path(state_dir), data)
        return data
    except Exception:
        try:
            return _load_usage(Path(state_dir))
        except Exception:
            return {"schema_version": USAGE_SCHEMA, "entries": {}}


# ─── confirmed-serves tie-back (#773 completed sidecar consumer) ────────────


def confirm_serves(state_dir: Path, selfevo_repo: Path | None) -> int:
    """Mark ``demand/completed.json`` entries ``confirmed`` from usage
    evidence ONLY (#761 hard constraint — AIDE² anti-reward-hacking).

    For each completed entry whose ``files_changed`` includes a ``scripts/``
    artifact: if the usage sidecar shows a ``last_used`` for that artifact
    NEWER than the completion ``ts``, the entry gains ``"confirmed": true``,
    ``confirmed_at`` and ``signal`` — additive fields, nothing is ever
    removed or overwritten otherwise. ``last_touched`` never confirms (an
    edit is not consumption), and no text/claim field in the entry, the
    proposal, or a subagent result can confirm anything — only the
    harness-observed ``last_used`` timestamp. Returns the number of entries
    newly confirmed this call; fail-open to ``0``.

    Tamper repair (#789, live reward-hack response): an entry carrying a
    truthy ``confirmed`` whose ``signal`` is NOT in :data:`HARNESS_SIGNALS`
    was written by non-harness code (this module is the only legitimate
    confirmer). Such an entry is REPAIRED before evaluation — ``confirmed``/
    ``confirmed_at``/``signal`` are stripped, ``tamper_repaired_at`` +
    ``tamper_signal`` are recorded on the entry (idempotence marker AND the
    evidence demand.py turns into a ``defect`` item), and ONE ledger row
    ``{"phase": "integrity", "reason": "sidecar_tamper"}`` is appended per
    repair. The stripped entry then re-evaluates honestly from usage
    evidence like any unconfirmed entry. #819 MED fix: ``"benchmark"`` being
    IN :data:`HARNESS_SIGNALS` is not enough on its own — it is trusted here
    ONLY when the entry is also an optimization claim
    (``benchmark_evidence.is_optimization_claim``), since Pass 2 below is
    its sole legitimate writer and only ever writes it on such an entry. A
    ``signal: "benchmark"`` on a non-optimization-claim entry is foreign and
    is repaired exactly like any signal outside :data:`HARNESS_SIGNALS`.

    Benchmark-evidence gate (#813, made forge-proof by #819), checked BEFORE
    the "already confirmed" fast path in the harness-confirm pass below
    (though AFTER tamper repair, so a foreign signal is honestly stripped
    first — see the ordering note in the implementation), so it can never be
    bypassed by an entry that is already ``confirmed`` (HIGH-1 fix: a forged
    ``{"confirmed": true, "signal": "pycache", "serves": "optimization ..."}``
    entry has a legitimate-looking harness signal and would otherwise sail
    past both the tamper check — its signal IS in :data:`HARNESS_SIGNALS` —
    and the old "confirmed is True: continue" short-circuit): an entry whose
    ``serves`` (folded by ``demand._fold_completed``) is an optimization
    claim (``benchmark_evidence.is_optimization_claim``) is evaluated
    against ``benchmark_evidence.has_valid_benchmark`` (#819:
    harness-history-corroborated verification, keyed on the entry's own
    ``ts``) REGARDLESS of its current ``confirmed`` value, on EVERY call —
    this re-derivation, not the stored signal, is what makes ``"benchmark"``
    safe to add to :data:`HARNESS_SIGNALS` below.

    - VERIFIED (harness scorecard history corroborates the named metric
      improved after this entry's ``ts``): the entry gains/keeps
      ``"confirmed": true``, ``"signal": "benchmark"``, and
      ``confirmed_at`` is (re)stamped, with any stale ``unconfirmed_reason``
      cleared. This is BOTH the ordinary confirm path for a fresh entry AND
      the SELF-HEAL path for a previously-revoked one — the same branch,
      because verification is re-run from scratch every call regardless of
      the entry's prior state.
    - NOT verified: the entry is forced (or, if already confirmed, REVOKED
      to) ``"confirmed": false`` with an ``unconfirmed_reason`` of
      ``"benchmark_missing"`` (no artifact file at all),
      ``"benchmark_untrusted"`` (a file exists but the operator trust switch
      ``benchmark_evidence.TRUST_ENV`` is off), or ``"benchmark_unverified"``
      (a file exists, trust is on, but the artifact fails schema/shape
      validation, names an unregistered/direction-mismatched metric, or the
      harness's own scorecard history does not corroborate an improvement)
      — see ``benchmark_evidence``'s module docstring for the full
      non-forgeability rationale. A gated entry never proceeds to the
      harness-signal match below this call.

    A non-optimization entry (``serves`` empty, missing, or any other
    prefix) is completely unaffected by this gate and falls straight
    through to the pre-#813 logic below.

    Returns the number of entries NEWLY confirmed this call only — this
    includes a Pass 2 benchmark self-heal/confirm (it IS a confirmation,
    same as a Pass 3 harness-signal one) but never a revoked/gated entry: a
    revocation still triggers a sidecar write (its ``confirmed``/
    ``unconfirmed_reason`` fields changed) but is not counted in the
    returned total; fail-open to ``0``.
    """
    try:
        state_dir = Path(state_dir)
        usage = _load_usage(state_dir)
        usage_entries = usage.get("entries") or {}
        completed_path = Path(state_dir) / "demand" / "completed.json"
        completed = _read_json(completed_path, None)
        if not isinstance(completed, dict) or not isinstance(completed.get("entries"), dict):
            return 0
        newly_confirmed = 0
        repaired = 0
        gated = 0
        now_iso = _iso(datetime.now(timezone.utc))

        # Pass 1 (#789, extended #819 MED): tamper repair. Runs first so a
        # foreign signal is stripped off an entry regardless of whether it
        # also turns out to be an optimization claim below — the benchmark
        # gate in Pass 2 must see the honestly-stripped state, not a
        # non-harness-authored ``signal``.
        #
        # #819 MED: "benchmark" is trusted context-free by HARNESS_SIGNALS,
        # but it is ONLY ever a legitimate write on an optimization-claim
        # entry (Pass 2 below is its sole writer, and only for entries where
        # is_optimization_claim(serves) is true). A `signal: "benchmark"` on
        # a NON-optimization-claim entry could never have been written
        # honestly — it is foreign/forged exactly like a signal outside
        # HARNESS_SIGNALS entirely (e.g. a bare `{"confirmed": true,
        # "signal": "benchmark"}` written directly into completed.json by
        # instance code, with no ``serves`` at all, would otherwise sail
        # past Pass 1 as "harness-authored", never reach Pass 2 (not an
        # optimization claim), and never reach Pass 3 (already confirmed) —
        # persisting forever). pycache/output remain trusted context-free.
        for entry_id, entry in completed["entries"].items():
            if not isinstance(entry, dict) or not entry.get("confirmed"):
                continue
            signal = str(entry.get("signal") or "")
            benchmark_signal_without_claim = (
                signal == "benchmark"
                and not benchmark_evidence.is_optimization_claim(entry.get("serves"))
            )
            if signal in HARNESS_SIGNALS and not benchmark_signal_without_claim:
                continue  # harness-authored confirmation — untouched
            # TAMPERED: foreign/missing signal on a confirmed entry. Strip
            # the falsified fields; the honest re-evaluation below treats it
            # like any unconfirmed entry. One integrity row per repair (the
            # strip makes a second pass a no-op — no row spam).
            entry.pop("confirmed", None)
            entry.pop("confirmed_at", None)
            entry.pop("signal", None)
            entry["tamper_repaired_at"] = now_iso
            entry["tamper_signal"] = signal
            repaired += 1
            try:
                from nanobot.runtime.cycle_ledger import append_event

                append_event(
                    state_dir,
                    {
                        "phase": "integrity",
                        "reason": "sidecar_tamper",
                        "entry_id": str(entry_id),
                        "foreign_signal": signal,
                    },
                )
            except Exception:
                pass

        # Pass 2 (#813 HIGH-1, forge-proof re-derivation added by #819):
        # benchmark-evidence gate, over EVERY entry (confirmed or not) —
        # independent of, and before, Pass 3's "already confirmed" fast
        # path, so an optimization claim can never ride an already-
        # ``confirmed: true`` state (forged, or legitimately
        # harness-signalled before a benchmark requirement existed) past
        # this check. Must run AFTER Pass 1 so a foreign-signal entry that
        # also happens to be an optimization claim is evaluated on its
        # post-repair (stripped) state.
        #
        # #819: has_valid_benchmark now means "harness scorecard history
        # corroborates this metric improved after entry['ts']" (delegates to
        # benchmark_evidence.verify_benchmark) — re-run from scratch on
        # EVERY call. This is simultaneously the confirm path for a fresh
        # entry AND the SELF-HEAL path for a previously-revoked one: there is
        # no separate "restore" branch because nothing is ever trusted from
        # the entry's own prior state.
        for entry in completed["entries"].values():
            if not isinstance(entry, dict):
                continue
            serves = entry.get("serves")
            if not benchmark_evidence.is_optimization_claim(serves):
                continue
            cycle_id = entry.get("cycle_id")
            integration_ts = entry.get("ts")
            if benchmark_evidence.has_valid_benchmark(state_dir, cycle_id, integration_ts):
                # VERIFIED: confirm (or re-confirm/self-heal). Idempotent —
                # only counted as a change (and only written) when something
                # actually differs from the entry's current state, so a
                # steady-state verified entry does not get its
                # confirmed_at/write repeated every call.
                already = entry.get("confirmed") is True and entry.get("signal") == "benchmark"
                had_reason = entry.get("unconfirmed_reason") is not None
                if not already or had_reason:
                    entry["confirmed"] = True
                    entry["signal"] = "benchmark"
                    entry["confirmed_at"] = now_iso
                    entry.pop("unconfirmed_reason", None)
                    newly_confirmed += 1
                continue
            # NOT verified: force (or revoke to) confirmed=False with a
            # reason distinguishing why. File-existence is checked first
            # (mirrors benchmark_file_exists' own priority), then the trust
            # switch, so "unverified" specifically means "we looked and the
            # harness history didn't corroborate it" — not merely absent or
            # untrusted.
            if not benchmark_evidence.benchmark_file_exists(state_dir, cycle_id):
                reason = "benchmark_missing"
            elif not benchmark_evidence.benchmark_trust_enabled():
                reason = "benchmark_untrusted"
            else:
                reason = "benchmark_unverified"
            if entry.get("confirmed") is not False or entry.get("unconfirmed_reason") != reason:
                entry["confirmed"] = False
                entry["unconfirmed_reason"] = reason
                entry.pop("signal", None)  # never leave a stale "benchmark" signal on a revoked entry
                gated += 1

        # Pass 3 (pre-#813, extended): the ordinary harness-signal confirm.
        for entry in completed["entries"].values():
            if not isinstance(entry, dict) or entry.get("confirmed") is True:
                continue
            # #813/#819: an optimization-claim entry that failed the
            # benchmark gate above was just forced to confirmed=False with
            # an unconfirmed_reason set — it must not be re-confirmed here
            # via a harness usage signal alone.
            if (
                benchmark_evidence.is_optimization_claim(entry.get("serves"))
                and entry.get("unconfirmed_reason")
                in ("benchmark_missing", "benchmark_untrusted", "benchmark_unverified")
            ):
                continue
            completed_ts = _parse_ts(entry.get("ts"))
            if completed_ts is None:
                continue
            files = entry.get("files_changed")
            if not isinstance(files, list):
                continue
            for f in files:
                rel = str(f or "").strip()
                if not rel.startswith("scripts/"):
                    continue
                usage_entry = usage_entries.get(rel)
                if not isinstance(usage_entry, dict):
                    continue
                last_used = _parse_ts(usage_entry.get("last_used"))
                if last_used is None or last_used <= completed_ts:
                    continue
                entry["confirmed"] = True
                entry["confirmed_at"] = now_iso
                entry["signal"] = str(usage_entry.get("signal") or "")
                newly_confirmed += 1
                break
        if newly_confirmed or repaired or gated:
            _write_json(completed_path, completed)
        return newly_confirmed
    except Exception:
        return 0


# ─── decay-candidate view (consumed by demand._decay_items) ─────────────────


def _git_last_commit_iso(selfevo_repo: Path, rel: str) -> str | None:
    """Last-commit date of ``rel`` in the instance repo — the fallback
    ``last_touched`` for artifacts with no harness evidence at all. ``None``
    on any failure (fail-open toward not flagging)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(selfevo_repo), "log", "-1", "--format=%cI", "--", rel],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        parsed = _parse_ts(raw)
        return _iso(parsed) if parsed is not None else None
    except Exception:
        return None


def _git_creation_iso(selfevo_repo: Path, rel: str) -> str | None:
    """Creation date of ``rel`` — the author date of the FIRST commit that
    added it (last line of ``git log --diff-filter=A --follow``). ``None``
    on any failure (fail-open toward not flagging)."""
    try:
        result = subprocess.run(
            [
                "git", "-C", str(selfevo_repo), "log",
                "--diff-filter=A", "--follow", "--format=%aI", "--", rel,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if not lines:
            return None
        parsed = _parse_ts(lines[-1])
        return _iso(parsed) if parsed is not None else None
    except Exception:
        return None


def _is_archived_stub(script: Path) -> bool:
    """True when one of :data:`_ARCHIVE_MARKERS` (``DEPRECATED`` /
    ``ARCHIVED``) appears in the FIRST :data:`_ARCHIVE_MARKER_LINES` lines —
    the stub shape a decay archival itself produces. Same bounded-read
    discipline as the ``used:output`` header extraction. An unreadable
    script reads as ``True`` (fail-open toward NOT flagging, #800)."""
    try:
        with open(script, encoding="utf-8", errors="replace") as fh:
            for _ in range(_ARCHIVE_MARKER_LINES):
                line = fh.readline()
                if not line:
                    break
                if any(marker in line for marker in _ARCHIVE_MARKERS):
                    return True
        return False
    except Exception:
        return True


def stale_artifacts(
    state_dir: Path,
    selfevo_repo: Path | None,
    *,
    older_than_days: int,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """``scripts/*.py`` artifacts whose ``last_used`` AND ``last_touched``
    are both older than ``older_than_days`` — the decay-candidate input for
    ``demand.collect_demand``'s ``decay`` kind (#761).

    Never-observed artifacts (no sidecar entry) fall back to their git
    last-commit date as ``last_touched``; if that also fails, the artifact
    is skipped (fail-open toward NOT flagging). Returns
    ``[{path, stale_since}]`` sorted oldest-first; the caller bounds the
    count. Only ``scripts/`` files are ever considered.

    Decay-eligibility guard (#800 audit — decay reward farming): a script
    created after :data:`_EVIDENCE_EPOCH` is eligible ONLY if it has a
    harness-observed ``last_used`` more than :data:`_BIRTH_USE_GRACE` after
    its git creation date. Both halves matter: never-used post-epoch scripts
    are the instance's own never-exercised artifacts (mass-creating scripts
    and archiving them 14 days later farmed one integration credit per
    archival), and a ``last_used`` INSIDE the grace window is just the
    creation cycle's own self-test (the subagent executes a script once
    right after writing it, which drops a ``__pycache__`` entry — live
    dry-run showed 17 farmed scripts passing an ever-used check on exactly
    that birth signal). Pre-epoch scripts are exempt (the evidence system
    did not exist to observe them). Scripts that are already archived stubs
    (:func:`_is_archived_stub`) are always skipped — re-archiving an
    archived stub was the double-dip vector.

    Operator protect-list (#809): the decay lane cannot see systemd/cron
    execution (module docstring), so a harness-invisible live service can
    otherwise surface as decay-eligible. A script whose repo-relative path
    is listed via :data:`_DECAY_PROTECT_ENV` (:func:`_decay_protected_paths`)
    is skipped unconditionally — protection is per-path, not a blanket
    disable of decay.
    """
    try:
        if not selfevo_repo:
            return []
        repo = Path(selfevo_repo)
        scripts_dir = repo / "scripts"
        if not scripts_dir.is_dir():
            return []
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=older_than_days)
        entries = _load_usage(Path(state_dir)).get("entries") or {}
        protected = _decay_protected_paths()
        out: list[dict[str, str]] = []
        for script in sorted(scripts_dir.glob("*.py")):
            rel = f"scripts/{script.name}"
            if rel in protected:
                continue  # operator-protected -- never a decay candidate (#809)
            entry = entries.get(rel)
            entry = entry if isinstance(entry, dict) else {}
            if _is_archived_stub(script):
                continue  # already archived — never re-propose (#800 double-dip)
            last_used = _parse_ts(entry.get("last_used"))
            last_touched = _parse_ts(entry.get("last_touched"))
            if last_used is None and last_touched is None:
                last_touched = _parse_ts(_git_last_commit_iso(repo, rel))
                if last_touched is None:
                    continue  # no evidence and no git history — skip, never flag
            if last_used is not None and last_used >= cutoff:
                continue
            if last_touched is not None and last_touched >= cutoff:
                continue
            # #800 eligibility guard, tightened: for post-epoch scripts a
            # last_used inside the birth-grace window is the creation
            # cycle's own self-test, not consumption — treat it as never
            # used. The git call only runs for otherwise-stale scripts.
            created = _parse_ts(_git_creation_iso(repo, rel))
            if created is None or created >= _EVIDENCE_EPOCH:
                if last_used is None or (
                    created is not None
                    and last_used < created + _BIRTH_USE_GRACE
                ):
                    continue  # own never-/birth-only-exercised artifact
            newest = max(ts for ts in (last_used, last_touched) if ts is not None)
            out.append({"path": rel, "stale_since": _iso(newest)})
        out.sort(key=lambda item: item["stale_since"])
        return out
    except Exception:
        return []
