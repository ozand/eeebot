"""Runtime-slice immutable deny-set + slice-path helpers (#875 extraction).

Pure stdlib module: no imports beyond the standard library. This is
deliberate — it is imported by THREE independent trust contexts that must
never accidentally pull in a heavier (and therefore more attackable or more
failure-prone) dependency chain:

- ``nanobot.runtime.bridge`` (the eeepc-agent-uid gate that classifies a
  cycle's changed files, #812/#812-classify),
- ``host/eeepc/libexec/eeepc_promotion_verifier.py`` (the ROOT-run verifier,
  #875 — re-checks eligibility itself, never trusting the instance's own
  classification),
- ``nanobot.runtime.promoted_overlay`` (the eeepc-agent-side loader, #875 —
  re-checks deny/slice membership again before loading a root-promoted
  module, independent of what the verifier already checked).

Originally this logic lived directly in ``bridge.py`` (#812). #875 moved it
here UNCHANGED (zero behavior change) so the root verifier and the agent
loader can share the exact same deny-set/slice-membership logic the gate
uses, rather than each hand-rolling (and potentially drifting from) their
own copy of "what counts as safety-shell" and "what counts as in-slice".
``bridge.py`` re-exports ``_RUNTIME_DENY_ALWAYS_FILES``,
``_RUNTIME_DENY_TOKENS`` and ``_is_runtime_deny`` under the SAME names for
backward compatibility (existing tests reference ``bridge._is_runtime_deny``
etc.) and keeps its own env-reading ``_runtime_slice_paths()`` wrapper
around :func:`runtime_slice_paths` here.

Trust invariant (#875, verbatim — do not weaken): the deny-set is the
safety shell around the runtime-slice tier. It must be enforced identically
wherever a candidate module is considered for execution — the bridge's
classifier, the root verifier's eligibility check, and the agent-side
overlay loader's final guard — so that a deny-set path can NEVER become
promotable/loadable no matter which of the three call sites is asked.
"""
from __future__ import annotations

import posixpath as _pp

# Immutable deny-set: the safety shell. These paths may NEVER be mutated by
# the loop even if an operator mistakenly lists them in SELFEVO_RUNTIME_SLICE
# — the allow-slice can only ADD compute modules, never re-open the
# gate/promotion/approval path. Product-PR-owned (this list lives in
# off-surface nanobot/), so the instance cannot widen it. #812 / #603
# (bounded blast radius).
_RUNTIME_DENY_ALWAYS_FILES = frozenset({
    'nanobot/runtime/bridge.py',       # the gate + the allow/deny lists
    'nanobot/runtime/promotion.py',    # promotion candidate review/apply workflow
    'nanobot/runtime/coordinator.py',  # cycle orchestration + promotion minting
})
# Fail-closed token match: any runtime file whose basename contains one of these
# is also denied, so a future gate/safety/approval module is covered without
# having to remember to add it to the explicit list above.
_RUNTIME_DENY_TOKENS = (
    'gate', 'precheck', 'promotion', 'approval', 'safety', 'security', 'stop_guard',
)


def _is_runtime_deny(path: str) -> bool:
    """True if ``path`` is in the immutable runtime deny-set (safety shell). #812.

    Defense-in-depth (independent of git's already-canonical path output): the
    path is backslash-normalized and ``..``/``.`` segments are collapsed before
    matching, and the explicit-file match is case-insensitive — so a deny path
    cannot be smuggled past the check via traversal or a case variant.
    """
    p = _pp.normpath(path.replace('\\', '/')).lstrip('/')
    if p in _RUNTIME_DENY_ALWAYS_FILES:
        return True
    pl = p.casefold()
    if any(pl == d.casefold() for d in _RUNTIME_DENY_ALWAYS_FILES):
        return True
    base = p.rsplit('/', 1)[-1].lower()
    return any(tok in base for tok in _RUNTIME_DENY_TOKENS)


def runtime_slice_paths(env_value: 'str | None') -> 'set[str]':
    """Parse a ``SELFEVO_RUNTIME_SLICE``-style comma value into a set of
    operator-approved ``nanobot/runtime/*.py`` paths (#875 extraction of the
    #812 logic, made PURE — takes the raw env string as an argument instead
    of reading ``os.environ`` itself, so callers with no business reading
    process env — the root verifier is handed its own copy of the value,
    never trusting ``os.environ`` implicitly — and unit tests can exercise
    it without ``monkeypatch``).

    Comma-separated, repo-relative. Empty/``None`` -> empty set (feature
    off). Only ``nanobot/runtime/*.py`` paths are honored; anything else is
    ignored (the value cannot re-open ``state/`` or add a deny-set path).
    Deny-set entries are dropped even if listed — the safety shell is never
    mutable. Fail-open to empty on any trouble: an unreadable value must
    never widen surface.
    """
    out: 'set[str]' = set()
    try:
        raw = env_value or ''
        for part in raw.split(','):
            p = part.strip().replace('\\', '/')
            if not p:
                continue
            # collapse traversal so 'nanobot/runtime/../bridge.py' can't sneak in
            p = _pp.normpath(p).lstrip('/')
            if not p.startswith('nanobot/runtime/') or not p.endswith('.py'):
                continue  # slice is compute-module-only; value cannot widen elsewhere
            if _is_runtime_deny(p):
                continue  # deny-set always wins over the allow-slice
            out.add(p)
    except Exception:
        return set()
    return out
