"""eeepc-agent-side loader for root-verified promoted runtime modules (#875).

**The trust problem this closes.** On the eeepc host, the bridge/harness AND
the instance's subagent run as the SAME uid (``eeepc-agent``). ``state/`` is
instance-writable — so a loader that execs code from ``state/`` trusting an
instance-written index would let the instance forge index+file into
arbitrary code execution in the harness's own process, before any gate ever
runs. No in-band signature closes this: same uid means the instance can read
any verification key the harness could read too.

**The only real boundary on this host is root.** A root-run systemd timer
(``host/eeepc/libexec/eeepc_promotion_verifier.py``) independently
re-measures and re-verifies each runtime-slice promotion candidate the
instance PROPOSED, and only if it is satisfied writes the verified module
bytes into ``PROMOTED_TREE`` (default ``/var/lib/eeepc-promoted``) — a
ROOT-OWNED, 0755 directory the ``eeepc-agent`` uid can read but never write.
Filesystem permission IS the boundary; there is no crypto key to steal
because there is no crypto key.

This module is the OTHER half: the eeepc-agent-uid loader that installs
those root-verified modules into the running process, shadowing the
as-installed ``nanobot/runtime/*.py`` copy. It is deliberately built from
STDLIB + :mod:`nanobot.runtime.runtime_deny` ONLY — no other nanobot.runtime
import — so that installing the overlay never itself depends on any module
that overlay could plausibly ever replace.

Trust invariants (#875, verbatim — do not weaken):

- The instance never authors the promoted bytes or the "verified" numbers;
  it only ever authors PROPOSALS. This module trusts nothing the instance
  wrote — it trusts only what ROOT wrote into ``PROMOTED_TREE``.
- :func:`install_promoted_overlay` REFUSES to load anything — returns ``[]``
  — unless BOTH ``PROMOTED_TREE`` itself and its ``manifest.json`` are
  root-owned (``st_uid == 0``) and neither group- nor other-writable. A
  misprovisioned or (impossibly, but defense-in-depth) instance-writable
  tree must never be trusted, no matter what its manifest claims.
- The deny-set + runtime-slice shape are re-checked HERE, independently of
  whatever the root verifier already checked, via the SAME
  ``nanobot.runtime.runtime_deny`` logic the bridge's gate uses — a
  manifest entry naming a deny-set path (however it got there) is refused.
- Content integrity: the on-disk promoted file's sha256 must match the
  manifest's recorded ``sha256`` for that module — a root-owned tree whose
  file was corrupted/truncated/only-partially-written is refused for that
  one module (installed stays), not trusted regardless.
- Fail-open PER MODULE (one bad entry never blocks the others; the
  installed copy of that one module is kept) but fail-CLOSED overall: any
  unexpected top-level exception anywhere in this function returns ``[]``
  (nothing loaded) rather than partially/ambiguously loading — an error
  here must degrade to the fully-installed, already-reviewed product tree,
  never to an unknown state.
- Host-only feature: on any non-POSIX platform (``os.name != 'posix'``,
  e.g. a developer's Windows machine) the ownership check ``os.stat(...)
  .st_uid`` is not meaningful, so this refuses to load ANYTHING there
  rather than silently skip the check.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from nanobot.runtime.runtime_deny import earned_ladder_slice, runtime_slice_paths

# Matches the systemd EnvironmentFile default this loader and the root
# verifier (host/eeepc/libexec/eeepc_promotion_verifier.py) agree on. An
# explicit ``promoted_tree`` argument or the ``PROMOTED_TREE`` env var
# override this for tests / non-default deployments.
_DEFAULT_PROMOTED_TREE = "/var/lib/eeepc-promoted"
_MANIFEST_FILENAME = "manifest.json"

# Any write bit for group or other (0o020 | 0o002) makes a path untrusted —
# only the owner (root, checked separately via st_uid) may ever write here.
_WRITABLE_BY_OTHERS_MASK = 0o022


def _resolve_promoted_tree(promoted_tree: "str | Path | None") -> Path:
    if promoted_tree:
        return Path(promoted_tree)
    env_value = os.environ.get("PROMOTED_TREE")
    if env_value:
        return Path(env_value)
    return Path(_DEFAULT_PROMOTED_TREE)


def _root_owned_and_not_writable(path: Path) -> bool:
    """True iff ``path`` is owned by root (uid 0) and not group/other-writable.

    POSIX-only by construction (``os.stat().st_uid`` is meaningless
    elsewhere) — callers gate this behind ``os.name == 'posix'`` first.
    """
    st = path.stat()
    if st.st_uid != 0:
        return False
    if st.st_mode & _WRITABLE_BY_OTHERS_MASK:
        return False
    return True


def _boundary_ok(tree_dir: Path, manifest_path: Path) -> bool:
    """The critical self-check: refuse EVERYTHING unless both the promoted
    tree directory and its manifest are root-owned and not group/other
    writable. Host-only (POSIX); refuses unconditionally elsewhere."""
    if os.name != "posix":
        return False
    try:
        return _root_owned_and_not_writable(tree_dir) and _root_owned_and_not_writable(manifest_path)
    except Exception:
        return False


def _flattened_filename(module_path: str) -> str:
    """The on-disk filename convention shared with the root verifier:
    ``nanobot/runtime/existence_index.py`` -> ``nanobot__runtime__existence_index.py``.
    """
    return module_path.replace("/", "__")


def _load_one_module(tree_dir: Path, module_path: str, entry: "dict[str, Any]") -> bool:
    """Load exactly one manifest-active module. Returns True on success.

    Never raises — any failure here means "skip this module, keep the
    installed copy", handled by the caller's per-entry try/except.
    """
    # Re-derive the canonical slice-shaped path for this key via the SAME
    # deny-set/slice logic the bridge gate + root verifier use. If the key
    # is malformed (traversal, backslashes, wrong prefix/suffix) or on the
    # immutable deny-set, runtime_slice_paths normalizes/drops it and the
    # result will not be exactly {module_path} — refuse in that case.
    if runtime_slice_paths(module_path) != {module_path}:
        return False

    sha_expected = entry.get("sha256")
    if not isinstance(sha_expected, str) or not sha_expected:
        return False

    tree_file = tree_dir / _flattened_filename(module_path)
    if not tree_file.is_file():
        return False

    data = tree_file.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha_expected:
        return False  # root-written file doesn't match root's own manifest — refuse

    dotted = module_path[: -len(".py")].replace("/", ".")
    parent_dotted, _, leaf = dotted.rpartition(".")
    parent_mod = importlib.import_module(parent_dotted) if parent_dotted else None

    spec = importlib.util.spec_from_file_location(dotted, str(tree_file))
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec, matching the standard import
    # protocol (supports the module's own internal imports resolving it if
    # something re-enters, and matches importlib convention).
    sys.modules[dotted] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(dotted, None)
        raise
    if parent_mod is not None:
        setattr(parent_mod, leaf, module)
    return True


def _is_genuinely_active(tree_dir: Path, module_path: str, entry: "dict[str, Any]") -> bool:
    """True iff a manifest entry claiming ``status == "active"`` is ALSO
    genuinely valid by the exact same rules :func:`_load_one_module` (the
    loader) enforces — the trust ladder must never advance on an entry the
    loader itself would refuse to load. Checks (all must pass): the
    module_path re-derives to itself via :func:`runtime_slice_paths`
    (slice-shape + deny-set re-check), the flattened tree file exists, and
    its sha256 matches the manifest's recorded ``sha256``. Never raises —
    any failure here means "don't count this entry", handled by the
    caller's per-entry try/except.
    """
    if runtime_slice_paths(module_path) != {module_path}:
        return False
    sha_expected = entry.get("sha256")
    if not isinstance(sha_expected, str) or not sha_expected:
        return False
    tree_file = tree_dir / _flattened_filename(module_path)
    if not tree_file.is_file():
        return False
    return hashlib.sha256(tree_file.read_bytes()).hexdigest() == sha_expected


def active_promoted_modules(promoted_tree: "str | Path | None" = None) -> "set[str]":
    """Return the ``module_path`` keys whose PROMOTED_TREE manifest entry is
    currently ``status == "active"`` AND genuinely valid (#876 — the
    trust-ladder's ONLY input).

    Reuses the exact same boundary self-check :func:`install_promoted_overlay`
    uses (:func:`_boundary_ok` via :func:`_resolve_promoted_tree`) — the
    ladder must never advance on an instance-writable or misprovisioned
    tree, so a boundary failure here returns ``set()`` exactly like the
    overlay loader refuses to load anything in that case. Each ``"active"``
    entry is ALSO re-validated via :func:`_is_genuinely_active` — the same
    slice-shape/deny-set/sha256/file-existence checks the loader itself
    enforces — so the ladder never advances on a corrupt or mismatched
    manifest entry that the loader would refuse to load anyway; a failing
    entry is simply skipped (per-entry fail-open), never counted. Fail-closed
    to ``set()`` on ANY error, missing tree/manifest, non-POSIX platform, or
    a malformed manifest — a bug here must never widen the ladder, only ever
    fail to advance it.
    """
    try:
        tree_dir = _resolve_promoted_tree(promoted_tree)
        manifest_path = tree_dir / _MANIFEST_FILENAME
        if not tree_dir.is_dir() or not manifest_path.is_file():
            return set()
        if not _boundary_ok(tree_dir, manifest_path):
            return set()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        if not isinstance(manifest, dict):
            return set()
        active: "set[str]" = set()
        for module_path, entry in manifest.items():
            try:
                if not isinstance(module_path, str) or not isinstance(entry, dict):
                    continue
                if entry.get("status") != "active":
                    continue
                if _is_genuinely_active(tree_dir, module_path, entry):
                    active.add(module_path)
            except Exception:
                continue  # per-entry fail-open — a bad entry is simply not counted
        return active
    except Exception:
        return set()


def effective_runtime_slice(env_value: "str | None", promoted_tree: "str | Path | None" = None) -> "set[str]":
    """The ONE function every runtime-slice consumer should call (#876):
    the operator-approved env slice UNION the trust-ladder rungs the loop
    has earned via root-verified promotions.

    Kept here (rather than in the pure ``runtime_deny`` module) because
    computing it requires reading the root-owned manifest — this module
    already owns that read plus its boundary self-check;
    ``nanobot.runtime.runtime_deny`` stays pure/filesystem-free per its own
    module contract. Byte-identical to the pre-#876 ``runtime_slice_paths``
    result whenever no ladder rung is active — ``earned_ladder_slice``
    contributes nothing at all in that case (see
    :func:`nanobot.runtime.runtime_deny.earned_ladder_slice`); rung 0
    (``existence_index.py``) reaches the effective slice only via the
    operator's own env allow-list, exactly as before #876.
    """
    return runtime_slice_paths(env_value) | earned_ladder_slice(active_promoted_modules(promoted_tree))


def install_promoted_overlay(promoted_tree: "str | Path | None" = None) -> "list[str]":
    """Install every ACTIVE root-verified module from ``PROMOTED_TREE`` over
    the installed ``nanobot/runtime/*.py`` copies, in THIS process.

    Returns the list of ``module_path`` entries actually loaded (possibly
    empty). Call this ONCE, as early as possible, before importing any
    ``nanobot.runtime`` module that might be in the operator's runtime
    slice (see ``bridge.py``'s wiring) — a module already imported before
    this call keeps running its installed copy for any reference already
    bound to it.

    Fail-closed overall (``[]`` on any unexpected top-level error) /
    fail-open per module (one bad manifest entry never blocks the rest).
    See the module docstring for the full trust-invariant list.
    """
    try:
        tree_dir = _resolve_promoted_tree(promoted_tree)
        manifest_path = tree_dir / _MANIFEST_FILENAME
        if not tree_dir.is_dir() or not manifest_path.is_file():
            return []  # nothing provisioned — quiet no-op

        if not _boundary_ok(tree_dir, manifest_path):
            return []  # misprovisioned/instance-writable tree — refuse EVERYTHING

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(manifest, dict):
            return []

        loaded: "list[str]" = []
        for module_path, entry in manifest.items():
            try:
                if not isinstance(module_path, str) or not isinstance(entry, dict):
                    continue
                if entry.get("status") != "active":
                    continue
                if _load_one_module(tree_dir, module_path, entry):
                    loaded.append(module_path)
            except Exception:
                continue  # per-module fail-open — the installed copy stays
        return loaded
    except Exception:
        return []  # fail-closed to fully-installed
