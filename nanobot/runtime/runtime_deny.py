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
    # #875 YELLOW-2 fix (opus-review round 2): the REST of the verification
    # kernel. The proposal claims this kernel "is never promotable" — that
    # was only structurally true for bridge/promotion/coordinator above;
    # an operator listing any of these in SELFEVO_RUNTIME_SLICE would have
    # made the loader's OWN trust logic (or the gate's evidence inputs)
    # self-promotable, e.g. instance-influenced bytes replacing
    # runtime_deny.py (the deny-set definition itself) or
    # promoted_overlay.py (the agent-side loader + its boundary
    # self-check) — a candidate could then simply delete its own deny
    # entry, or weaken the boundary check, and promote itself next pass.
    'nanobot/runtime/scorecard.py',           # #822/#875 control-plane + promotion visibility
    'nanobot/runtime/benchmark_evidence.py',  # #819/#822 benchmark-claim corroboration gate
    'nanobot/runtime/usage_evidence.py',      # reference-signal / usage-evidence gate input
    'nanobot/runtime/promoted_overlay.py',    # #875 agent-side loader + its boundary self-check
    'nanobot/runtime/runtime_deny.py',        # this module — the deny-set definition itself
    # #877: steers WHICH sha the bridge branches a cycle from (fitness-
    # adjacent, see evolution_tree.py's module docstring for the full trust
    # argument) — an instance must never be able to make its own steering
    # module promotable. No basename token match applies here (deliberate,
    # per the proposal — "evolution" was not added to _RUNTIME_DENY_TOKENS),
    # so this explicit entry is the only thing keeping it denied.
    'nanobot/runtime/evolution_tree.py',
    # #878: computes the hypothesis verdict (supported/refuted/inconclusive)
    # from the same #789-protected FITNESS_SIDECARS every other fitness-
    # adjacent module here reads — fitness-adjacent steering logic exactly
    # like benchmark_evidence.py/usage_evidence.py above, so it gets the
    # same explicit deny entry (no basename token match applies: "verdict"
    # is not in _RUNTIME_DENY_TOKENS, deliberately, so a future unrelated
    # "*_verdict.py" module is not silently swept in by name alone).
    'nanobot/runtime/hypothesis_verdict.py',
    # #879: steers WHICH improvement direction (proposer-quality,
    # cycle-cost, ...) the loop prefers to invest its next cycles in from
    # the same #789-protected scorecard-derived gains every other
    # fitness-adjacent module here reads — same steering tier as
    # evolution_tree.py right above (no basename token match applies:
    # "tree" alone is too broad a token to add here, and "tech" is not in
    # _RUNTIME_DENY_TOKENS, deliberately, so this explicit entry is the
    # only thing keeping it denied).
    'nanobot/runtime/tech_tree.py',
    # #939: skill-fitness sidecar writer — the module that persists confirmed
    # skill reads into state_dir/skill_fitness/reads.json (a FITNESS_SIDECARS
    # member).  The instance must never be able to rewrite the harness-owned
    # record of which skills it read and whether reads were confirmed, since
    # that would let it fabricate fitness credit.  Same explicit-entry pattern
    # as tech_tree.py above (no basename token match applies: 'fitness' is not
    # in _RUNTIME_DENY_TOKENS, deliberately).
    'nanobot/runtime/skill_fitness.py',
    # #941: the skill-eval A/B grader — runs instance-authored eval plans
    # with/without the skill and writes the measured verdict rows into
    # state_dir/skill_fitness/evals.jsonl (a FITNESS_SIDECARS member). The
    # instance may author eval plans, never the grader: rewriting this module
    # would let it fabricate skill fitness. Same explicit-entry pattern as
    # skill_fitness.py above (no basename token match applies: 'eval' and
    # 'harness' are not in _RUNTIME_DENY_TOKENS, deliberately, so unrelated
    # future '*_eval*.py'/'*_harness.py' modules are not swept in by name).
    'nanobot/runtime/skill_eval_harness.py',
    # #1093: knowledge-lift evaluation harness
    'nanobot/runtime/knowledge_lift.py',
    # #899: the single resolver for runtime LLM model selection (operator
    # control-plane, same tier as bridge.py's own model knobs) — the loop
    # must never be able to rewrite which model each role runs on.
    'nanobot/runtime/model_registry.py',
    # #986: knowledge curator — writes to memory/ and docs/ OKF KB; the instance
    # must never be able to rewrite the curator's path-validation logic, output-cap,
    # or watermark semantics (any of which would let it bypass bounded KB writes or
    # advance its own watermark on a failed run). No basename token match applies:
    # 'curator' is not in _RUNTIME_DENY_TOKENS, deliberately, so a future unrelated
    # '*_curator.py' module is not silently swept in by name alone; this explicit
    # entry is the only protection.
    'nanobot/runtime/knowledge_curator.py',
    # #959: context-compaction module — controls which messages survive each
    # loop iteration and writes the compaction journal; the instance must
    # never be able to weaken compaction logic (e.g. raise the threshold or
    # remove the deny entry itself).  No basename token match applies here
    # ('compact' is not in _RUNTIME_DENY_TOKENS, deliberately, so a future
    # unrelated '*_compact*.py' module is not silently swept in by name
    # alone); this explicit entry is the only protection.
    'nanobot/runtime/context_compaction.py',
    # #996: deterministic goal-gap futility tracking and suppression.
    'nanobot/runtime/goal_gap_futility.py',
    # #1006: deterministic action-index recurrence miner.
    'nanobot/runtime/skill_candidate_mining.py',
    # #999: strategist periodic archive reviewer.
    'nanobot/runtime/strategist.py',
})
# Fail-closed token match: any runtime file whose basename contains one of these
# is also denied, so a future gate/safety/approval module is covered without
# having to remember to add it to the explicit list above.
_RUNTIME_DENY_TOKENS = (
    'gate', 'precheck', 'promotion', 'approval', 'safety', 'security', 'stop_guard',
    # #875 YELLOW-2 fix: basename tokens for the rest of the verification
    # kernel (same fail-closed rationale as the tokens above) — covers a
    # FUTURE file in this family (e.g. a second scorecard/evidence helper)
    # without anyone having to remember to widen the explicit list too.
    'bench', 'scorecard', 'overlay', 'deny', 'usage', 'evidence', 'heldout',
)
# The entire #780 held-out pack — the invisible verification kernel this
# whole design depends on never being instance-visible/mutable. A bare
# basename token match on "heldout" (above) would miss
# nanobot/runtime/heldout/__init__.py and .../checkers.py (their basenames
# don't contain "heldout"), so the WHOLE subtree is denied structurally
# here instead — current AND any future file added to that package.
_RUNTIME_DENY_DIR_PREFIXES = (
    'nanobot/runtime/heldout/',
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
    if pl.startswith(_RUNTIME_DENY_DIR_PREFIXES):
        return True
    base = p.rsplit('/', 1)[-1].lower()
    return any(tok in base for tok in _RUNTIME_DENY_TOKENS)


#875-YELLOW-2-ADJACENT ladder (#876): a ranked, lowest-to-highest-blast-radius
# tuple of compute modules the loop can EARN access to widen its own runtime
# mutation surface into, purely as a function of what root has already
# verified and promoted (never a new mutable state machine — see
# ``earned_ladder_slice``/``earned_ladder_level`` below and
# ``nanobot.runtime.promoted_overlay.active_promoted_modules`` for the
# root-owned-manifest read side). Rung 0 (``existence_index.py``) is NOT
# part of the ladder's own unlock logic — it is the operator-seeded base
# module, reachable only through the existing ``SELFEVO_RUNTIME_SLICE`` env
# allow-list (``runtime_deny.runtime_slice_paths``), exactly as before #876.
# The ladder only ever ADDS rungs 1-2 on top of that: rung ``i+1`` unlocks once
# rung ``i`` has an ACTIVE root-verified promotion — consecutive-from-bottom
# only, so a higher rung being promoted (e.g. an operator manually widening
# ``SELFEVO_RUNTIME_SLICE`` directly) can never skip over an unproven lower
# rung. With zero active promotions the ladder contributes nothing at all —
# this keeps ``effective_runtime_slice`` byte-identical to the pre-#876
# env-only ``runtime_slice_paths`` result whenever nothing has been promoted
# yet, including on a deployment where the env slice itself is unset.
#
# Ordering rationale (ascending blast radius): existence_index.py is a small,
# already-microbenched (#822) read-mostly indexer; demand.py shapes what the
# proposer sees but does not itself decide/act; llm_proposer.py decides WHAT
# to propose next (no direct execution power) — the widest-reaching of the
# three. (#924: the former rung 3, cycle_planning.py, was dropped — that
# module was deleted in #916/#923 and the entry was inert.)
RUNTIME_TRUST_LADDER: 'tuple[str, ...]' = (
    'nanobot/runtime/existence_index.py',
    'nanobot/runtime/demand.py',
    'nanobot/runtime/llm_proposer.py',
)
# Invariant assertion (#876): the ladder must never contain a deny-set path —
# the verification kernel stays constitutionally unmodifiable (#603) no
# matter how the ladder is edited in the future. Runs at import time so a
# future edit that accidentally lists a deny-set module fails loudly rather
# than silently degrading the safety shell.
assert not any(_is_runtime_deny(_m) for _m in RUNTIME_TRUST_LADDER), (
    'RUNTIME_TRUST_LADDER must never contain a deny-set path'
)


def earned_ladder_slice(active_modules: 'set[str]') -> 'set[str]':
    """Derive the set of ladder modules the loop has EARNED access to,
    ON TOP OF the operator's env-approved base (rung 0 is NOT included
    here — it comes only from ``runtime_slice_paths``/the env allow-list,
    see :func:`nanobot.runtime.promoted_overlay.effective_runtime_slice`).

    Pure function of ``active_modules`` (the set of module_path entries
    with an ACTIVE root-verified promotion right now — see
    ``promoted_overlay.active_promoted_modules``). Rung ``i+1`` unlocks
    only when rung ``i`` is present in ``active_modules`` — the walk
    STOPS at the first rung whose module is not active, so a higher rung
    being active can never skip over an unproven lower rung
    (consecutive-from-bottom only). Zero active promotions -> ``set()``.

    No new mutable state: this is derived entirely from whatever the
    root-owned promotion manifest says is active right now. Fail-open to
    ``set()`` on any error — a bug here must never widen the surface, only
    ever fail to unlock further rungs.
    """
    try:
        unlocked: 'set[str]' = set()
        for i in range(len(RUNTIME_TRUST_LADDER) - 1):
            if RUNTIME_TRUST_LADDER[i] not in active_modules:
                break
            unlocked.add(RUNTIME_TRUST_LADDER[i + 1])
        return unlocked
    except Exception:
        return set()


def earned_ladder_level(active_modules: 'set[str]') -> int:
    """Number of consecutive leading ladder rungs present in
    ``active_modules`` (0..``len(RUNTIME_TRUST_LADDER)``) — a single
    integer summary for ledger/scorecard visibility. Same
    consecutive-from-bottom rule as :func:`earned_ladder_slice`: a
    non-consecutive active rung does not count. Fail-open to ``0``.
    """
    try:
        level = 0
        for module_path in RUNTIME_TRUST_LADDER:
            if module_path not in active_modules:
                break
            level += 1
        return level
    except Exception:
        return 0


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
