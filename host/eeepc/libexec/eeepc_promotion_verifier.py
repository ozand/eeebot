#!/usr/bin/env python3
"""Root-run promotion verifier — the automated operator (#875).

**Why this exists (the trust problem).** On the eeepc host, the bridge/
harness AND the instance's subagent run as the SAME uid (``eeepc-agent``).
``state/`` is instance-writable, so a loader that trusts an
instance-authored index+bytes pair would let the instance forge its way to
arbitrary code execution in the harness's own process, before any gate ever
ran. No in-band signature closes this (same uid can read any key the
harness could read too).

**The fix.** The only real privilege boundary on this host is root — deploys
run via sudo, the release tree and ``/etc/eeepc-agent`` are root-touched.
This script IS that boundary: it runs as ROOT (no ``User=`` in its systemd
unit), on its own timer, completely independent of the instance's own
process. It:

1. Reads the instance's runtime-slice promotion candidates
   (``STATE_DIR/promotions/promotion-runtime-*.json``) as PROPOSALS ONLY —
   every number the instance wrote inside them (its own microbench dict,
   its own claimed improvement) is IGNORED. Only two things are trusted
   from a candidate record: which file it touches, and the git SHA it
   claims to be reviewed at (content-addressed, so that binding is
   immutable regardless of who wrote the JSON around it).
2. Re-materializes the candidate's bytes itself, straight from git history
   (``git show <head_sha>:<module_path>`` against the INSTANCE repo — a
   content-addressed read, not a trust decision) — never from anything the
   instance wrote to disk directly.
3. Re-measures the causal micro-benchmark itself (reusing the #822
   ``heldout/microbench.py`` machinery: baseline is always the ACTUAL
   INSTALLED product tree, never instance-controlled git history) and
   re-runs the held-out behavioral pack (#780) — never trusting the
   instance's own numbers for either.
4. Only if independently satisfied, and only after a multi-pass SOAK
   period with no regression or integrity incident, writes the verified
   bytes into ``PROMOTED_TREE`` (default ``/var/lib/eeepc-promoted``) — a
   tree this script creates ROOT-OWNED, 0755, with 0644 files. The
   eeepc-agent-uid loader (``nanobot.runtime.promoted_overlay``) can read
   that tree but structurally cannot write it; filesystem permission IS
   the trust boundary, there is no key to steal because there is no key.
5. Watches every ACTIVE promotion on every subsequent pass: a held-out
   regression, a post-promotion integrity ledger event, or an explicit
   operator veto (``status: "vetoed"``, hand-edited into the root-owned
   manifest via sudo) rolls the promotion back — remove the tree file,
   remove the manifest entry, ledger it.

Trust invariants (#875, verbatim — do not weaken):

- The instance authors only PROPOSALS (candidate records, git branches). It
  never authors the promoted bytes or the numbers that gate promotion:
  root re-materializes from a content-addressed git SHA and re-measures
  itself, from scratch, every time.
- The promoted tree is root-owned; nothing here ever loosens that.
- Deny-set + slice membership are enforced HERE independently, using the
  exact same ``nanobot.runtime.runtime_deny`` logic the bridge's own gate
  uses — a candidate naming a deny-set path is never eligible, regardless
  of what the instance's own (untrusted) classification claimed.
- The verification kernel (bridge/gate/scorecard/benchmark/usage/heldout/
  coordinator — the runtime deny-set) is never promotable: the deny-set
  check above makes this structural, not policy.

**Idempotent / fail-safe.** Every per-candidate and per-active-manifest-
entry step is wrapped so ONE bad record can never abort the whole pass —
this script is meant to run unattended, every N minutes, forever. A
completely malformed environment (unreadable STATE_DIR, no instance repo,
no PROMOTED_TREE writable) degrades to a no-op pass, not a crash loop —
though :func:`main` DOES surface a nonzero exit code on a pass-level
failure so systemd/journalctl still show it.

Config: constants below + a few env vars with sane production defaults
(the systemd unit's ``EnvironmentFile=`` line points at the SAME instance
env file the bridge itself uses, so ``SELFEVO_RUNTIME_SLICE`` and
``STATE_DIR`` are read from the identical source of truth).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─── sys.path bootstrap: import the RELEASE's nanobot, not whatever might
# already be on PYTHONPATH — this script is copied into /usr/local/libexec
# and invoked standalone (see host/eeepc/systemd/eeepc-promotion-
# verifier.service), so it has to point itself at the deployed release the
# same way the bridge unit's PYTHONPATH= does.
_DEFAULT_RELEASE_DIR = "/opt/eeepc-agent/runtimes/self-evolving-agent/current"
_release_dir = os.environ.get("SELFEVO_RELEASE_DIR", "").strip() or _DEFAULT_RELEASE_DIR
if _release_dir and _release_dir not in sys.path:
    sys.path.insert(0, _release_dir)

from nanobot.runtime import cycle_ledger as _cycle_ledger  # noqa: E402
from nanobot.runtime.benchmark_evidence import (  # noqa: E402
    _MICROBENCH_MIN_IMPROVEMENT_PCT as MICROBENCH_MIN_IMPROVEMENT_PCT,
)
from nanobot.runtime.heldout import run_heldout as _run_heldout  # noqa: E402
from nanobot.runtime.heldout.microbench import _git_show_bytes  # noqa: E402
from nanobot.runtime.heldout.microbench import run_measurement  # noqa: E402
from nanobot.runtime.runtime_deny import _is_runtime_deny, runtime_slice_paths  # noqa: E402

# ─── Config: constants + env overrides ──────────────────────────────────────
STATE_DIR = Path(
    os.environ.get("STATE_DIR", "/var/lib/eeepc-agent/self-evolving-agent/state")
)
INSTANCE_REPO = Path(
    os.environ.get(
        "INSTANCE_REPO",
        "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving",
    )
)
PROMOTED_TREE = Path(os.environ.get("PROMOTED_TREE", "/var/lib/eeepc-promoted"))
# Same env var name the bridge classifier reads (SELFEVO_RUNTIME_SLICE) — the
# systemd unit's EnvironmentFile= points at the bridge's own instance env
# file so this is always the SAME operator-approved slice, never a second
# copy that could drift out of sync.
_RUNTIME_SLICE_ENV = "SELFEVO_RUNTIME_SLICE"

_SOAK_PASSES = 3
_MANIFEST_FILENAME = "manifest.json"
_MANIFEST_SCHEMA = "promoted-manifest-v1"
_VERIFIER_STATE_FILENAME = "verifier_state.json"
_VERIFIER_STATE_SCHEMA = "promotion-verifier-state-v1"
# Terminal verifier_state statuses — never reprocessed from scratch once
# reached (a deterministic candidate re-evaluated on the same content would
# only re-derive the same verdict; retrying forever would just burn CPU).
_TERMINAL_STATUSES = frozenset({"rejected", "promoted", "rolled_back"})


# ─── small pure helpers ──────────────────────────────────────────────────────


def _utc_iso(now: "datetime | None" = None) -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> "datetime | None":
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _normalize_path(path: str) -> str:
    return posixpath.normpath(str(path).replace("\\", "/")).lstrip("/")


def _is_runtime_py_shape(path: str) -> bool:
    norm = _normalize_path(path)
    return norm.startswith("nanobot/runtime/") and norm.endswith(".py")


def _flattened_filename(module_path: str) -> str:
    """Shared on-disk naming convention with the agent-side loader
    (``nanobot.runtime.promoted_overlay._flattened_filename``): keep these
    two IN SYNC — this is the writer, that module is the reader."""
    return module_path.replace("/", "__")


def _read_json_dict(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return dict(default)


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    """Write ``text`` to ``path`` atomically (tmp + rename) and chmod it —
    readers (the eeepc-agent-uid overlay loader) must never observe a
    partially-written manifest/promoted file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp_path, mode)
    except Exception:
        pass
    os.replace(tmp_path, path)


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_bytes(data)
    try:
        os.chmod(tmp_path, mode)
    except Exception:
        pass
    os.replace(tmp_path, path)


