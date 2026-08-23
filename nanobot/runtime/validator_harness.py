"""Validator harness — run built validators, consume their findings (#925).

Root cause this module addresses: the loop *builds* validator-class scripts
(``check_*``/``validate_*``/``audit_*``/``analyze_*``/``verify_*`` under the
instance repo's ``scripts/``) but nothing ever *runs* them afterward. Usage
evidence (``nanobot.runtime.usage_evidence``) only records consumption that
happens organically, and birth-use deliberately does not count (#800/#802
anti-farming rules) — so genuinely useful artifacts sit unconfirmed forever
and eventually decay. This module is a bounded, periodic, read-only runner
that actually EXERCISES those scripts and feeds their findings back into the
loop as real demand, making the resulting usage evidence genuine rather than
farmed.

Design, one call to :func:`run_validator_harness` per invocation:

1. Select up to :func:`_max_k` (env ``SELFEVO_VALIDATOR_HARNESS_MAX``,
   default 5) scripts matching the allowlist
   ``scripts/(check|validate|audit|analyze|verify)_*.py``, least-recently-run
   first, excluding any script whose own source declares it archived/decayed
   (#928: see :data:`_ARCHIVED_RE` — there is no machine-readable decay
   registry, so the marker text is the only signal; an archived script's
   refusal to run is correct behaviour, not a defect, and must never be
   selected in the first place). Rotation state persists in
   ``<state_dir>/validator_harness/rotation.json`` (same served-map schema
   style as ``llm_proposer``'s #902 demand rotation: ``{"served":
   {"<rel_path>": "<iso-ts>"}}``, pruned to the current candidate set on
   every call). Unlike #902 (which stamps at *presentation* time), this
   stamps at *execution* time only — a script that was merely SELECTED but
   never actually run (total-budget exhaustion) must not appear more
   recently run than it truly was.
2. BIRTH-WINDOW EXCLUSION (#800/#802 semantics, unchanged): a script whose
   git creation commit (``git log --diff-filter=A --format=%cI``) is younger
   than :data:`_BIRTH_WINDOW_HOURS` (24h) is skipped — running (and thus
   confirming) a script the instance JUST wrote would be indistinguishable
   from the birth-use self-test #800/#802 already ban for the pycache/output
   signals. Fail-open here means "cannot determine age -> skip" (never run
   something whose age is unknown), matching the module's overall
   skip-means-not-run discipline.
3. Each selected script is run with ``<venv python> <script> --json`` when
   the script's own text mentions ``--json`` (cheap textual check — this is
   a SELECTION heuristic, not a security boundary), else a plain invocation.
   ``subprocess`` with ``cwd=selfevo_repo``, a per-script timeout
   (:data:`_PER_SCRIPT_TIMEOUT`, 60s) and a total per-invocation budget
   (:data:`_TOTAL_BUDGET_SECONDS`, 240s) that stops selecting further runs
   once exhausted. Environment is inherited unchanged (no env= override).
   stdout/stderr are drained incrementally by a dedicated reader thread per
   stream (:func:`_drain_capped`), each retaining at most
   :data:`_MAX_OUTPUT_BYTES` and discarding — while still READING, never
   stopping — everything beyond that (#928: the earlier ``communicate()``-
   based capture buffered the WHOLE stream before this slice was applied,
   so a runaway printer was bounded only by the unit's ``MemoryMax``, i.e.
   an OOM kill, not by this cap).
4. READ-ONLY DISCIPLINE — this harness writes NO trust input at all
   (2026-08 security review outcome). It is a findings producer, nothing
   more: it never touches ``usage/last_used.json``, never records
   usage evidence, and therefore cannot move ``confirmed_integration`` by
   itself. That is deliberate. An earlier design did record a confirming
   usage signal for runs that produced findings; review showed that any
   phase which BOTH executes instance-authored code AND can write a
   fitness input is forgeable (a validator could write the sidecar for
   OTHER scripts), and that an in-process integrity bracket around such a
   window unavoidably races the concurrently-running bridge (which rewrites
   the same sidecar every cycle), producing false tamper verdicts. Dropping
   the usage write removes the whole class of problem: with nothing
   trust-bearing to write, no bracket is needed and no forgery is possible.
   The metric now improves only the honest way — through the follow-up work
   the findings generate.
   a. SANDBOX (physical prevention, the ONLY control needed): the systemd
      unit (``host/eeepc/systemd/eeebot-validator-harness.service``) confines
      the whole unit — the harness process AND every validator subprocess it
      spawns, which share its cgroup and mount namespace — to writing ONLY
      ``state/validator_harness/``. Everything else is read-only, INCLUDING
      the instance repo, and the subtrees that actually feed
      ``confirmed_integration`` (``demand/``, ``scorecard/``, ``promotions/``,
      ``goals/``, ``usage/``) are fully INACCESSIBLE. ``ledger/`` is the one
      exception (#928): it is merely READ-ONLY, not inaccessible, because a
      large family of validators in the INSTANCE repo exists to read it and
      the first production run crashed two of them with ``PermissionError``
      — see the unit file's own comment for what that read access does and
      does not give a hostile validator.
   b. WHY NO GIT BRACKET: an earlier revision compared ``git status
      --porcelain`` before/after each run and restored the tree with ``git
      checkout -- .`` + ``git clean -fd`` when it differed. That was unsafe:
      the instance repo is a checkout SHARED with the bridge, which holds
      uncommitted subagent work mid-cycle and serializes only against other
      bridge runs (its own ``bridge.lock``). An unlocked restore could
      therefore destroy in-flight work, and an unlocked comparison could
      blame an innocent validator for the bridge's own writes. A read-only
      repo mount removes both: a validator simply cannot dirty the tree, so
      there is nothing to detect or undo — and the ``pycache`` usage signal
      (repo ``__pycache__`` mtimes) becomes unreachable too.
5. Results are appended to ``<state_dir>/validator_harness/last_runs.jsonl``
   (one JSON line per run; findings are parsed with a deliberately tiny,
   fail-open heuristic — the count of items in any top-level JSON
   list/dict field literally named ``findings``/``alerts``/``missing``/
   ``failures``; anything else falls back to "no findings parsed", never a
   crash). On write the file is brought back inside BOTH a row count
   (:data:`_MAX_LAST_RUNS_LINES`) and a byte total
   (:data:`_MAX_LAST_RUNS_KEEP_BYTES`) by :func:`_select_within_budget`,
   which keeps the newest row per path in preference to older ones — not by
   a tail slice, which was itself an eviction channel (#928 round 4).
   ``demand._validator_defect_items`` reads this sidecar and turns a non-zero
   exit or a positive findings count into bounded ``defect`` demand — a validator that finds a real problem
   generates real follow-up work.

Fail-open and bounded throughout, with ONE deliberate exception (#928): any
error in selection or in a single script's execution never raises into the
caller and never blocks another script's run or the bridge loop, but a
``<state_dir>/validator_harness/`` directory that turns out not to be
writable is reported via ``result["errors"]`` (and a non-zero ``main()``
exit code) rather than silently swallowed — every write into it was
otherwise fail-open, so a broken carve-out used to make the unit report
success while recording nothing. This module's own writes are confined to
``<state_dir>/validator_harness/`` — never a fitness sidecar, never a git
commit, never a mutation of the instance repo (which the sandbox mounts
read-only anyway).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROTATION_SCHEMA = "validator-harness-rotation-v1"
_LAST_RUNS_FILENAME = "last_runs.jsonl"
_MAX_LAST_RUNS_LINES = 500  # bounded growth (#925), same discipline as scorecard/benchmark history
# #928: a validator can append to last_runs.jsonl itself, and demand REFUSES
# to read the file at all once it exceeds its own 2 MB guard — which silences
# every validator defect, real ones included. So _prune_last_runs bounds what
# it WRITES, well under that guard, rather than only bounding what it reads:
# a budget the round-2 review demonstrated was missing, by filling the file
# with 300 medium rows (~3 MB total, no single line over the per-line cap) and
# watching the prune leave it untouched.
#
# Byte counts throughout, not character counts: records are serialised with
# ensure_ascii=False, so one 16 000-character line can be ~64 KB on disk.
_MAX_LAST_RUNS_LINE_BYTES = 16 * 1024  # per record
_MAX_LAST_RUNS_KEEP_BYTES = 1024 * 1024  # total kept, vs demand's 2 MB refusal
_MAX_READ_BYTES = 8 * 1024 * 1024  # refuse to even read past this

_MAX_K_ENV = "SELFEVO_VALIDATOR_HARNESS_MAX"
_DEFAULT_MAX_K = 5

_PER_SCRIPT_TIMEOUT = 60.0  # seconds, hard cap per script
_TOTAL_BUDGET_SECONDS = 240.0  # seconds, hard cap for the whole invocation

_BIRTH_WINDOW_HOURS = 24  # #800/#802: never run (and thus never confirm) a just-created script

_MAX_OUTPUT_BYTES = 64 * 1024  # captured stdout/stderr cap per run
_MAX_SCAN_BYTES = 200_000  # bounded read for the cheap "--json" text check

# scripts/(check|validate|audit|analyze|verify)_*.py — the built-validator class
_ALLOWLIST_RE = re.compile(r"^(check|validate|audit|analyze|verify)_.*\.py$")

# #928: there is NO machine-readable decay registry anywhere in this repo —
# whether a script has been archived/decayed exists only as text the script
# prints (or the marker its own body carries) at run time, e.g.
#   "WARNING: scripts/analyze_repo_size.py is deprecated and marked as
#    archived (decay-36bd86468443) as unused."
#   "Error: Execution is disabled because this script is archived."
# An archived script's declared contract is "do not run me"; running it and
# then scoring its (correct) refusal as a crash manufactures a false defect.
# On the live host 11 of 42 allowlisted validators carried this marker, 6 of
# them still exiting non-zero (counted 2026-08-23; the scripts live in the
# INSTANCE repo, not here — this repo has two allowlisted scripts and no
# marker hits). Matched against the SOURCE TEXT: the marker is the script's
# own self-declaration, not something we have to execute it to observe.
#
# The wording is LLM-authored — nothing in this repo generates it — so it
# will drift, and a variant this pattern misses lands back in the false-defect
# path. Be honest about that cost: it is NOT one wasted cycle and the
# sandbox-denial marker in _run_one does not cover it (an archived refusal is a
# plain non-zero exit, nothing resembling a PermissionError). The script stays
# a candidate, so it is re-selected and re-fails every rotation, and the 7-day
# completed-TTL re-presents it as demand every week until the pattern is
# widened or the script is deleted. The fail-open direction is still "treat as
# a candidate", because assuming archival on an unreadable file would silently
# stop running a real validator — a worse trade than a visible recurring
# false defect.
_ARCHIVED_RE = re.compile(r"marked as archived|script is archived")

# #928: the ONE error that makes main() exit non-zero (see its comment).
_NOT_WRITABLE_ERROR = "state_dir_not_writable"

# #928: a run that failed because THIS unit's sandbox denied it a read is not
# a defect in the script. Matched against the child's stderr; see the marker
# in _run_one for why the exception name alone is the right signal here.
_SANDBOX_DENIAL_RE = re.compile(r"PermissionError|Errno 13|Permission denied")

# Tiny, deliberately narrow findings-count heuristic (#925 design: "keep the
# parse heuristic tiny and fail-open to raw exit code"). Only a top-level
# JSON object with one of these list/dict fields counts; anything else
# yields None (no findings parsed), never a crash or a guess.
_FINDINGS_KEYS = ("findings", "alerts", "missing", "failures")


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


def _max_k() -> int:
    """#925: ``SELFEVO_VALIDATOR_HARNESS_MAX`` env override for K, default
    5; unset, empty, non-numeric, or non-positive falls back to the
    default."""
    raw = os.environ.get(_MAX_K_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_K
    try:
        value = int(raw)
    except Exception:
        return _DEFAULT_MAX_K
    return value if value > 0 else _DEFAULT_MAX_K


# ─── selection ───────────────────────────────────────────────────────────


def _scan_head(script: Path) -> str:
    """First :data:`_MAX_SCAN_BYTES` characters of ``script``'s source, for
    the cheap textual checks below. Bounded on purpose (#928): the file is
    instance-authored, so slurping it whole would put an attacker-chosen
    length inside a unit capped at ``MemoryMax=512M``. Raises on read
    failure; every caller fails open."""
    with script.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read(_MAX_SCAN_BYTES)


def _is_archived(script: Path) -> bool:
    """#928: does the script's own source declare itself archived/decayed
    (see :data:`_ARCHIVED_RE`)? Bounded read, same pattern as
    :func:`_accepts_json_flag` — this scans text, it does not execute
    anything. Fail-open to ``False`` (NOT archived, i.e. still a candidate)
    on any read error: an unreadable script will simply fail to run on its
    own, which is an honest outcome, not a fabricated one."""
    try:
        return bool(_ARCHIVED_RE.search(_scan_head(script)))
    except Exception:
        return False


def _candidate_scripts(selfevo_repo: Path) -> list[Path]:
    """Allowlisted validator-class scripts in the instance repo, sorted for
    determinism, excluding any that declare themselves archived/decayed
    (#928: an archived script's refusal to run is correct behaviour, not a
    defect). Fail-open: no ``scripts/`` dir or any error yields ``[]``."""
    try:
        scripts_dir = Path(selfevo_repo) / "scripts"
        if not scripts_dir.is_dir():
            return []
        return sorted(
            p
            for p in scripts_dir.glob("*.py")
            if _ALLOWLIST_RE.match(p.name) and not _is_archived(p)
        )
    except Exception:
        return []


def _git_creation_iso(selfevo_repo: Path, rel: str) -> str | None:
    """Author-independent creation date of ``rel`` (the committer date of
    the FIRST commit that added it) — ``None`` on any failure (fail-open;
    the caller treats an unknown creation date as "in the birth window",
    i.e. never run something whose age it cannot establish)."""
    try:
        result = subprocess.run(
            [
                "git", "-C", str(selfevo_repo), "log",
                "--diff-filter=A", "--format=%cI", "--", rel,
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
        return lines[-1]  # oldest add-commit = creation
    except Exception:
        return None


def _in_birth_window(selfevo_repo: Path, rel: str, now: datetime) -> bool:
    """True iff ``rel`` was created less than :data:`_BIRTH_WINDOW_HOURS`
    ago, OR its creation date cannot be determined at all (fail-open toward
    NOT running it — #800/#802: birth-use must never confirm)."""
    created = _parse_ts(_git_creation_iso(selfevo_repo, rel))
    if created is None:
        return True
    return (now - created) < timedelta(hours=_BIRTH_WINDOW_HOURS)


# ─── rotation state (least-recently-run) ────────────────────────────────


def _rotation_path(state_dir: Path) -> Path:
    return Path(state_dir) / "validator_harness" / "rotation.json"


def _load_rotation(state_dir: Path) -> dict[str, Any]:
    """Load rotation state; missing/corrupt/malformed all fail-open to
    "nothing run yet" rather than aborting the harness invocation."""
    try:
        path = _rotation_path(state_dir)
        if not path.is_file():
            return {"schema_version": _ROTATION_SCHEMA, "served": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("served"), dict):
            return {"schema_version": _ROTATION_SCHEMA, "served": {}}
        return data
    except Exception:
        return {"schema_version": _ROTATION_SCHEMA, "served": {}}


def _write_rotation(state_dir: Path, data: dict[str, Any]) -> None:
    """Write-temp-then-``os.replace`` so a crash mid-write never leaves a
    half-written ``rotation.json`` behind for the next invocation."""
    try:
        path = _rotation_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        pass


def _rotation_key(script: Path, served: dict[str, str]) -> tuple[int, str, str]:
    """Sort key for least-recently-run-first: never-run scripts (no
    rotation entry) sort before any run one; among run ones, oldest
    timestamp first; ties broken by path for determinism."""
    rel = f"scripts/{script.name}"
    parsed = _parse_ts(served.get(rel))
    if parsed is None:
        return (0, "", rel)
    return (1, _iso(parsed), rel)


# ─── execution ───────────────────────────────────────────────────────────


def _accepts_json_flag(script: Path) -> bool:
    """Cheap textual check (not a security boundary): does the script's own
    source mention ``--json`` anywhere in its first :data:`_MAX_SCAN_BYTES`
    characters? Fail-open to ``False`` (plain invocation) on any read error.

    #928: reads a BOUNDED prefix rather than the whole file. These scripts are
    instance-authored, so their size is not ours to trust, and this runs once
    per candidate inside a unit capped at ``MemoryMax=512M``."""
    try:
        return "--json" in _scan_head(script)
    except Exception:
        return False


def _parse_findings_count(stdout: str) -> int | None:
    """Deliberately tiny findings heuristic: a top-level JSON object with a
    ``findings``/``alerts``/``missing``/``failures`` field that is a list or
    dict yields that field's length; anything else (unparseable, not an
    object, no matching field) yields ``None`` — the caller then falls back
    to the raw exit code alone, never a fabricated count."""
    try:
        data = json.loads(stdout)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in _FINDINGS_KEYS:
        value = data.get(key)
        if isinstance(value, (list, dict)):
            return len(value)
    return None


def _process_group_id(proc: "subprocess.Popen[str] | None") -> int | None:
    """The process-group id of a just-spawned child (POSIX only; ``None``
    elsewhere or on any error).

    Derived from the pid rather than asked of the kernel. ``Popen`` is called
    with ``start_new_session=True``, which makes the child a session AND
    process-group leader, so its pgid IS its pid by construction. The earlier
    ``os.getpgid(proc.pid)`` raced the child's own ``setsid()`` — that call
    happens in the child between fork and exec, and until it lands the child
    is still in the PARENT's group, so in that window ``getpgid`` returned
    the HARNESS's own pgid. :func:`_kill_process_group` would then
    ``killpg(SIGKILL)`` that group, killing the harness and taking the whole
    systemd unit down with it. Narrow window, unbounded consequence.

    The equality check below is the belt to that braces: never hand back a
    group we are ourselves a member of, whatever the arithmetic says."""
    if proc is None or os.name != "posix":
        return None
    pgid = proc.pid
    try:
        if pgid == os.getpgrp():
            return None
    except Exception:
        return None
    return pgid


def _kill_process_group(
    proc: "subprocess.Popen[str] | None", pgid: int | None = None
) -> None:
    """Kill the WHOLE process group a validator spawned into (via
    ``start_new_session=True``), not just the direct child — a plain
    ``proc.kill()`` leaves forked grandchildren running past the cap.
    ``pgid`` comes from :func:`_process_group_id`, which derives it from the
    pid, so unlike the earlier ``os.getpgid`` version it stays valid after the
    child is reaped. POSIX-only; falls back to killing the direct child, and
    swallows any failure (the process may already be dead)."""
    if pgid is not None and os.name == "posix":
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except Exception:
            pass
    if proc is None:
        return
    try:
        proc.kill()
    except Exception:
        pass


def _drain_capped(stream: Any, sink: list[str], cap: int) -> None:
    """Reader-thread body (#928): read ``stream`` in a loop until EOF,
    keeping only the first ``cap`` chars in ``sink`` and discarding
    everything read beyond that. This is the piece that makes the output
    cap enforced DURING capture rather than after ``communicate()`` has
    already buffered the whole stream in memory (previously bounded only by
    ``MemoryMax``, i.e. an OOM kill, for a runaway printer).

    CRITICAL: this loop must keep calling ``stream.read()`` even after the
    cap is reached, discarding the excess, rather than returning early. A
    pipe has a finite OS buffer; if nobody drains it once full, the child
    blocks on its next write and hangs until the per-script timeout kills
    it — turning a merely chatty (but otherwise fine) validator into a
    bogus timeout record. Swallows any read error (e.g. the pipe closing
    from under it during a kill) — this is a background drain, never the
    source of truth for ``exit_code``."""
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if total < cap:
                keep = chunk[: cap - total]
                sink.append(keep)
                total += len(keep)
            # else: deliberately discarded -- still consumed to keep draining
    except Exception:
        pass


def _run_one(script: Path, selfevo_repo: Path, timeout: float) -> dict[str, Any]:
    """Run a single validator script under the read-only/bounded discipline
    described in the module docstring. Never raises — timeouts and any
    other execution error are captured as ``exit_code: None`` records
    rather than propagated. Uses ``Popen`` directly (rather than
    ``subprocess.run``'s own timeout handling) so a timeout can kill the
    script's whole process GROUP, not just the direct child — see
    :func:`_kill_process_group`.

    #928: stdout/stderr are drained by one dedicated thread per stream (see
    :func:`_drain_capped`) rather than ``communicate(timeout=...)``, which
    would buffer the ENTIRE output before the :data:`_MAX_OUTPUT_BYTES`
    slice is applied. The threads start before ``proc.wait()`` is called so
    a runaway printer's pipes are always being drained (never blocking the
    child) while the main thread separately waits for termination / the
    timeout."""
    rel = f"scripts/{script.name}"
    started = datetime.now(timezone.utc)

    cmd = [sys.executable, str(script)]
    if _accepts_json_flag(script):
        cmd.append("--json")

    exit_code: int | None = None
    stdout = ""
    stderr = ""
    proc: "subprocess.Popen[str] | None" = None
    pgid: int | None = None
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    reader_threads: list[threading.Thread] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(selfevo_repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # POSIX: new session -> new process group, so a timed-out run's
            # forked grandchildren die together with it (killpg below).
            # Harmless no-op on Windows.
            start_new_session=True,
        )
        pgid = _process_group_id(proc)
        reader_threads = [
            threading.Thread(
                target=_drain_capped,
                args=(proc.stdout, stdout_chunks, _MAX_OUTPUT_BYTES),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_capped,
                args=(proc.stderr, stderr_chunks, _MAX_OUTPUT_BYTES),
                daemon=True,
            ),
        ]
        for t in reader_threads:
            t.start()
        proc.wait(timeout=timeout)
        exit_code = proc.returncode
        # Kill the group BEFORE joining the readers, not after. A validator
        # can fork a child that inherits the pipe write ends; the readers then
        # never see EOF, so each join would burn its full 5s (10s per such
        # script, out of a 240s invocation budget) and the chunk lists could
        # still be growing while being joined below.
        #
        # Killing first releases those fds for anything still IN the process
        # group. It does not help against a grandchild that called setsid()
        # and left the group — killpg cannot reach that, so the joins do time
        # out and the capture for that one script is whatever arrived first.
        # Bounded either way, which is the point; see the note below on why
        # the pipes are then left for the GC rather than closed here.
        _kill_process_group(proc, pgid)
        for t in reader_threads:
            t.join(timeout=5)
        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc, pgid)
        try:
            if proc is not None:
                proc.wait(timeout=5)
        except Exception:
            pass
        for t in reader_threads:
            t.join(timeout=5)
        stderr = f"timeout after {timeout:.0f}s"
    except Exception as exc:
        _kill_process_group(proc, pgid)
        for t in reader_threads:
            t.join(timeout=5)
        stderr = f"error: {exc}"

    # Belt to the braces above: killpg is idempotent (ESRCH is swallowed),
    # and this covers any path that reached here without one — a validator
    # that double-forks a detached grandchild must not outlive its caps.
    _kill_process_group(proc, pgid)

    # NOT closing proc.stdout/proc.stderr here, deliberately. proc.wait()
    # leaves them open where communicate() would have closed them, so each run
    # leaves two TextIOWrapper objects to the garbage collector — but closing
    # them explicitly DEADLOCKS this function: close() acquires the same io
    # lock a reader thread already holds while blocked inside read(), which is
    # precisely the state after its join(timeout=5) above has expired. That
    # happens whenever a process still holds the pipe write end, e.g. a
    # grandchild that called setsid() and so escaped the group killpg() can
    # reach. Measured on a validator that spawns a detached sleeper and exits
    # 1: 10.2s to return without the close, never returning with it — and
    # since the record is appended and the rotation stamped only AFTER this
    # function returns, a hang means no row, no rotation stamp, the same
    # script selected first next time, and the unit SIGKILLed at
    # TimeoutStartSec every 6h with nothing recorded to explain it. A leaked
    # wrapper in a oneshot process is the cheaper of the two.

    finished = datetime.now(timezone.utc)

    record: dict[str, Any] = {
        "path": rel,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "duration_s": round((finished - started).total_seconds(), 3),
        "exit_code": exit_code,
        "findings_count": _parse_findings_count(stdout),
        "stderr_tail": stderr[-2000:],
    }
    if isinstance(exit_code, int) and exit_code != 0 and _SANDBOX_DENIAL_RE.search(stderr):
        # #928: this run did not fail because the script is broken — it failed
        # because THIS unit's sandbox denied it a read. Two of the three false
        # defects from the harness's first production run were exactly that
        # (validators whose purpose is reading state/ledger, which was in
        # InaccessiblePaths=). Removing ledger from that list fixes those two;
        # this marker closes the class, because demand/, scorecard/, goals/,
        # promotions/ and usage/ are still inaccessible and a validator has
        # every reason to read some of them.
        #
        # Deliberately keyed on the exception name alone, not on the denied
        # path: under this sandbox the whole instance tree is read-only and
        # several subtrees are unreadable, so EACCES is far more likely to be
        # the sandbox than the script. The trade is explicit — a genuine
        # script bug that surfaces only as EACCES stops becoming demand — and
        # it is the right way round, because a false defect actively poisons
        # the loop while a missed one merely goes unreported.
        record["harness_env_error"] = "permission_denied"
    return record


def _last_runs_path(state_dir: Path) -> Path:
    return Path(state_dir) / "validator_harness" / _LAST_RUNS_FILENAME


def _select_within_budget(
    lines: list[str],
    valid_rels: "set[str] | None" = None,
    max_lines: "int | None" = None,
) -> list[str]:
    """Choose which sidecar rows to keep, in their original order.

    Two passes, and the order matters (#928 round-3 review). Pass 1 takes the
    NEWEST row per path; pass 2 spends whatever budget is left on older rows,
    newest first. A single newest-first pass looked equivalent and was not: a
    validator that appends rows naming ITSELF fills the newest bytes, and the
    budget then evicts every other script's newest verdict — deleting it,
    where merely exceeding demand's read guard had at least left the row on
    disk. Demand presents only the newest row per path, so pass 1 is exactly
    the set that must not be sacrificed to make room.

    EVERY bound goes through this function (#928 round-4 review). Both the
    byte cap and ``max_lines`` are applied inside the two passes, because a
    raw tail slice applied BEFORE them defeats the whole design: the passes
    never see the rows the slice already discarded. That is how the line trim
    stayed destructive after the byte trim was fixed, and it was two orders of
    magnitude cheaper to exploit — 500 minimal rows is about 25 KB, nowhere
    near any byte bound, and the harness itself performed the deletion on its
    next append with no attacker timing required.

    Pass 1 is bounded by the number of distinct paths, so pass ``valid_rels``
    wherever the caller knows the candidate set: with it, that number is the
    candidate count and pass 1 is small by construction. ``None`` means "keep
    every path", which leaves the count attacker-controlled — acceptable only
    for direct calls in tests, never on a production path.

    Neither pass is unconditional: a row that does not fit the remaining
    budget is skipped (``continue``, not ``break``, so a small row after a
    large one is still taken). When even the newest-per-path set does not fit,
    the SMALLEST rows win — see the comment on that sort for why size, not
    recency, is the right tie-break against an attacker who chooses sizes.
    """
    parsed: list[tuple[int, str, str, int]] = []  # index, line, rel, bytes
    for index, line in enumerate(lines):
        encoded = len(line.encode("utf-8")) + 1
        if not line.strip() or encoded > _MAX_LAST_RUNS_LINE_BYTES:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        rel = str(row.get("path") or "")
        if valid_rels is not None and rel not in valid_rels:
            continue
        parsed.append((index, line, rel, encoded))

    keep: set[int] = set()
    budget = 0

    def fits(encoded: int) -> bool:
        if budget + encoded > _MAX_LAST_RUNS_KEEP_BYTES:
            return False
        return not (max_lines is not None and len(keep) >= max_lines)

    seen: set[str] = set()
    newest_per_path: list[tuple[int, int]] = []  # index, encoded
    for index, _line, rel, encoded in reversed(parsed):
        if rel in seen:
            continue
        seen.add(rel)
        newest_per_path.append((index, encoded))

    # Admit the newest-per-path set SMALLEST ROW FIRST (#928 round-5 review).
    # This only matters when the whole set does not fit, and then it decides
    # who is sacrificed. Newest-path-first was the wrong answer: pass 1 is
    # bounded by the NUMBER of distinct candidate paths, but each row's SIZE
    # is attacker-chosen up to _MAX_LAST_RUNS_LINE_BYTES, so a validator that
    # forges one padded ~16 KB row for each of ~65 other real candidate
    # scripts fills the budget exactly and the next append drops a genuine
    # 120-byte verdict. Smallest-first inverts that: padding a forgery makes
    # it the first thing dropped, not the last. Ties break toward the newer
    # row for determinism.
    for index, encoded in sorted(newest_per_path, key=lambda item: (item[1], -item[0])):
        if not fits(encoded):
            continue
        budget += encoded
        keep.add(index)

    for index, _line, _rel, encoded in reversed(parsed):
        if index in keep:
            continue
        if not fits(encoded):
            continue
        budget += encoded
        keep.add(index)

    return [line for index, line, _rel, _encoded in parsed if index in keep]


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step. Atomic because demand reads
    this file concurrently and must never see a torn or empty version, and
    uuid-suffixed because a validator subprocess can create paths in this
    directory: a fixed ``.tmp`` name is squattable (``mkdir`` it and every
    write here fails open, silently disabling both the prune and the trim).

    "Atomic" is precise on POSIX, where this is ``rename(2)`` within one
    directory. On Windows ``replace`` can fail outright while another process
    holds the target open; the caller swallows that and the file is left as it
    was, which is the safe direction. The host is Linux."""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        # newline="" for the same reason as the append: keep the bytes on disk
        # equal to the bytes _select_within_budget counted.
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        tmp.replace(path)
    finally:
        # Any failure after the write (ENOSPC, EIO, or a Windows sharing
        # violation on replace while demand holds the file open) would
        # otherwise orphan the temp file: callers swallow the exception,
        # nothing prunes this directory, and it has no size bound.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _prune_last_runs(state_dir: Path, valid_rels: set[str]) -> None:
    """Drop rows the harness will never refresh (#928).

    ``demand._validator_defect_items`` presents the LAST row per script, so a
    row outlives whatever produced it until a newer row for the same path
    replaces it. That is fine for a script still in rotation, but a script
    that has been archived or deleted is never selected again — its failing
    row would stay newest for the ~25 days it takes to scroll out of the
    :data:`_MAX_LAST_RUNS_LINES` window, with the 7-day completed-TTL
    re-presenting it as demand every week. Since this file is the harness's
    own store, the harness prunes it.

    Also drops absurdly long lines. Every validator subprocess can append
    here (it is the unit's one writable carve-out), and a single line over
    ``demand``'s 2 MB sidecar guard silences ALL validator demand — real
    defects included. Over-long lines are now dropped at parse time by both
    this prune and the append path, so such a line cannot survive a single
    write; the tail slice that used to preserve it is gone (#928 round 4).

    Fail-open and bounded: any error leaves the file exactly as it was."""
    try:
        path = _last_runs_path(state_dir)
        if not path.is_file():
            return
        if path.stat().st_size > _MAX_READ_BYTES:
            # Past anything a legitimate run sequence could produce; reading it
            # to filter would defeat the point of the bound.
            _atomic_write(path, "")
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = _select_within_budget(
            lines, valid_rels, max_lines=_MAX_LAST_RUNS_LINES
        )
        if len(kept) == len(lines):
            return
        _atomic_write(path, ("\n".join(kept) + "\n") if kept else "")
    except Exception:
        pass


def _append_last_run(
    state_dir: Path, record: dict[str, Any], valid_rels: "set[str] | None" = None
) -> None:
    """Append one run record, then bring the file back inside BOTH bounds —
    :data:`_MAX_LAST_RUNS_LINES` rows and :data:`_MAX_LAST_RUNS_KEEP_BYTES`
    total — through :func:`_select_within_budget`, so the newest verdict per
    path survives either bound being hit.

    Both bounds are enforced here and not only in :func:`_prune_last_runs`
    (which now applies the identical pair), because the prune runs once at
    the top of an invocation: a validator that appends to this file during
    its own run would otherwise have 6h of free rein, either to push the
    file past demand's read guard or (before #928
    round 4) to force a tail slice that deleted another script's verdict.

    ``valid_rels`` should be the current candidate set; passing it is what
    keeps the per-path pass bounded by the candidate count rather than by an
    attacker-chosen number of distinct paths.

    Fail-open: a sidecar write failure never breaks the run loop."""
    try:
        path = _last_runs_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" so the byte accounting in _select_within_budget matches
        # what actually lands on disk; the default would translate each \n to
        # \r\n on Windows and quietly overshoot the budget by the row count.
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = _select_within_budget(
            lines, valid_rels, max_lines=_MAX_LAST_RUNS_LINES
        )
        if len(kept) != len(lines):
            _atomic_write(path, ("\n".join(kept) + "\n") if kept else "")
    except Exception:
        pass


# ─── entrypoint ──────────────────────────────────────────────────────────


def _probe_writable(state_dir: Path) -> bool:
    """#928: every write into ``<state_dir>/validator_harness/`` is
    otherwise fail-open (:func:`_write_rotation`, :func:`_append_last_run`),
    so a broken writable carve-out (e.g. a misconfigured unit
    ``ReadWritePaths=``) previously made the unit exit 0 while recording
    nothing, with no signal anything was wrong. Create the directory if
    missing and write+remove a small probe file before doing any real work,
    so that failure is reported instead of silently swallowed."""
    try:
        directory = Path(state_dir) / "validator_harness"
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write_probe.{uuid.uuid4().hex[:8]}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def run_validator_harness(state_dir: Path, selfevo_repo: Path) -> dict[str, Any]:
    """Run one bounded validator-harness invocation. See the module
    docstring for the full design. Returns
    ``{"selected": [...], "ran": [...], "skipped_birth_window": [...],
    "errors": [...]}`` (repo-relative ``scripts/*.py`` paths). Fail-open for
    everything EXCEPT the writable-directory probe below (#928): a broken
    ``state/validator_harness`` carve-out must be reported loudly, not
    swallowed into an empty-but-successful-looking result. Any other
    unexpected error yields whatever was collected so far plus an
    ``"errors"`` note, never a raised exception."""
    result: dict[str, Any] = {
        "selected": [],
        "ran": [],
        "skipped_birth_window": [],
        "errors": [],
    }
    try:
        state_dir = Path(state_dir)
        selfevo_repo = Path(selfevo_repo)
        if not _probe_writable(state_dir):
            result["errors"].append(_NOT_WRITABLE_ERROR)
            return result
        if not selfevo_repo.is_dir():
            return result

        candidates = _candidate_scripts(selfevo_repo)
        if not candidates:
            # Guarded return, and _prune_last_runs is deliberately BELOW it:
            # _candidate_scripts fails open to [], and pruning against an
            # empty valid set would wipe every verdict on a transient error.
            return result

        _prune_last_runs(state_dir, {f"scripts/{p.name}" for p in candidates})

        now = datetime.now(timezone.utc)
        eligible: list[Path] = []
        for script in candidates:
            rel = f"scripts/{script.name}"
            if _in_birth_window(selfevo_repo, rel, now):
                result["skipped_birth_window"].append(rel)
            else:
                eligible.append(script)

        rotation = _load_rotation(state_dir)
        all_rels = {f"scripts/{p.name}" for p in candidates}
        served: dict[str, str] = {
            k: v for k, v in dict(rotation.get("served") or {}).items() if k in all_rels
        }

        if eligible:
            eligible.sort(key=lambda s: _rotation_key(s, served))
            selected = eligible[: _max_k()]
            result["selected"] = [f"scripts/{p.name}" for p in selected]

            deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS
            for script in selected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                record = _run_one(script, selfevo_repo, min(_PER_SCRIPT_TIMEOUT, remaining))
                rel = record["path"]
                served[rel] = record["finished_at"]
                _append_last_run(state_dir, record, all_rels)
                result["ran"].append(rel)
                # Persist rotation after EVERY run, not once at the end: a
                # systemd timeout or an OOM kill mid-loop would otherwise lose
                # all rotation progress, and since never-run scripts sort
                # first, the same head-of-list scripts would be re-selected
                # forever while the tail never ran.
                _write_rotation(
                    state_dir, {"schema_version": _ROTATION_SCHEMA, "served": served}
                )

        _write_rotation(state_dir, {"schema_version": _ROTATION_SCHEMA, "served": served})
        return result
    except Exception:
        result["errors"].append("harness_failed")
        return result


def _default_state_root() -> Path:
    return Path(os.environ.get("STATE_DIR", "/var/lib/eeepc-agent/self-evolving-agent/state"))


def _default_repo(state_root: Path) -> Path:
    return state_root.parent / "eeebot-self-evolving"


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the systemd oneshot timer
    (``eeebot-validator-harness.service``). Explicit ``--state-root``/
    ``--repo`` args (the unit passes both explicitly) rather than bridge.py's
    env-derivation style, so this module stays independently testable/
    invocable without coupling to the bridge's own env-file — falls back to
    ``$STATE_DIR`` (bridge's own default) / ``<state-root>/../eeebot-self-evolving``
    when the flags are omitted, same convention ``scripts/cleanup_subagent_queue.py``
    uses for its ``--state-root``."""
    parser = argparse.ArgumentParser(
        description="Run the validator harness — execute built validator-class "
        "scripts and record their findings (#925)."
    )
    parser.add_argument(
        "--state-root", type=str, default=None,
        help="State directory root (default: $STATE_DIR or "
        "/var/lib/eeepc-agent/self-evolving-agent/state)",
    )
    parser.add_argument(
        "--repo", type=str, default=None,
        help="Instance repo path (default: <state-root>/../eeebot-self-evolving)",
    )
    args = parser.parse_args(argv)

    state_root = Path(args.state_root) if args.state_root else _default_state_root()
    repo = Path(args.repo) if args.repo else _default_repo(state_root)

    result = run_validator_harness(state_root, repo)
    print(json.dumps(result, indent=2))
    # #928: a broken writable carve-out means the run did NOT do its job, so
    # the unit's exit code must say so instead of a misleadingly successful 0.
    # Scoped to THAT error only, deliberately: "errors" also carries the
    # generic catch-all's "harness_failed", and this module's contract is
    # fail-open -- a transient error in one script must not fail the unit.
    return 1 if _NOT_WRITABLE_ERROR in (result.get("errors") or []) else 0


if __name__ == "__main__":
    raise SystemExit(main())
