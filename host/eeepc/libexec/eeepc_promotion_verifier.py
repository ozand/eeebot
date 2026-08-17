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

**Rework after #880 (opus-review RED1/RED2 — see
docs/changes/875-root-verified-auto-promotion/proposal.md for the full
write-up).** #880 made the release tree read-only to the runtime uid via
the SANDBOX (``ProtectSystem=strict``), but left the on-disk owner as
``eeepc-agent`` — meaning this script, importing straight from that
release as root, was importing runtime-uid-owned code as root (RED1: root
RCE via a planted/mutated module). And three pieces of work here executed
instance-influenced bytes AS ROOT with no privilege drop at all (RED2):
``git show`` against the instance repo, the microbench measurement, and
the held-out pack. Both are fixed now:

- RED1: ``deploy_release.sh`` chowns the release ``root:root`` (the runtime
  uid only ever needed read+exec — #880 already proved no runtime write
  into ``/opt`` exists). This script independently verifies that promise
  before importing anything from the release —
  :func:`_verify_release_ownership_or_exit` fails closed (prints one
  reason, exits nonzero) if the release dir / its ``nanobot/`` / its
  ``nanobot/runtime/`` are not root-owned and non-group/other-writable.
- RED2: nothing here EXECUTES instance-influenced bytes as root anymore.
  ``git show`` runs demoted (:func:`_git_show_bytes_demoted`); the
  measurement + held-out pack run inside a demoted CHILD re-invocation of
  this same script (``--child-verify``, see :func:`_run_child_verify` /
  :func:`_child_verify_main`) that prints ONE JSON verdict to stdout. Root
  parses that JSON as untrusted DATA and makes the ONLY trust decision (the
  improvement_pct threshold + heldout-clean check) itself — the child
  never decides to promote anything.