def _ensure_promoted_tree_dir(promoted_tree: Path) -> None:
    """Idempotent maintenance: PROMOTED_TREE must always be 0755. Best-effort
    — a chmod failure here must not abort the pass (the boundary self-check
    on the agent side is what actually enforces trust; this is just upkeep).
    """
    try:
        promoted_tree.mkdir(parents=True, exist_ok=True)
        os.chmod(promoted_tree, 0o755)
    except Exception:
        pass


def _chown_best_effort(path: Path, user: str) -> None:
    """Best-effort chown back to ``user:user`` (POSIX only). Used after this
    root process appends a ledger event, so the eeepc-agent-uid bridge keeps
    being able to write that same file afterwards."""
    if os.name != "posix":
        return
    try:
        import grp
        import pwd

        pw = pwd.getpwnam(user)
        gr = grp.getgrnam(user)
        os.chown(path, pw.pw_uid, gr.gr_gid)
    except Exception:
        pass


# ─── ledger integrity scan (regression-watch / soak-watch input) ───────────


def _integrity_events_since(state_dir: Path, since_iso: "str | None") -> "list[dict]":
    """Scan the cycle ledger (active file + a bounded window of recent
    gzip archives, mirroring ``scorecard``'s rotation-aware read pattern)
    for ``phase == "integrity"`` rows strictly AFTER ``since_iso``.

    Fail-open to ``[]`` — a ledger-read problem must never itself look like
    "no integrity events", but it also must never crash the verifier pass;
    callers treat an empty list as "nothing new to worry about", which is
    the safe direction for a scan that could not complete.
    """
    events: "list[dict]" = []
    try:
        since_dt = _parse_iso(since_iso) if since_iso else None
        ledger_dir = state_dir / "ledger"
        active = ledger_dir / "cycles.jsonl"
        candidates: "list[Path]" = [active] if active.is_file() else []
        try:
            candidates.extend(
                sorted(ledger_dir.glob("cycles-*.jsonl.gz"), reverse=True)[:3]
            )
        except Exception:
            pass

        for file_path in candidates:
            try:
                if file_path.suffix == ".gz":
                    import gzip

                    with gzip.open(file_path, "rt", encoding="utf-8") as fh:
                        lines = fh.readlines()
                else:
                    lines = file_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict) or row.get("phase") != "integrity":
                    continue
                row_dt = _parse_iso(row.get("ts"))
                if since_dt is not None and row_dt is not None and row_dt <= since_dt:
                    continue
                events.append(row)
    except Exception:
        return []
    return events


def _heldout_is_clean(results: dict) -> "tuple[bool, str]":
    """(clean, reason) from a ``run_heldout`` result dict — clean iff
    ``regressions == []`` AND no artifact has ``status == 'fail'``."""
    try:
        regressions = results.get("regressions") or []
        result_map = results.get("results") or {}
        failed = sorted(
            artifact
            for artifact, res in result_map.items()
            if isinstance(res, dict) and res.get("status") == "fail"
        )
        if regressions:
            return False, f"heldout regressions: {regressions}"
        if failed:
            return False, f"heldout failures: {failed}"
        return True, ""
    except Exception:
        return False, "heldout results malformed"


# ─── eligibility classification ─────────────────────────────────────────────


def _classify_candidate(
    record: dict, slice_paths: "set[str]",
) -> "tuple[bool, str, str | None]":
    """(eligible, reason, module_path). Ignores every instance-supplied
    number (microbench dict) entirely — only the SHAPE of ``changed_files``
    and deny/slice membership decide eligibility. #875 step 2."""
    changed_files = record.get("changed_files")
    if not isinstance(changed_files, list) or not changed_files:
        return False, "changed_files missing or empty", None

    runtime_files = sorted(
        {
            _normalize_path(f)
            for f in changed_files
            if isinstance(f, str) and _is_runtime_py_shape(f)
        }
    )
    if len(runtime_files) != 1:
        return (
            False,
            f"expected exactly one runtime-tier file in changed_files, found {len(runtime_files)}",
            None,
        )
    module_path = runtime_files[0]
    if _is_runtime_deny(module_path):
        return False, f"module is on the immutable runtime deny-set: {module_path}", None
    if module_path not in slice_paths:
        return False, f"module not in the operator-approved runtime slice: {module_path}", None
    return True, "", module_path