Accepted residual risk: the measured candidate code necessarily EXECUTES
inside the demoted measurement sandbox (that's the whole point — root
can't measure it without running it somewhere). A sufficiently adversarial
candidate could compromise that sandboxed, unprivileged child process, but
it starts with no root access and no write access to ``PROMOTED_TREE``
(root-owned) — compromising the child can at most forge THAT child's own
verdict, never gain root or write the promoted tree directly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
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

# ─── RED1 fix (opus-review): fail-closed release-ownership check ───────────
# This process is about to `sys.path.insert` the release dir and import
# `nanobot` straight out of it, AS ROOT. `deploy_release.sh` now chowns the
# release `root:root` (#880 proved no runtime write into /opt exists;
# PYTHONDONTWRITEBYTECODE=1 everywhere means no runtime write happens even
# incidentally) — the runtime uid only ever needs read+exec, which
# world-read from the release tar/umask already provides. Before importing
# ANYTHING from the release, prove that promise held: the release dir
# itself, its `nanobot/` subdir, and `nanobot/runtime/` must each be
# `st_uid == 0` and neither group- nor other-writable. Any failure prints
# ONE reason to stderr and exits nonzero WITHOUT ever reaching the
# `from nanobot...` import lines below — fail closed, no partial import.
_OWNERSHIP_SKIP_ENV = "EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK"
_WRITABLE_BY_OTHERS_MASK = 0o022


def _stat_root_owned_and_not_writable(target: Path) -> None:
    """Shared leaf check for :func:`_verify_release_ownership_or_exit`:
    ``target`` must stat-able, ``st_uid == 0``, and neither group- nor
    other-writable. Prints one reason and ``sys.exit(1)``s otherwise."""
    try:
        st = target.stat()
    except OSError as exc:
        print(
            f"eeepc_promotion_verifier: refusing to import — cannot stat "
            f"release path {target}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    if st.st_uid != 0:
        print(
            f"eeepc_promotion_verifier: refusing to import — {target} is "
            f"not root-owned (uid={st.st_uid}); a root-run verifier must "
            f"never import code a non-root uid could have planted",
            file=sys.stderr,
        )
        sys.exit(1)
    if st.st_mode & _WRITABLE_BY_OTHERS_MASK:
        print(
            f"eeepc_promotion_verifier: refusing to import — {target} is "
            f"group/other writable (mode={oct(st.st_mode & 0o777)})",
            file=sys.stderr,
        )
        sys.exit(1)


def _verify_release_ownership_or_exit(release_dir: str) -> Path:
    """Refuse to let this process import a release tree a non-root uid
    could have planted or modified. Skippable ONLY via
    ``EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK=1`` — documented test-only escape
    hatch, because tests run as a non-root user against temp dirs that can
    never satisfy a real root-ownership check.

    Returns the RESOLVED (realpath) release dir — callers MUST use this
    return value for ``sys.path`` (see the module-level call site just
    below), never the raw ``release_dir`` string again. This closes a
    check-then-use gap (opus-review round 2, YELLOW-1): stat-checking the
    unresolved ``current`` symlink and then separately re-resolving it for
    ``sys.path.insert`` leaves a window where the symlink could be flipped
    between the two; resolving ONCE here and using that same concrete path
    for both the check and the import removes the second, independent
    resolution entirely.
    """
    if os.environ.get(_OWNERSHIP_SKIP_ENV) == "1":
        return Path(release_dir).resolve()
    if os.name != "posix":
        print(
            "eeepc_promotion_verifier: refusing to run — the release-"
            "ownership check is POSIX-only (this feature is host-only); "
            f"set {_OWNERSHIP_SKIP_ENV}=1 only for tests/dev",
            file=sys.stderr,
        )
        sys.exit(1)

    root = Path(release_dir).resolve()
    for rel in ("", "nanobot", "nanobot/runtime"):
        _stat_root_owned_and_not_writable((root / rel) if rel else root)

    # Optional extra (opus-review round 2, YELLOW-1): the interpreter this
    # process itself is running under should also be root-owned — in
    # production this is the release's own venv (already covered by
    # deploy_release.sh's root:root chown of VENV_BASE), so this is
    # defense-in-depth against a replaced/symlinked interpreter binary, not
    # a new trust boundary on its own.
    try:
        _stat_root_owned_and_not_writable(Path(sys.executable).resolve())
    except SystemExit:
        raise
    except Exception:
        pass  # sys.executable resolution is best-effort; never block on it

    return root


_release_dir_resolved = _verify_release_ownership_or_exit(_release_dir)

if str(_release_dir_resolved) not in sys.path:
    sys.path.insert(0, str(_release_dir_resolved))

from nanobot.runtime.benchmark_evidence import (  # noqa: E402
    _MICROBENCH_MIN_IMPROVEMENT_PCT as MICROBENCH_MIN_IMPROVEMENT_PCT,
)
from nanobot.runtime.heldout import run_heldout as _run_heldout  # noqa: E402
from nanobot.runtime.heldout.microbench import _git_cmd as _microbench_git_cmd  # noqa: E402
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

# ─── RED2 fix (opus-review): the demoted-user this root process drops
# privilege to before ever EXECUTING instance-influenced code (git against
# the instance repo, the microbench measurement, the held-out pack). Root
# only ever receives the child's stdout as DATA (a JSON verdict + numbers);
# the promotion decision itself always stays in this root parent (see
# _measure_candidate / _run_child_verify).
_DEMOTE_USER = os.environ.get("EEEPC_VERIFIER_DEMOTE_USER", "").strip() or "eeepc-agent"
_CHILD_TIMEOUT_SECONDS = 600  # hard cap: baseline + candidate measurement + heldout

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


def _reassert_promoted_tree_root_ownership(promoted_tree: Path) -> bool:
    """RED1 fix (opus-review round 2): re-check PROMOTED_TREE's root
    ownership at the START of every pass, not just rely on it having been
    created root-owned once. ``_ensure_promoted_tree_dir`` never re-asserts
    ``st_uid`` — a one-time drift (e.g. the very symlink-chown attack this
    same rework closes elsewhere) would otherwise persist forever, silently
    downgrading the agent-side loader's boundary self-check from "trusted"
    to "would refuse everything" without anyone noticing why.

    Returns ``True`` if the tree is (now) root-owned and safe to use this
    pass. Returns ``False`` if it drifted AND could not be healed — the
    caller must fail the WHOLE pass closed rather than trust a possibly
    instance-writable tree; this prints a loud stderr line (systemd/
    journalctl visible) since writing that failure into the tree's own
    ledger would mean trusting the very tree that just failed its check.

    Skippable via the same ``EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK=1`` escape
    hatch as the release check (test-only: tests create PROMOTED_TREE as
    the current non-root user, which can never satisfy this for real).
    """
    if os.environ.get(_OWNERSHIP_SKIP_ENV) == "1":
        return True
    if os.name != "posix":
        return True  # ownership semantics are POSIX-only; nothing to check here
    try:
        if not promoted_tree.is_dir():
            return True  # not created yet — _ensure_promoted_tree_dir makes it fresh, root-owned
        st = promoted_tree.stat()
        if st.st_uid == 0:
            return True
    except Exception:
        print(
            "eeepc_promotion_verifier: could not stat PROMOTED_TREE to verify "
            "root ownership — refusing to process this pass (fail closed)",
            file=sys.stderr,
        )
        return False

    # Drifted away from root. Running as root we CAN heal it directly; if
    # this process is not actually root (shouldn't happen in production —
    # the systemd unit has no User=), chown itself will fail and we must
    # fail this pass closed rather than guess.
    try:
        os.chown(promoted_tree, 0, 0)
        return True
    except Exception:
        print(
            "eeepc_promotion_verifier: PROMOTED_TREE is not root-owned "
            f"(uid={st.st_uid}) and could not be chowned back to root — "
            "refusing to process this pass (fail closed)",
            file=sys.stderr,
        )
        return False


_VERIFIER_LEDGER_FILENAME = "verifier_ledger.jsonl"


def _append_verifier_ledger_event(event: dict) -> None:
    """Append one JSON line to a ROOT-OWNED ledger under ``PROMOTED_TREE``
    — NEVER ``STATE_DIR/ledger`` (RED1 fix, opus-review round 2).

    The instance owns ``state/`` and can plant
    ``state/ledger/cycles.jsonl`` as a symlink to ANYWHERE, e.g.
    ``PROMOTED_TREE`` itself — a root ``open(path, "a")`` (as the old
    ``nanobot.runtime.cycle_ledger.append_event`` call used here did) or a
    root ``os.chown`` on that same path (as the old ``_chown_best_effort``
    call used here did) would follow the symlink and durably corrupt/chown
    whatever it points at, INCLUDING ``PROMOTED_TREE`` — breaking the
    "promoted tree is root-owned" invariant this whole design rests on.

    The fix is structural, not a symlink-follow guard bolted onto the old
    call sites: root's own audit trail now lives INSIDE ``PROMOTED_TREE``,
    which is root-owned 0755 (non-writable by any other uid) — nothing but
    root can ever create a file OR a symlink in that directory in the
    first place, so there is no attacker-controlled path for this write to
    ever follow. ``os.O_NOFOLLOW`` (POSIX; silently unavailable on
    Windows) is still added as cheap defense-in-depth for the file itself.
    Best-effort — a ledger write must never abort a verifier pass.
    """
    try:
        record = dict(event)
        record.setdefault("ts", _utc_iso())
        line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")

        ledger_path = PROMOTED_TREE / _VERIFIER_LEDGER_FILENAME
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(ledger_path, flags, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
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


# ─── RED2 fix (opus-review): privilege drop for instance-influenced work ───
#
# Root must never EXECUTE anything the instance influenced — only ever
# receive bytes/verdicts FROM a demoted child as inert data. Three things
# used to run as root and no longer do:
#   1. ``git show`` against INSTANCE_REPO (git parses repo-local config /
#      attributes / hooks — running it as root against an instance-owned
#      repo is itself a privilege-escalation surface, independent of what
#      the blob content is used for afterward).
#   2. the microbench measurement (``run_measurement`` executes the
#      candidate's own bytes).
#   3. the held-out pack (``run_heldout`` copies + executes instance
#      artifacts in a sandboxed subprocess).
#
# All three now happen either (a) inside a demoted ``git`` subprocess, or
# (b) inside a demoted CHILD re-invocation of this same interpreter/script
# (``--child-verify``) that does the sys.path bootstrap + imports + the
# actual measurement/heldout calls itself and prints ONE JSON object to
# stdout. The root PARENT parses that JSON as untrusted data and makes the
# ONLY trust decision (the improvement_pct threshold + heldout-clean check)
# itself — the child never decides whether to promote anything.


def _is_root() -> bool:
    """POSIX-only by construction (``os.geteuid`` does not exist on
    Windows) — always False on a dev box, which is exactly the "skip
    demotion" branch every caller below already handles explicitly."""
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0


def _resolve_demote_ids(user: str) -> "tuple[int, int] | None":
    """(uid, gid) for ``user``, or ``None`` if unresolvable or non-POSIX.
    Imports ``pwd`` lazily (module-level import would break Windows)."""
    if os.name != "posix":
        return None
    try:
        import pwd

        pw = pwd.getpwnam(user)
        return pw.pw_uid, pw.pw_gid
    except Exception:
        return None


def _demote_preexec_fn(uid: int, gid: int):
    """Build a ``preexec_fn`` for ``subprocess.run`` that drops this
    process's child to ``uid``/``gid`` — group list, gid, then uid, in that
    exact order (uid must be dropped last, or the process would lose the
    permission needed to change gid/groups).

    ``os.setgroups``/``os.setgid``/``os.setuid`` are POSIX-only (absent
    from typeshed's Windows view of ``os``) — the ``# type: ignore``
    comments match this repo's existing convention for POSIX-only stdlib
    access (see ``bridge.py``'s ``fcntl`` import). Callers only ever build
    (via :func:`_resolve_demote_ids`) or invoke (via :func:`_demote_kwargs`)
    this on a path already gated by ``os.name == "posix"``.
    """

    def _demote() -> None:
        os.setgroups([gid])  # type: ignore[attr-defined]
        os.setgid(gid)  # type: ignore[attr-defined]
        os.setuid(uid)  # type: ignore[attr-defined]

    return _demote


def _demote_kwargs() -> "tuple[dict, bool]":
    """(subprocess kwargs, demoted). When this process itself is root,
    returns ``preexec_fn`` kwargs that drop the child to
    :data:`_DEMOTE_USER`. When not root (tests, dev boxes, Windows), skips
    demotion entirely with an explicit log line — there is no privilege to
    drop, and forcing a demotion attempt here would just fail on a dev
    machine that has no ``eeepc-agent`` system user."""
    if not _is_root():
        print(
            "eeepc_promotion_verifier: not running as root — skipping "
            f"privilege drop to {_DEMOTE_USER!r} (expected under tests/dev; "
            "the production systemd unit always runs this as root)",
            file=sys.stderr,
        )
        return {}, False
    ids = _resolve_demote_ids(_DEMOTE_USER)
    if ids is None:
        raise RuntimeError(
            f"eeepc_promotion_verifier: cannot resolve demote user {_DEMOTE_USER!r} "
            "(getpwnam failed) — refusing to run instance-influenced work as root"
        )
    return {"preexec_fn": _demote_preexec_fn(*ids)}, True


def _git_show_bytes_demoted(
    repo_root: Path, ref: str, module_path: str, *, timeout: int = 30,
) -> "bytes | None":
    """Same contract as ``microbench._git_show_bytes`` — but root must
    never itself run ``git`` against the instance-owned repo (git parses
    repo-local config/attributes/hooks). Demotes when running as root;
    falls back to no privilege drop when not (tests/dev). Returns the blob
    bytes as inert DATA — never executes them."""
    kwargs, _ = _demote_kwargs()
    try:
        proc = subprocess.run(
            _microbench_git_cmd(Path(repo_root)) + ["show", f"{ref}:{module_path}"],
            capture_output=True, timeout=timeout, **kwargs,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception:
        return None


def _minimal_child_env(home: Path) -> "dict[str, str]":
    """The stripped env for the ``--child-verify`` subprocess: PATH, HOME
    (a root-created tmpdir chowned to the demoted uid — see
    :func:`_run_child_verify`), the same STATE_DIR/INSTANCE_REPO this
    process resolved, ``SELFEVO_RELEASE_DIR`` so the child's own sys.path
    bootstrap finds the same release, and ``PYTHONDONTWRITEBYTECODE=1`` so
    the child never writes a ``.pyc`` anywhere. No secrets, no inherited
    process env beyond exactly these keys.

    ``SELFEVO_RELEASE_DIR`` is the ALREADY-RESOLVED realpath
    (``_release_dir_resolved``), not the raw (possibly symlink) env value —
    same YELLOW-1 reasoning as the parent's own ``sys.path`` pin: the child
    re-runs the identical ownership-check + bootstrap, and must check/use
    the exact same concrete path the parent already verified, not
    independently re-resolve a mutable symlink of its own.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "STATE_DIR": str(STATE_DIR),
        "INSTANCE_REPO": str(INSTANCE_REPO),
        "SELFEVO_RELEASE_DIR": str(_release_dir_resolved),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.name == "nt":
        # Dev-box-only accommodation (mirrors microbench._sandbox_env):
        # Windows' CreateProcess needs a real SYSTEMROOT to resolve system
        # DLLs when launching the interpreter — the actual production host
        # (eeepc, POSIX) never takes this branch.
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT") or os.environ.get("windir", "C:\\Windows")
        env["USERPROFILE"] = str(home)
        env["TMP"] = str(home)
        env["TEMP"] = str(home)
    # Test-only escape hatch, propagated so the CHILD's own ownership check
    # (it re-executes this whole script from scratch) doesn't fail against
    # a test's non-root-owned temp release dir. Never set in production —
    # the production parent never has this env var either.
    skip_flag = os.environ.get(_OWNERSHIP_SKIP_ENV)
    if skip_flag:
        env[_OWNERSHIP_SKIP_ENV] = skip_flag
    return env


def _run_child_verify(
    mode: str, *, module_path: "str | None" = None, head_sha: "str | None" = None,
    timeout: int = _CHILD_TIMEOUT_SECONDS,
) -> dict:
    """Re-invoke this script's own interpreter as a DEMOTED child process
    (``--child-verify --mode <mode>``) to do every piece of work that
    touches instance-influenced bytes or executes instance-authored code.
    Root itself never executes any of that — it only spawns this child,
    waits, and parses ONE JSON object from its stdout as untrusted DATA.

    ``mode="full"`` (requires ``module_path``/``head_sha``): materializes
    candidate bytes via ``git show``, measures baseline + candidate via
    ``run_measurement``, and runs the held-out pack — all inside the one
    child. ``mode="heldout"``: runs only the held-out pack (used by the
    soak-continuation and post-promotion regression-watch passes, which
    don't need a fresh measurement).

    Returns a dict always containing ``"ok"``. ``"ok": False`` covers both
    a well-formed child-reported failure (bad reason string included) AND
    a malformed child process outcome (nonzero exit / unparseable stdout /
    timeout) — callers must treat any of these as "could not verify this
    pass", never as a green light. The child's numbers are parsed here but
    the THRESHOLD/trust decision is made by the caller (``_measure_candidate``
    or the soak/regression-watch call sites in ``verify_pass``), never by
    this function and never by the child itself.
    """
    home = Path(tempfile.mkdtemp(prefix="eeepc-verifier-child-home-"))
    try:
        kwargs, demoted = _demote_kwargs()
        if demoted:
            ids = _resolve_demote_ids(_DEMOTE_USER)
            if ids is not None:
                try:
                    os.chown(home, ids[0], ids[1])
                except Exception:
                    pass

        argv = [
            sys.executable, str(Path(__file__).resolve()),
            "--child-verify", "--mode", mode,
        ]
        if module_path:
            argv += ["--module-path", module_path]
        if head_sha:
            argv += ["--head-sha", head_sha]

        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            env=_minimal_child_env(home),
            **kwargs,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"failed to spawn child verify process: {exc}"}
    finally:
        try:
            shutil.rmtree(home, ignore_errors=True)
        except Exception:
            pass

    if proc.returncode != 0:
        stderr_tail = proc.stderr.decode("utf-8", "replace")[-400:] if proc.stderr else ""
        return {
            "ok": False,
            "reason": f"child verify process exited {proc.returncode}: {stderr_tail}",
        }
    try:
        stdout_bytes = proc.stdout or b""
        result = json.loads(stdout_bytes.decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "reason": f"child verify stdout unparseable: {exc}"}
    if not isinstance(result, dict) or "ok" not in result:
        return {"ok": False, "reason": "child verify stdout malformed (not a dict with 'ok')"}
    return result


def _child_verify_main(
    mode: str, module_path: "str | None", head_sha: "str | None",
) -> int:
    """Entry point for the DEMOTED CHILD process (see
    :func:`_run_child_verify`). Does every piece of instance-influenced
    work — ``git show``, the microbench measurement, the held-out pack —
    and prints exactly ONE JSON object to stdout. Makes NO trust decision
    (no threshold comparison): the parent applies
    ``MICROBENCH_MIN_IMPROVEMENT_PCT`` itself. Always exits 0 with a
    well-formed ``{"ok": ...}`` JSON object unless something fails before
    this function can even run (import/argv errors) — those surface to the
    parent as a nonzero exit / no stdout, which it already treats as
    failure.
    """
    result: dict = {"ok": False, "reason": f"unknown child-verify mode {mode!r}"}
    try:
        if mode == "full":
            if not module_path or not head_sha:
                result = {
                    "ok": False,
                    "reason": "child-verify full mode requires --module-path and --head-sha",
                }
            else:
                candidate_bytes = _git_show_bytes(INSTANCE_REPO, head_sha, module_path)
                if candidate_bytes is None:
                    result = {
                        "ok": False,
                        "reason": f"failed to materialize {module_path}@{head_sha} via git show",
                    }
                else:
                    baseline_ms = run_measurement(module_path, None)
                    if baseline_ms is None or baseline_ms <= 0:
                        result = {"ok": False, "reason": "baseline measurement failed or non-positive"}
                    else:
                        candidate_ms = run_measurement(module_path, candidate_bytes)
                        if candidate_ms is None or candidate_ms <= 0:
                            result = {"ok": False, "reason": "candidate measurement failed or non-positive"}
                        else:
                            heldout_results = _run_heldout(STATE_DIR, INSTANCE_REPO)
                            clean, heldout_reason = _heldout_is_clean(heldout_results)
                            result = {
                                "ok": True,
                                "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                                "baseline_ms": baseline_ms,
                                "candidate_ms": candidate_ms,
                                "heldout_clean": clean,
                                "heldout_reason": heldout_reason,
                            }
        elif mode == "heldout":
            heldout_results = _run_heldout(STATE_DIR, INSTANCE_REPO)
            clean, heldout_reason = _heldout_is_clean(heldout_results)
            result = {"ok": True, "heldout_clean": clean, "heldout_reason": heldout_reason}
    except Exception as exc:
        result = {"ok": False, "reason": f"child-verify exception: {exc}"}
    print(json.dumps(result))
    return 0


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
    module_path: str, head_sha: "str | None",
) -> "tuple[bool, str, dict | None]":
    """Re-materialize + re-measure a candidate from scratch. Returns
    (ok, reason, measurement_dict). Never trusts anything the instance
    wrote, and — RED2 fix — never EXECUTES any of it itself either: the
    actual ``git show`` / measurement / heldout work happens inside a
    demoted child process (:func:`_run_child_verify`); this function only
    parses that child's numbers (untrusted data) and applies the ONE trust
    decision (the improvement_pct threshold + heldout-clean check) itself.
    #875 steps 3-4.

    ``head_sha`` is typed ``str | None`` (rather than ``str``) because the
    caller reads it straight out of an untrusted candidate record, where it
    may genuinely be absent — the check below is exactly that case, not
    just defensive padding.
    """
    if not head_sha:
        return False, "candidate record has no head_sha to materialize from", None

    child = _run_child_verify("full", module_path=module_path, head_sha=head_sha)
    if not child.get("ok"):
        return False, str(child.get("reason") or "child verify failed"), None

    baseline_ms = child.get("baseline_ms")
    candidate_ms = child.get("candidate_ms")
    if isinstance(baseline_ms, bool) or not isinstance(baseline_ms, (int, float)) or baseline_ms <= 0:
        return False, "child verify returned an invalid baseline_ms", None
    if isinstance(candidate_ms, bool) or not isinstance(candidate_ms, (int, float)) or candidate_ms <= 0:
        return False, "child verify returned an invalid candidate_ms", None

    improvement_pct = (baseline_ms - candidate_ms) / baseline_ms * 100.0
    if improvement_pct < MICROBENCH_MIN_IMPROVEMENT_PCT:
        return (
            False,
            f"improvement {improvement_pct:.2f}% below required "
            f"{MICROBENCH_MIN_IMPROVEMENT_PCT:.2f}%",
            None,
        )

    if not child.get("heldout_clean"):
        return False, str(child.get("heldout_reason") or "heldout not clean"), None

    sha256 = child.get("sha256")
    if not isinstance(sha256, str) or not sha256:
        return False, "child verify returned no sha256", None

    return (
        True,
        "",
        {
            "sha256": sha256,
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
    failure and leaves the candidate in ``soaking`` for the next pass).

    RED2 fix: the re-fetch runs via ``_git_show_bytes_demoted`` — root must
    never run ``git`` against the instance-owned repo itself, even for this
    read-only re-materialization; the demoted subprocess hands back the
    same bytes as inert data.
    """
    try:
        candidate_bytes = _git_show_bytes_demoted(INSTANCE_REPO, head_sha, module_path)
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

        # RED1 fix (opus-review round 2): root's own audit event goes into
        # the root-owned verifier ledger under PROMOTED_TREE — never
        # STATE_DIR/ledger (instance-writable; see _append_verifier_ledger_event's
        # docstring for the symlink attack this closes).
        _append_verifier_ledger_event({
            "phase": "promotion",
            "reason": "root_verified_promoted",
            "candidate_id": candidate_id,
            "module_path": module_path,
            "head_sha": head_sha,
            "sha256": sha256,
        })

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
    # RED1 fix (opus-review round 2): same root-owned ledger as _promote —
    # never STATE_DIR/ledger.
    _append_verifier_ledger_event({
        "phase": "promotion",
        "reason": "root_verified_rolled_back",
        "candidate_id": candidate_id,
        "module_path": module_path,
        "rollback_reason": reason,
    })


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
        # RED1 fix (opus-review round 2): re-assert PROMOTED_TREE's root
        # ownership BEFORE doing anything else this pass — a one-time
        # drift must never silently persist. Fails the WHOLE pass closed
        # (no candidates processed, no manifest/state read or written) if
        # the tree is not root-owned and could not be healed back.
        if not _reassert_promoted_tree_root_ownership(PROMOTED_TREE):
            summary["errors"] += 1
            return summary

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
                    assert module_path is not None  # eligible=True always pairs with a module_path (see _classify_candidate)

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

                # RED2 fix: heldout re-check runs in a demoted child (it
                # executes instance artifacts) — an integrity-scan is pure
                # ledger JSON reading, no execution, so it stays in this
                # root parent. A malformed/failed child result here is a
                # verifier-side error (not a soak verdict) — raise so the
                # existing per-candidate try/except records it as an
                # "errors" count and leaves the entry untouched for retry.
                child = _run_child_verify("heldout")
                if not child.get("ok"):
                    raise RuntimeError(
                        str(child.get("reason") or "heldout child verify failed")
                    )
                clean = bool(child.get("heldout_clean"))
                heldout_reason = str(child.get("heldout_reason") or "")
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
                    # RED1 fix (opus-review round 2): force verifier_state.json
                    # to be rewritten too (not just manifest.json) on every
                    # rollback pass, even one with no pending candidate files
                    # at all — _atomic_write_text's tmp+os.replace heals that
                    # file's ownership every time it fires, so ownership drift
                    # never lingers just because nothing else this pass
                    # happened to touch verifier_state.json.
                    state_dirty = True
                    continue
                if status != "active":
                    continue

                # RED2 fix: same reasoning as the soak re-check above — the
                # heldout run executes instance artifacts, so it happens in
                # a demoted child; a child-side error is a verifier error,
                # not evidence of regression, so it must not roll anything
                # back — raise into the per-entry try/except instead.
                child = _run_child_verify("heldout")
                if not child.get("ok"):
                    raise RuntimeError(
                        str(child.get("reason") or "heldout child verify failed")
                    )
                clean = bool(child.get("heldout_clean"))
                heldout_reason = str(child.get("heldout_reason") or "")
                integrity_events = _integrity_events_since(STATE_DIR, entry.get("verified_utc"))
                if not clean or integrity_events:
                    reason = heldout_reason or f"integrity events since promotion: {len(integrity_events)}"
                    _rollback(manifest, module_path, entry.get("candidate_id"), reason)
                    summary["rolled_back"] += 1
                    manifest_dirty = True
                    state_dirty = True  # RED1 fix round 2: heal verifier_state.json ownership every rollback pass too
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
    # Internal, undocumented-to-operators re-entry point (RED2 fix): this is
    # how the root parent gets instance-influenced work (git show, the
    # microbench measurement, the held-out pack) executed under a demoted
    # uid instead of as root — see _run_child_verify/_child_verify_main.
    # help=SUPPRESS keeps it out of --help without hiding it from anyone
    # reading the source.
    parser.add_argument("--child-verify", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=("full", "heldout"), default="full", help=argparse.SUPPRESS)
    parser.add_argument("--module-path", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--head-sha", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.child_verify:
        return _child_verify_main(args.mode, args.module_path, args.head_sha)

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