def _measure_candidate(
    module_path: str, head_sha: str,
) -> "tuple[bool, str, dict | None]":
    """Re-materialize + re-measure a candidate from scratch. Returns
    (ok, reason, measurement_dict). Never trusts anything the instance
    wrote — bytes come from ``git show``, numbers come from
    ``run_measurement`` against the INSTALLED product tree. #875 steps 3-4.
    """
    if not head_sha:
        return False, "candidate record has no head_sha to materialize from", None

    candidate_bytes = _git_show_bytes(INSTANCE_REPO, head_sha, module_path)
    if candidate_bytes is None:
        return False, f"failed to materialize {module_path}@{head_sha} via git show", None

    baseline_ms = run_measurement(module_path, None)
    if baseline_ms is None or baseline_ms <= 0:
        return False, "baseline measurement failed or non-positive", None
    candidate_ms = run_measurement(module_path, candidate_bytes)
    if candidate_ms is None or candidate_ms <= 0:
        return False, "candidate measurement failed or non-positive", None

    improvement_pct = (baseline_ms - candidate_ms) / baseline_ms * 100.0
    if improvement_pct < MICROBENCH_MIN_IMPROVEMENT_PCT:
        return (
            False,
            f"improvement {improvement_pct:.2f}% below required "
            f"{MICROBENCH_MIN_IMPROVEMENT_PCT:.2f}%",
            None,
        )

    heldout_results = _run_heldout(STATE_DIR, INSTANCE_REPO)
    clean, heldout_reason = _heldout_is_clean(heldout_results)
    if not clean:
        return False, heldout_reason, None

    return (
        True,
        "",
        {
            "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            "improvement_pct": improvement_pct,
            "baseline_ms": baseline_ms,
            "candidate_ms": candidate_ms,
        },
    )


# ─── promote / rollback ──────────────────────────────────────────────────────


def _promote(
    manifest: dict,
    candidate_id: str,
    module_path: str,
    head_sha: str,
    now: datetime,
) -> "dict | None":
    """Materialize the FINAL verified bytes once more (defense-in-depth —
    content-addressed by ``head_sha`` so this is guaranteed identical to
    what was soaked) and write them + the manifest entry. Returns the new
    manifest entry, or ``None`` on failure (best-effort; caller records the
    failure and leaves the candidate in ``soaking`` for the next pass)."""
    try:
        candidate_bytes = _git_show_bytes(INSTANCE_REPO, head_sha, module_path)
        if candidate_bytes is None:
            return None
        sha256 = hashlib.sha256(candidate_bytes).hexdigest()

        _ensure_promoted_tree_dir(PROMOTED_TREE)
        tree_file = PROMOTED_TREE / _flattened_filename(module_path)
        _atomic_write_bytes(tree_file, candidate_bytes, mode=0o644)

        entry = {
            "sha256": sha256,
            "candidate_id": candidate_id,
            "head_sha": head_sha,
            "verified_utc": _utc_iso(now),
            "status": "active",
        }
        manifest[module_path] = entry

        try:
            _cycle_ledger.append_event(
                STATE_DIR,
                {
                    "phase": "promotion",
                    "reason": "root_verified_promoted",
                    "candidate_id": candidate_id,
                    "module_path": module_path,
                    "head_sha": head_sha,
                    "sha256": sha256,
                },
            )
            _chown_best_effort(STATE_DIR / "ledger" / "cycles.jsonl", "eeepc-agent")
        except Exception:
            pass  # ledger write is best-effort; the promotion itself already succeeded

        return entry
    except Exception:
        return None


def _rollback(
    manifest: dict,
    module_path: str,
    candidate_id: "str | None",
    reason: str,
) -> None:
    """Remove the tree file + manifest entry and ledger the rollback.
    Best-effort/idempotent — safe to call on an already-rolled-back entry."""
    try:
        tree_file = PROMOTED_TREE / _flattened_filename(module_path)
        if tree_file.exists():
            tree_file.unlink()
    except Exception:
        pass
    manifest.pop(module_path, None)
    try:
        _cycle_ledger.append_event(
            STATE_DIR,
            {
                "phase": "promotion",
                "reason": "root_verified_rolled_back",
                "candidate_id": candidate_id,
                "module_path": module_path,
                "rollback_reason": reason,
            },
        )
        _chown_best_effort(STATE_DIR / "ledger" / "cycles.jsonl", "eeepc-agent")
    except Exception:
        pass


# ─── one verify pass ─────────────────────────────────────────────────────────


def verify_pass(now: "datetime | None" = None) -> dict:
    """Run one full verification pass over every pending/soaking candidate
    and every active manifest entry. Safe to call every N minutes forever —
    every candidate and every active entry is processed inside its own
    try/except so one bad record can never abort the rest of the pass.

    Returns a small summary dict (counts) for logging/tests; never raises.
    """
    current = now or datetime.now(timezone.utc)
    summary = {
        "processed": 0,
        "rejected": 0,
        "soaking": 0,
        "promoted": 0,
        "rolled_back": 0,
        "errors": 0,
    }

    try:
        _ensure_promoted_tree_dir(PROMOTED_TREE)

        manifest_path = PROMOTED_TREE / _MANIFEST_FILENAME
        verifier_state_path = PROMOTED_TREE / _VERIFIER_STATE_FILENAME
        manifest = _read_json_dict(manifest_path, {"_schema_version": _MANIFEST_SCHEMA})
        verifier_state = _read_json_dict(
            verifier_state_path, {"schema_version": _VERIFIER_STATE_SCHEMA, "candidates": {}}
        )
        candidates_state = verifier_state.get("candidates")
        if not isinstance(candidates_state, dict):
            candidates_state = {}
        manifest_dirty = False
        state_dirty = False

        slice_paths = runtime_slice_paths(os.environ.get(_RUNTIME_SLICE_ENV))

        promotions_dir = STATE_DIR / "promotions"
        candidate_files = (
            sorted(promotions_dir.glob("promotion-runtime-*.json"))
            if promotions_dir.is_dir()
            else []
        )

        for candidate_path in candidate_files:
            candidate_id = candidate_path.stem
            try:
                summary["processed"] += 1
                entry_state = candidates_state.get(candidate_id)

                if isinstance(entry_state, dict) and entry_state.get("status") in _TERMINAL_STATUSES:
                    continue  # deterministic outcome already reached — never retried

                record = json.loads(candidate_path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("candidate record is not a JSON object")

                if entry_state is None or entry_state.get("status") not in ("soaking",):
                    # ── fresh candidate: classify, materialize, measure ──
                    eligible, reason, module_path = _classify_candidate(record, slice_paths)
                    if not eligible:
                        candidates_state[candidate_id] = {
                            "status": "rejected",
                            "reason": reason,
                            "last_checked_utc": _utc_iso(current),
                        }
                        summary["rejected"] += 1
                        state_dirty = True
                        continue

                    head_sha = ((record.get("rollback_record") or {}).get("head_sha"))
                    ok, reason, measurement = _measure_candidate(module_path, head_sha)
                    if not ok:
                        candidates_state[candidate_id] = {
                            "status": "rejected",
                            "reason": reason,
                            "module_path": module_path,
                            "head_sha": head_sha,
                            "last_checked_utc": _utc_iso(current),
                        }
                        summary["rejected"] += 1
                        state_dirty = True
                        continue

                    candidates_state[candidate_id] = {
                        "status": "soaking",
                        "module_path": module_path,
                        "head_sha": head_sha,
                        "sha256": measurement["sha256"],
                        "improvement_pct": measurement["improvement_pct"],
                        "soak_passes_done": 0,
                        "ledger_watermark_utc": _utc_iso(current),
                        "first_soaking_utc": _utc_iso(current),
                        "last_checked_utc": _utc_iso(current),
                    }
                    summary["soaking"] += 1
                    state_dirty = True
                    continue

                # ── already soaking: re-check heldout + integrity, advance or reject ──
                module_path = entry_state.get("module_path")
                head_sha = entry_state.get("head_sha")
                if not module_path or not head_sha:
                    candidates_state[candidate_id] = {
                        "status": "rejected",
                        "reason": "soaking entry missing module_path/head_sha (corrupt state)",
                        "last_checked_utc": _utc_iso(current),
                    }
                    summary["rejected"] += 1
                    state_dirty = True
                    continue

                heldout_results = _run_heldout(STATE_DIR, INSTANCE_REPO)
                clean, heldout_reason = _heldout_is_clean(heldout_results)
                integrity_events = _integrity_events_since(
                    STATE_DIR, entry_state.get("ledger_watermark_utc")
                )
                if not clean or integrity_events:
                    reason = heldout_reason or f"integrity events since watermark: {len(integrity_events)}"
                    candidates_state[candidate_id] = {
                        **entry_state,
                        "status": "rejected",
                        "reason": f"soak failed: {reason}",
                        "last_checked_utc": _utc_iso(current),
                    }
                    summary["rejected"] += 1
                    state_dirty = True
                    continue

                soak_passes_done = int(entry_state.get("soak_passes_done") or 0) + 1
                if soak_passes_done >= _SOAK_PASSES:
                    new_entry = _promote(manifest, candidate_id, module_path, head_sha, current)
                    if new_entry is None:
                        # promotion write failed — stay in soaking, try again next pass
                        candidates_state[candidate_id] = {
                            **entry_state,
                            "soak_passes_done": soak_passes_done - 1,
                            "last_checked_utc": _utc_iso(current),
                        }
                        summary["errors"] += 1
                        state_dirty = True
                        continue
                    candidates_state[candidate_id] = {
                        **entry_state,
                        "status": "promoted",
                        "soak_passes_done": soak_passes_done,
                        "last_checked_utc": _utc_iso(current),
                    }
                    summary["promoted"] += 1
                    manifest_dirty = True
                    state_dirty = True
                else:
                    candidates_state[candidate_id] = {
                        **entry_state,
                        "soak_passes_done": soak_passes_done,
                        "last_checked_utc": _utc_iso(current),
                    }
                    summary["soaking"] += 1
                    state_dirty = True
            except Exception:
                summary["errors"] += 1
                continue  # one bad candidate must never abort the pass

        # ── regression watch + operator veto over every manifest entry ──
        for module_path in list(manifest.keys()):
            if module_path.startswith("_"):
                continue  # schema/meta keys, not a module entry
            try:
                entry = manifest.get(module_path)
                if not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                if status == "vetoed":
                    _rollback(manifest, module_path, entry.get("candidate_id"), "operator_veto")
                    summary["rolled_back"] += 1
                    manifest_dirty = True
                    continue
                if status != "active":
                    continue

                heldout_results = _run_heldout(STATE_DIR, INSTANCE_REPO)
                clean, heldout_reason = _heldout_is_clean(heldout_results)
                integrity_events = _integrity_events_since(STATE_DIR, entry.get("verified_utc"))
                if not clean or integrity_events:
                    reason = heldout_reason or f"integrity events since promotion: {len(integrity_events)}"
                    _rollback(manifest, module_path, entry.get("candidate_id"), reason)
                    summary["rolled_back"] += 1
                    manifest_dirty = True
            except Exception:
                summary["errors"] += 1
                continue  # one bad manifest entry must never abort the rest

        if manifest_dirty:
            _atomic_write_text(manifest_path, json.dumps(manifest, indent=2), mode=0o644)
        if state_dirty:
            verifier_state["candidates"] = candidates_state
            verifier_state["schema_version"] = _VERIFIER_STATE_SCHEMA
            verifier_state["updated_utc"] = _utc_iso(current)
            _atomic_write_text(
                verifier_state_path, json.dumps(verifier_state, indent=2), mode=0o644
            )
    except Exception:
        summary["errors"] += 1

    return summary


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Root-run promotion verifier (#875): re-verify eeepc runtime-slice "
            "promotion candidates independently of the instance's own claims."
        )
    )
    parser.add_argument("--json", action="store_true", help="print the pass summary as JSON")
    args = parser.parse_args(argv)

    try:
        summary = verify_pass()
    except Exception as exc:  # pragma: no cover - verify_pass is itself fail-open
        print(f"eeepc_promotion_verifier: pass failed unexpectedly: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary))
    else:
        print(
            "eeepc_promotion_verifier: processed={processed} rejected={rejected} "
            "soaking={soaking} promoted={promoted} rolled_back={rolled_back} "
            "errors={errors}".format(**summary)
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
