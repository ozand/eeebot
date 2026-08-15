"""State-light LLM proposer (#707).

Fills the gap left when the deterministic generator in
``nanobot.runtime.cycle_planning`` (``next_bounded_candidate`` /
``_derive_generated_candidates``) has run out of hand-maintained
``goal_text.json`` priorities to propose. Reuses the same downstream
machinery — the queued-request JSON the bridge already consumes via
``find_pending_request`` — so from the bridge's point of view a
proposer-written request is indistinguishable from a planner-written one
(C1, see ``docs/changes/707-state-light-proposer/proposal.md`` and
``docs/changes/702-ledger-loop-architecture-decision/design-constraints.md``).

Kill switch: ``SELFEVO_LLM_PROPOSER_ENABLED`` (env, default unset/off).
When off, :func:`should_propose` always returns ``False`` and this module
never makes an LLM call or writes any file — this is the entire rollout
control surface, no other config.

#760 (demand-driven inversion): behind a second, default-ON switch
(``SELFEVO_DEMAND_DRIVEN_ENABLED``, see :mod:`nanobot.runtime.demand`), the
proposer works only when :func:`demand.collect_demand` yields at least one
demand item — the LLM selects and refines from presented demand, it never
invents from a bare inventory. With no demand a bridge cycle makes ZERO LLM
calls and records one ``'idle'`` heartbeat ledger row. The pre-#760
supply-driven policy below stays intact behind that switch.

Everything here is fail-open/fail-closed by design, never raises: a broken
environment, a network error, or a malformed LLM reply degrades to "nothing
proposed this cycle" — identical to today's idle-safe behavior when the
deterministic generator has nothing to offer.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from nanobot.runtime import demand, existence_index, hypothesis_backlog, system_map
from nanobot.runtime.cycle_ledger import append_event
from nanobot.runtime.cycle_planning import (
    _recent_git_log,
    _title_already_done_in_git_log,
    filter_completed_priorities_from_goal_text,
)

ENABLED_ENV = "SELFEVO_LLM_PROPOSER_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}

# Mirrors nanobot.runtime.bridge._ALLOWED_PATH_PREFIXES exactly (#707 C2 —
# checkable sizing). Not imported from bridge.py to avoid a circular import
# (bridge.py imports this module for the invocation hook); duplicated as a
# small literal instead of a shared constant, per the "minimal wiring, no new
# config surface" scope of this change.
_ALLOWED_PATH_PREFIXES = ("surfaces/", "scripts/", "memory/", "lessons/", "docs/", "tests/")

# #823: runtime-slice tier mirror. #812 widened the bounded GATE
# (bridge._classify_mutation_surface) to allow an operator-approved slice of
# nanobot/runtime/*.py modules, but the proposer keeps its own independent copy
# of the surface allowlist (above) and hard-rejects a runtime target_path before
# the gate ever sees it. These mirror bridge._RUNTIME_SLICE_ENV /
# _RUNTIME_DENY_ALWAYS_FILES / _RUNTIME_DENY_TOKENS / _is_runtime_deny /
# _runtime_slice_paths EXACTLY (duplicated, not imported — bridge.py imports this
# module, so importing back would be circular; same reasoning as
# _ALLOWED_PATH_PREFIXES above). Keep these in sync with bridge.py.
_RUNTIME_SLICE_ENV = "SELFEVO_RUNTIME_SLICE"
_RUNTIME_DENY_ALWAYS_FILES = frozenset({
    "nanobot/runtime/bridge.py",
    "nanobot/runtime/promotion.py",
    "nanobot/runtime/coordinator.py",
})
_RUNTIME_DENY_TOKENS = (
    "gate", "precheck", "promotion", "approval", "safety", "security", "stop_guard",
)


def _is_runtime_deny(path: str) -> bool:
    """Mirror of bridge._is_runtime_deny — immutable runtime deny-set. #823."""
    import posixpath as _pp

    p = _pp.normpath(str(path).replace("\\", "/")).lstrip("/")
    if p in _RUNTIME_DENY_ALWAYS_FILES:
        return True
    pl = p.casefold()
    if any(pl == d.casefold() for d in _RUNTIME_DENY_ALWAYS_FILES):
        return True
    base = p.rsplit("/", 1)[-1].lower()
    return any(tok in base for tok in _RUNTIME_DENY_TOKENS)


def _runtime_slice_paths() -> "set[str]":
    """Mirror of bridge._runtime_slice_paths — operator-approved runtime slice
    from SELFEVO_RUNTIME_SLICE. Empty/unset → empty set (feature off, proposer
    behaviour byte-identical to pre-#823). Deny-set entries dropped. #823."""
    import posixpath as _pp

    raw = os.environ.get(_RUNTIME_SLICE_ENV, "") or ""
    out: "set[str]" = set()
    for part in raw.split(","):
        p = part.strip().replace("\\", "/")
        if not p:
            continue
        p = _pp.normpath(p).lstrip("/")
        if not p.startswith("nanobot/runtime/") or not p.endswith(".py"):
            continue
        if _is_runtime_deny(p):
            continue
        out.add(p)
    return out


# #826: sized to fit the operator goal_text (~5KB) PLUS the bounded guardrail
# sections (recently-proposed dupes + #716 recently-failed, each title-capped) so
# the do-not-retry signals are not truncated away when the goal is large. ~2K
# tokens of context is negligible cost. The goal+outcomes blob is still trimmed to
# fit; the guardrails + surface_rule are appended AFTER the trim (never dropped).
_MAX_CONTEXT_CHARS = 8000
_MAX_INVENTORY_CHARS = 4000
_MAX_INVENTORY_ENTRIES = 90
_LEDGER_DIGEST_ROWS = 15
_DUP_STREAK_K = 3
_MAX_TITLE_CHARS = 120
_RECENT_PROPOSED_TITLES_N = 10
# #716: recency window for "attempted but never integrated" titles — see
# _recent_failed_titles. Kept modest (same order of magnitude as
# _LEDGER_DIGEST_ROWS) so an old failure ages out and stops blocking a
# legitimate retry, rather than a permanent ban.
_RECENT_FAILED_TITLES_N = 10
_RECENT_FAILED_WINDOW_CYCLES = 15
_MAX_LLM_CALLS = 3
_MAX_CONSECUTIVE_NOOP_SKIPS = 3
_MAX_SERVES_CHARS = 160

_PRIORITY_PATTERN = re.compile(
    r"\([A-Za-z]\)\s*Priority\s+(\d+)\s*[—-]\s*(.+?):\s*(.+?)(?=\n\([A-Za-z]\)|\Z)",
    re.DOTALL,
)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
# #760: 'demand ' is the primary form under demand-driven mode; the pre-#760
# prefixes are kept as accepted legacy forms for one release so a model
# replying in the old vocabulary is not hard-rejected during rollout.
# #813: 'optimization' is the explicit, structured signal a cycle uses to
# declare an optimization claim (e.g. 'optimization latency: p95 request
# time') — matches nanobot.runtime.benchmark_evidence.is_optimization_claim's
# check exactly, so a value valid here is recognized there too. Steering the
# proposer to actually emit this form is #815; this change only makes the
# signal schema-valid + enforceable at confirmation time.
_SERVES_PREFIXES = ("demand ", "priority ", "vector 1", "vector 2", "hypothesis ", "optimization")
_SERVES_DEMAND_RE = re.compile(r"^demand\s+(\S+)", re.IGNORECASE)

_MAX_DEMAND_CHARS = 4000  # separately bounded, same precedent as _MAX_INVENTORY_CHARS

_PROPOSER_SYSTEM_PROMPT = (
    "You are proposing exactly ONE small, bounded engineering improvement for a "
    "self-evolving codebase. Reply with ONLY a JSON object with keys "
    "task_title, rationale, target_path, serves — no prose, no markdown code "
    "fences. task_title must be non-empty and at most 120 characters, "
    "describing a single behavior/bug (not a bundle). target_path must name "
    "exactly ONE path (file or directory) under one of these mutable "
    "surfaces: surfaces/, scripts/, memory/, lessons/, docs/, tests/ — no "
    "other path is acceptable. serves must name what goal this task serves — "
    "non-empty, at most 160 characters, starting with one of: 'priority <N>' "
    "(a numbered goal_text priority, e.g. 'priority 5'), 'vector 1' or "
    "'vector 2' (optionally followed by a colon and a short 3-8 word "
    "justification, e.g. 'vector 1: reduces cycle disk writes'), or "
    "'hypothesis <id-or-short-title>' naming an entry from the Hypothesis "
    "backlog section below (e.g. 'hypothesis h3'). rationale must briefly "
    "justify the change and must NOT repeat any already-done or recently-"
    "failed work described in the context below. If the goal text lists "
    "numbered 'Current priority targets', propose EXACTLY one of them (the "
    "first not yet done) VERBATIM as the task, with serves naming that "
    "priority number; only invent a new task when no numbered priorities "
    "remain. The context lists existing scripts; do NOT propose a script "
    "that duplicates one (same purpose under a different name) — extend the "
    "existing file or pick a different task instead. If nothing you could "
    "propose creates real value toward the goals — everything worthwhile is "
    "done, queued, or listed as existing — you MAY instead reply with ONLY "
    '{"no_valuable_task": true, "reason": "<short reason>"} instead of '
    "inventing filler work."
)

# #760 demand-driven mode: the model SELECTS AND REFINES from presented
# demand items — it never invents from a bare inventory. Vector 1/2 are no
# longer offered as open-ended invention targets; serves must reference a
# demand id (legacy forms still pass validation for one release, but the
# prompt asks only for demand ids).
_DEMAND_PROPOSER_SYSTEM_PROMPT = (
    "You are selecting exactly ONE demand item from the '## Demand' section "
    "of the context and proposing a small, bounded engineering task that "
    "addresses it. You MUST NOT invent work that no demand item calls for. "
    "Reply with ONLY a JSON object with keys task_title, rationale, "
    "target_path, serves — no prose, no markdown code fences. task_title "
    "must be non-empty and at most 120 characters, describing a single "
    "behavior/bug (not a bundle). target_path must name exactly ONE path "
    "(file or directory) under one of these mutable surfaces: surfaces/, "
    "scripts/, memory/, lessons/, docs/, tests/ — no other path is "
    "acceptable. serves must be 'demand <id>' where <id> is the bracketed id "
    "of the ONE demand item this task addresses (e.g. 'demand "
    "defect-1a2b3c4d5e6f'). rationale must briefly explain how the task "
    "resolves the selected demand item's evidence, and must NOT repeat any "
    "already-done or recently-failed work described in the context. Prefer "
    "'priority'-kind items first (operator-seeded), then 'defect', then "
    "'hypothesis'. The context lists existing scripts; do NOT propose a "
    "script that duplicates one (same purpose under a different name) — "
    "extend the existing file or pick a different demand item instead. If "
    "no presented demand item is addressable with a bounded task, reply "
    'with ONLY {"no_valuable_task": true, "reason": "<short reason>"} '
    "instead of inventing filler work."
)

_FORCE_PROPOSAL_NOTE = (
    "IMPORTANT: you have already replied no_valuable_task for "
    f"{_MAX_CONSECUTIVE_NOOP_SKIPS} consecutive cycles. Do NOT reply "
    "no_valuable_task this cycle — you MUST propose a concrete, valid task "
    "using the schema above, choosing the least-wasteful available option."
)


def _enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "0").strip().lower() in _TRUTHY


def _requests_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "subagents" / "requests"


def _bridge_state_dir(state_dir: Path) -> Path:
    """Mirrors ``nanobot.runtime.bridge``'s ``BRIDGE_STATE_DIR`` (bridge.py:52
    ``os.environ.get('SUBAGENT_BRIDGE_STATE_DIR', str(STATE_DIR /
    'subagent_bridge'))``), rooted at THIS call's own ``state_dir`` argument
    instead of the bridge's module-level ``STATE_DIR`` global — this module
    cannot import bridge.py (circular import, see the ``_ALLOWED_PATH_PREFIXES``
    comment above), so the relation is duplicated as a small literal rather
    than a shared constant. The bridge always calls this module with its own
    ``STATE_DIR`` as ``state_dir``, so the two resolve to the identical path
    in production; tests that use a temp ``state_dir`` get the matching temp
    ``subagent_bridge`` subdirectory.
    """
    return Path(os.environ.get("SUBAGENT_BRIDGE_STATE_DIR", str(Path(state_dir) / "subagent_bridge")))


def _request_id_of(req: dict[str, Any], path: Path) -> str:
    """Same fallback chain the bridge uses to name a request for marker
    purposes (bridge.py:1225, mirrored from ``find_pending_request``'s
    ``rid`` at bridge.py:151): ``request_id``, else ``verification_task_id``,
    else the request file's own path."""
    return str(req.get("request_id") or req.get("verification_task_id") or str(path))


def _is_request_handled(state_dir: Path, req: dict[str, Any], path: Path) -> bool:
    """True iff the bridge already wrote a ``handled_<safe_rid>.txt`` marker
    for this request (bridge.py:1231-1233, 1276 etc.) — the ONLY source of
    handledness; the request file's own ``request_status`` is never rewritten
    (#745). Mirrors the bridge's exact sanitization: ``rid.replace('/',
    '_')[:120]``."""
    rid = _request_id_of(req, path)
    safe_rid = rid.replace("/", "_")[:120]
    marker = _bridge_state_dir(state_dir) / f"handled_{safe_rid}.txt"
    return marker.exists()


def _is_proposer_request(req: dict[str, Any]) -> bool:
    """True iff ``req`` is a request this module itself wrote (``write_request``).

    Matches on the same markers ``write_request`` sets: a ``request_id``
    prefixed ``llm-proposer-``, or a ``source_artifact`` whose filename is
    prefixed ``llm-proposed-`` (the companion artifact this module writes
    under ``improvements/``). Either alone is sufficient — a request forged
    or replayed with only one of the two markers is still ours.
    """
    request_id = str(req.get("request_id") or "")
    if request_id.startswith("llm-proposer-"):
        return True
    source_artifact = str(req.get("source_artifact") or "")
    return Path(source_artifact).name.startswith("llm-proposed-")


def _has_queued_proposer_request(state_dir: Path) -> bool:
    """Anti-stacking guard: is there already a queued, NOT-YET-HANDLED
    proposer-written request?

    Deliberately narrower than "any queued request" (#707 canary finding):
    in production the deterministic planner mints stale duplicate requests
    faster than the bridge consumes them, so the queue never empties and a
    "no queued request at all" clause meant the proposer could never fire —
    a queue full of planner duplicates IS novelty exhaustion, not a reason to
    stay silent. Only a request this module already queued blocks a new one,
    preventing unbounded proposer stacking while still firing on planner-only
    queues.

    #745: a request's ``request_status`` field is never rewritten once
    written — handledness is marker-file based only (bridge.py's
    ``handled_<safe_rid>.txt``, see ``_is_request_handled``). Before this fix,
    a proposer request the bridge had already executed kept reading as
    "queued" in its own file and blocked every subsequent proposal until the
    hourly archiver deleted it — the loop's cadence was accidentally
    archiver-paced instead of timer-paced. A request whose handled marker
    already exists no longer counts as blocking, regardless of its stale
    ``request_status``.
    """
    req_dir = _requests_dir(state_dir)
    if not req_dir.is_dir():
        return False
    for path in req_dir.glob("*.json"):
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(req, dict):
            continue
        status = str(req.get("request_status") or req.get("status") or "").strip().lower()
        if status not in ("queued", "pending") or not _is_proposer_request(req):
            continue
        try:
            if _is_request_handled(state_dir, req, path):
                continue
        except Exception:
            pass
        return True
    return False


def _queue_effectively_empty(state_dir: Path) -> bool:
    """#745: is the requests dir free of ANY unhandled queued/pending
    request, regardless of who wrote it (planner, operator, or this module)?

    This is the "fresh-priorities deadlock" fix: spec R28 (subagent-bridge)
    says the proposer fires "when the queue is empty", but the #731 rework
    lost that clause — ``should_propose`` fired only on priorities-empty or a
    last-3-outcomes duplicate streak. With the deterministic planner off,
    dup streaks stop occurring; if an operator seeds fresh ``goal_text.json``
    priorities that the #712 filter says still remain (nothing done yet),
    the proposer went permanently silent even though there was nothing at
    all queued to work through.

    A request counts as "pending" iff its status is queued/pending AND its
    handled marker does NOT exist (same handledness check as
    ``_has_queued_proposer_request``); an unreadable/malformed request file
    counts as NOT pending — fail-open, consistent with the rest of this
    module. Returns ``True`` iff no request in the dir counts as pending
    (including when the dir does not exist at all).
    """
    req_dir = _requests_dir(state_dir)
    if not req_dir.is_dir():
        return True
    for path in req_dir.glob("*.json"):
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(req, dict):
            continue
        status = str(req.get("request_status") or req.get("status") or "").strip().lower()
        if status not in ("queued", "pending"):
            continue
        try:
            if _is_request_handled(state_dir, req, path):
                continue
        except Exception:
            pass
        return False
    return True


def _load_goal_text(state_dir: Path) -> str:
    path = Path(state_dir) / "goals" / "goal_text.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("text") or "")


def _priorities_remain(filtered_goal_text: str) -> bool:
    marker = "Current priority targets:"
    idx = filtered_goal_text.find(marker)
    if idx == -1:
        return False
    section = filtered_goal_text[idx + len(marker):]
    return bool(_PRIORITY_PATTERN.search(section))


def _load_ledger_rows(state_dir: Path) -> list[dict[str, Any]]:
    path = Path(state_dir) / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _terminal_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("phase") == "outcome"]


def _proposed_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("phase") == "proposed"]


def _recent_proposed_titles(rows: list[dict[str, Any]], n: int = _RECENT_PROPOSED_TITLES_N) -> list[str]:
    """Titles from the last ``n`` 'proposed' ledger rows (this module's own
    write_request appends one per proposal, whether or not it was later
    consumed/rejected by the bridge). Unlike ``'outcome'`` rows, ``'proposed'``
    rows always carry ``task_title`` — this is the only ledger phase that can
    reconstruct "what did the proposer already suggest" for #707 canary
    novelty-collapse detection (rejected-as-duplicate proposals never reach
    git log, since no commit is ever made for them)."""
    titles: list[str] = []
    for row in _proposed_rows(rows)[-n:]:
        title = str(row.get("task_title") or "").strip()
        if title:
            titles.append(title)
    return titles


# #716: outcomes that mean "attempted, but not integrated" — the opposite of
# 'success'/'promotion_candidate', which DID land. Deliberately excludes
# 'skipped-duplicate' (never an attempt at new work; already covered by
# _recent_proposed_titles/git-log dedup).
_NON_INTEGRATED_OUTCOMES = frozenset({"failed", "partial", "timeout"})
# #716: gate rollback reasons that mean the cycle produced real work but it
# was blocked from integrating — same "attempted, not integrated" bucket as
# _NON_INTEGRATED_OUTCOMES above, just recorded on a 'gate' row instead of an
# 'outcome' row.
_NON_INTEGRATED_GATE_REASONS = frozenset(
    {"mutation_surface_violation", "blocked_file_present", "gate_failed"}
)


def _recent_failed_titles(
    rows: list[dict[str, Any]],
    n: int = _RECENT_FAILED_TITLES_N,
    window_cycles: int = _RECENT_FAILED_WINDOW_CYCLES,
) -> list[str]:
    """Titles of recent attempts that were NEVER integrated (#716).

    Before this, the proposer's context only showed already-INTEGRATED work
    (git log) and its OWN recently-proposed titles
    (:func:`_recent_proposed_titles`) — it had no visibility into a theme it
    already tried that failed, timed out, or was rolled back by the gate, so
    it kept re-proposing the same dead-end idea (observed live: 26
    ``proposer_reject reason=self_dedup`` in one loop run, each a re-hit of
    an already-attempted-but-failed theme).

    Joins two ledger phases back to their originating ``'proposed'`` row via
    ``cycle_id`` (the only field that links a title to its terminal result):

    - an ``'outcome'`` row whose ``outcome`` is in
      :data:`_NON_INTEGRATED_OUTCOMES` (``failed``/``partial``/``timeout`` —
      attempted, but never integrated; ``success`` and
      ``promotion_candidate`` are excluded on purpose, they DID integrate);
    - a ``'gate'`` row that blocked integration (``allowed`` falsy) with a
      rollback ``reason`` in :data:`_NON_INTEGRATED_GATE_REASONS`.

    Restricted to the last ``window_cycles`` rows of each phase — a
    deliberate recency policy (#716): an old failure ages out and stops
    blocking a retry, since the surrounding code/goal may have shifted
    enough that the same title is worth another attempt. Titles are
    deduplicated (first occurrence wins) and capped at ``n``. Fail-open: any
    error yields ``[]`` (never blocks a proposal it can't confidently flag).
    """
    try:
        title_by_cycle: dict[str, str] = {}
        for row in _proposed_rows(rows):
            cycle_id = str(row.get("cycle_id") or "").strip()
            title = str(row.get("task_title") or "").strip()
            if cycle_id and title:
                title_by_cycle[cycle_id] = title

        failed_cycle_ids: list[str] = []
        for row in _terminal_rows(rows)[-window_cycles:]:
            if str(row.get("outcome") or "") in _NON_INTEGRATED_OUTCOMES:
                cycle_id = str(row.get("cycle_id") or "").strip()
                if cycle_id:
                    failed_cycle_ids.append(cycle_id)

        # #825 review (MED-3, deliberate, not a bug): a gate-blocked title
        # (out-of-surface path, blocked filename, or a smoke/runtime-slice
        # gate that never went green) feeds the SAME suppression path as a
        # failed outcome — re-proposing the identical wrong approach is
        # exactly #716's motivating case (an out-of-surface rejection kept
        # getting re-proposed). This is windowed (ages out of the
        # window_cycles lookback) and, via _is_duplicate_proposal's
        # self_dedup rejects, subject to demand.py's existing 24h exhaustion
        # expiry — so it is a temporary "stop repeating the same mistake"
        # nudge, never a permanent ban. Do not "fix" this by excluding gate
        # rows.
        gate_rows = [r for r in rows if r.get("phase") == "gate"]
        for row in gate_rows[-window_cycles:]:
            if row.get("allowed"):
                continue
            if str(row.get("reason") or "") in _NON_INTEGRATED_GATE_REASONS:
                cycle_id = str(row.get("cycle_id") or "").strip()
                if cycle_id:
                    failed_cycle_ids.append(cycle_id)

        # #825 review fix: failed_cycle_ids is built oldest-to-newest (ledger
        # append order), so applying the cap forward would keep the OLDEST
        # n titles and silently drop the newest failures — exactly the
        # recent churn #716 needs visible. Walk it newest-first to fill the
        # cap with the most recent failures, then reverse back so the
        # returned list matches _recent_proposed_titles' convention
        # (oldest-first, most-recent-last).
        titles: list[str] = []
        seen: set[str] = set()
        for cycle_id in reversed(failed_cycle_ids):
            title = title_by_cycle.get(cycle_id)
            if title and title not in seen:
                seen.add(title)
                titles.append(title)
                if len(titles) >= n:
                    break
        titles.reverse()
        return titles
    except Exception:
        return []


def _last_k_all_duplicate(state_dir: Path, k: int = _DUP_STREAK_K) -> bool:
    terminal = _terminal_rows(_load_ledger_rows(state_dir))
    if len(terminal) < k:
        return False
    last_k = terminal[-k:]
    return all(r.get("outcome") == "skipped-duplicate" for r in last_k)


# #760: at most ONE idle heartbeat row per bridge cycle. One bridge cycle ==
# one bridge process invocation (timer-paced), so a process-lifetime flag is
# the exact "once per cycle" guard with no extra state file. Tests reset it
# directly.
_idle_recorded_this_process = False


def _record_idle(state_dir: Path) -> None:
    """#760 idle heartbeat: a distinct ``'idle'`` ledger phase recording that
    the cycle deliberately made ZERO LLM calls because there was no demand —
    structurally different from ``proposer_skip`` (an LLM call was made and
    the model declined) and from silence (which is indistinguishable from a
    crash). Fail-open (``append_event`` is best-effort); no ``cycle_id`` —
    no cycle/subagent request exists for an idle cycle. Recorded from inside
    ``should_propose`` (not ``maybe_propose``) because ``should_propose`` is
    the single point that knows the failure reason is "no demand" rather
    than e.g. the anti-stacking guard — recording from ``maybe_propose``
    would force ``should_propose`` to grow a richer return type or the
    demand to be collected twice."""
    global _idle_recorded_this_process
    if _idle_recorded_this_process:
        return
    _idle_recorded_this_process = True
    append_event(state_dir, {"phase": "idle", "reason": "no_demand"})


def should_propose(state_dir: Path, selfevo_repo: Path | None) -> bool:
    """Invocation policy (#707, extended by #745; inverted by #760).

    #760 demand-driven mode (``SELFEVO_DEMAND_DRIVEN_ENABLED``, default ON):
    after the unchanged enabled/anti-stacking gates, the proposer fires iff
    :func:`demand.collect_demand` returns at least one non-exhausted demand
    item — operator-seeded goal_text priorities (preserving R30: seeding a
    fresh priority still wakes the loop), recent real defects, or
    measurement-backed hypotheses. With no demand the cycle makes ZERO LLM
    calls and records one ``'idle'`` heartbeat ledger row (``_record_idle``).
    The pre-#760 supply-driven policy below is preserved verbatim behind the
    kill switch (``SELFEVO_DEMAND_DRIVEN_ENABLED=0`` restores it wholesale).

    Pre-#760 policy (#707, extended by #745): fires on proven novelty
    exhaustion OR an effectively empty request queue.

    ``(no queued, unhandled proposer request) AND ((the request queue has NO
    unhandled queued/pending request at all) OR (filtered goal_text has no
    remaining "Current priority targets" entries) OR (the last K=3 terminal
    ledger outcome rows are all "skipped-duplicate"))``.

    The first clause is an anti-stacking guard on the proposer's OWN
    requests only (see ``_has_queued_proposer_request``) — a queued planner
    request no longer blocks proposing, since a queue full of stale planner
    duplicates is itself a novelty-exhaustion signal this function exists to
    catch. Both this guard and the queue-empty clause below check the
    bridge's marker-file handledness (``_is_request_handled``), not the
    request file's own (never-rewritten) ``request_status`` — otherwise an
    already-executed request keeps blocking until the hourly archiver
    deletes it, making the loop's cadence archiver-paced instead of
    timer-paced (#745).

    The queue-empty clause (spec R28, subagent-bridge) fires regardless of
    who wrote the pending request (any author, not just the proposer): with
    the deterministic planner off, dup streaks stop occurring, so if an
    operator seeds fresh ``goal_text.json`` priorities that still remain
    (nothing done yet) and nothing is queued, the proposer must still fire
    or the loop idles forever (the "fresh-priorities deadlock", #745). The
    priorities-empty and last-3-duplicate-streak clauses are unchanged
    fallbacks for when the queue is non-empty (e.g. still holds an unhandled
    planner request) — they cover the case a re-enabled planner mints stale
    duplicates faster than the bridge consumes them.

    Fail-closed: any error, or a completely missing/unreadable state
    directory, returns ``False``. Always ``False`` when the kill switch
    (``SELFEVO_LLM_PROPOSER_ENABLED``) is off.
    """
    if not _enabled():
        return False
    try:
        state_dir = Path(state_dir)
        if not state_dir.is_dir():
            return False
        if _has_queued_proposer_request(state_dir):
            return False
        if demand.demand_driven_enabled():
            # #760: demand gate. Both prior gates passed, so an empty
            # collection means "no demand" is the ONLY reason not to
            # propose — record the idle heartbeat (at most once per bridge
            # cycle, see _record_idle) and stay silent: zero LLM calls.
            if demand.collect_demand(state_dir, selfevo_repo):
                return True
            _record_idle(state_dir)
            return False
        goal_text_path = state_dir / "goals" / "goal_text.json"
        if not goal_text_path.is_file():
            return False
        if _queue_effectively_empty(state_dir):
            return True
        raw_goal_text = _load_goal_text(state_dir)
        filtered = filter_completed_priorities_from_goal_text(
            raw_goal_text, selfevo_repo, state_dir=state_dir
        )
        if not _priorities_remain(filtered):
            return True
        return _last_k_all_duplicate(state_dir)
    except Exception:
        return False


def _digest_ledger(rows: list[dict[str, Any]], n: int = _LEDGER_DIGEST_ROWS) -> list[str]:
    tail = _terminal_rows(rows)[-n:]
    lines: list[str] = []
    for row in tail:
        outcome = str(row.get("outcome") or "unknown")
        reason = str(row.get("reason") or "").strip()
        branch = str(row.get("branch") or row.get("cycle_id") or "").strip()
        one_liner = f"{outcome}: {reason or branch or '(no detail)'}"
        lines.append(one_liner[:160])
    return lines


def _system_map_inventory_section(
    selfevo_repo: Path | None, *, state_dir: Path | None = None, query: str = "",
) -> str:
    """Bounded ``## Existing scripts`` context (#749): prefer the committed
    ``docs/SYSTEM_MAP.md`` inventory (kept fresh by :func:`update_system_map`
    each cycle); fall back to generating the inventory directly (still no
    LLM call) if the map file is absent, empty, OR present but without our
    own ``## Inventory`` section (#749 follow-up: the instance may ship a
    foreign generator — e.g. ``scripts/generate_system_map.py`` — that writes
    the same file in a different format our parser cannot read; our own
    ``update_system_map`` defers to that generator entirely rather than
    clobbering it, so this fallback ensures the proposer's inventory context
    never silently goes empty just because the on-disk format changed).
    Deterministic, fail-open — returns ``""`` on any error or when
    ``selfevo_repo`` is not given, so the caller can omit the whole section
    gracefully.

    Capped at :data:`_MAX_INVENTORY_ENTRIES` entries and :data:`_MAX_INVENTORY_CHARS`
    characters — kept separate from :data:`_MAX_CONTEXT_CHARS` so this section
    never eats into the goal_text/ledger budget. When over the entry cap, the
    surviving entries used to be picked purely by newest-``st_mtime`` — a
    script RELEVANT to the current demand could be older than the cap and
    silently drop out, inviting the proposer to rebuild it under a new name
    (#840, the behavioral root of a low confirmed_integration_ratio). Now:
    when ``query`` is non-empty and ``state_dir`` is given,
    :func:`nanobot.runtime.existence_index.related_scripts` supplies a
    best-first relevance ranking; those entries are kept FIRST (in relevance
    order), then remaining slots are filled with the existing newest-by-mtime
    ordering (skipping already-included paths) — relevant tools now survive
    the cap. With ``query`` empty, no ``state_dir``, the index disabled, or no
    relevant hits, behavior is EXACTLY the prior mtime-only ordering (no
    regression).
    """
    if not selfevo_repo:
        return ""
    try:
        repo = Path(selfevo_repo)
        map_path = repo / "docs" / "SYSTEM_MAP.md"
        lines: list[str] = []
        if map_path.is_file():
            lines = system_map.parse_inventory_section(map_path.read_text(encoding="utf-8"))
        if not lines:
            # Absent file, empty file, or a foreign-format map our parser
            # could not extract an "## Inventory" section from — generate
            # directly from the repo rather than losing the section.
            lines = system_map.inventory_lines(repo)
        if not lines:
            return ""

        total = len(lines)
        if total > _MAX_INVENTORY_ENTRIES:
            def _rel_for_line(line: str) -> str:
                try:
                    return line[2:].split(" — ", 1)[0].strip()
                except Exception:
                    return ""

            def _mtime_for_line(line: str) -> float:
                try:
                    rel = _rel_for_line(line)
                    return (repo / rel).stat().st_mtime
                except Exception:
                    return 0.0

            related: list[str] = []
            if query and state_dir is not None:
                try:
                    related = existence_index.related_scripts(
                        Path(state_dir), repo, query,
                    )
                except Exception:
                    related = []

            if related:
                by_path = {_rel_for_line(line): line for line in lines}
                ordered: list[str] = []
                included: set[str] = set()
                for rel in related:
                    line = by_path.get(rel)
                    if line is not None and rel not in included:
                        ordered.append(line)
                        included.add(rel)
                remaining = [
                    line for line in lines if _rel_for_line(line) not in included
                ]
                remaining.sort(key=_mtime_for_line, reverse=True)
                slots = max(0, _MAX_INVENTORY_ENTRIES - len(ordered))
                lines = ordered + remaining[:slots]
            else:
                lines = sorted(lines, key=_mtime_for_line, reverse=True)[:_MAX_INVENTORY_ENTRIES]

            note = f"({total} scripts total; showing the {_MAX_INVENTORY_ENTRIES} most recently modified)"
            section = note + "\n" + "\n".join(lines)
        else:
            section = "\n".join(lines)

        if len(section) > _MAX_INVENTORY_CHARS:
            section = section[:_MAX_INVENTORY_CHARS]
        return section
    except Exception:
        return ""


def _inventory_query(
    demand_items: list[dict[str, str]] | None, goal_text: str,
) -> str:
    """Build the relevance query for the existing-scripts inventory (#840):
    the demand item(s) being presented this cycle (summary/title text,
    whichever field is populated), or — with no demand — the goal-text
    priorities already in scope. Bounded to keep the FTS query cheap.
    Fail-open: returns ``""`` on any error (caller then falls back to the
    unranked, mtime-only inventory ordering)."""
    try:
        parts: list[str] = []
        for item in demand_items or []:
            if not isinstance(item, dict):
                continue
            text = str(
                item.get("summary") or item.get("title") or item.get("task_title") or "",
            ).strip()
            if text:
                parts.append(text)
        if not parts:
            parts.append(goal_text or "")
        return " ".join(parts).strip()[:500]
    except Exception:
        return ""


def _demand_section(demand_items: list[dict[str, str]]) -> str:
    """Bounded ``## Demand`` body (#760): one block per item with kind,
    stable id, summary, and quoted evidence, capped at
    :data:`_MAX_DEMAND_CHARS` (same separately-bounded-section precedent as
    :data:`_MAX_INVENTORY_CHARS`). Fail-open: returns ``""`` on any error."""
    try:
        lines: list[str] = []
        for item in demand_items:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()
            if not summary:
                continue
            line = f"- [{item.get('id') or '?'}] ({item.get('kind') or '?'}) {summary}"
            evidence = str(item.get("evidence") or "").strip()
            if evidence:
                line += f' — evidence: "{evidence}"'
            affected = str(item.get("affected_path") or "").strip()
            if affected:
                line += f" (affected: {affected})"
            lines.append(line)
        if not lines:
            return ""
        section = "\n".join(lines)
        if len(section) > _MAX_DEMAND_CHARS:
            section = section[:_MAX_DEMAND_CHARS]
        return section
    except Exception:
        return ""


def build_context(
    state_dir: Path,
    selfevo_repo: Path | None,
    *,
    force_proposal: bool = False,
    demand_items: list[dict[str, str]] | None = None,
) -> str:
    """Compact, bounded proposer context (#707 C3; extended by #749, #751).

    Two read-only base inputs, kept separate: the filtered (done-items
    stripped) goal_text, and a bounded digest of the last N terminal ledger
    rows (done/failure signal, so the LLM does not re-propose already-
    handled work). The base is hard-capped to ~4000 chars. A third,
    separately-bounded section (#749) is appended when ``selfevo_repo`` has a
    script inventory: what already exists, so the loop stops shipping
    near-duplicate scripts under new names (the confirmed
    ``track_memory.py``/``monitor_memory.py`` failure) — omitted entirely
    when there is nothing to show, rather than truncating the base context
    to make room. A fourth, separately-bounded section (#751) lists the top
    active hypothesis-backlog candidates (see
    ``nanobot.runtime.hypothesis_backlog``) as ``serves: hypothesis <id>``
    targets — also omitted entirely when there is nothing to show.

    ``force_proposal`` (#751): when the proposer's consecutive
    ``no_valuable_task`` skip streak has hit its cap, the caller
    (``maybe_propose``) sets this so the appended note tells the model it
    must propose a concrete task this cycle rather than skip again.

    ``demand_items`` (#760): when demand-driven mode is on, the caller
    passes the collected demand items and this function leads with a
    separately-bounded ``## Demand`` section (kind, id, summary, quoted
    evidence per item) plus a selection instruction: the model selects ONE
    demand item and sets ``serves`` to ``demand <id>``, or replies
    ``no_valuable_task``. The existing inventory/system-map/hypothesis/
    ledger sections are kept — they prevent duplicates — but the model no
    longer gets open-ended "invent from Vector 1/2" framing.

    Fail-open: returns an empty string on any error.
    """
    try:
        state_dir = Path(state_dir)
        raw_goal_text = _load_goal_text(state_dir)
        filtered_goal = filter_completed_priorities_from_goal_text(
            raw_goal_text, selfevo_repo, state_dir=state_dir
        )
        ledger_rows = _load_ledger_rows(state_dir)
        digest_lines = _digest_ledger(ledger_rows)
        recent_proposed_titles = _recent_proposed_titles(ledger_rows)
        recent_failed_titles = _recent_failed_titles(ledger_rows)
        surface_rule = (
            "Mutable surface rule: target_path MUST be a single path under "
            "one of: " + ", ".join(_ALLOWED_PATH_PREFIXES) + " — no other "
            "path is acceptable."
        )
        # #823/#812: when the operator has opened a runtime slice, the proposer
        # may also target those specific nanobot/runtime modules for Vector-1
        # self-optimization. Off by default → surface_rule unchanged.
        _slice = sorted(_runtime_slice_paths())
        if _slice:
            surface_rule += (
                " Additionally, these operator-approved runtime modules MAY be "
                "targeted for Vector-1 self-optimization: " + ", ".join(_slice) +
                " — such a change faces a STRICTER gate and is NEVER auto-integrated"
                " (it lands as a promotion candidate for operator review). Attach a"
                " before/after measurement for any performance claim. Do NOT target"
                " the gate, promotion, coordinator, or any safety module."
            )
        # #826: split the prompt into a TRUNCATABLE blob (goal + outcomes digest —
        # the large, low-priority-if-cut content) and PROTECTED guardrail sections
        # (recently-proposed dupes + #716 recently-failed + surface_rule) that must
        # always reach the model. Previously all sections were one blob hard-cut to
        # _MAX_CONTEXT_CHARS with the goal first, so a large goal truncated the
        # trailing guardrails away — the do-not-retry signals never reached the
        # proposer and it kept generating duplicate/failed proposals (post-hoc
        # dedup caught them, but the wasted LLM call already happened). Now only
        # the blob is trimmed; guardrails + surface_rule are appended after.
        blob_parts = [
            "## Goal (filtered — already-completed priorities removed)",
            filtered_goal.strip() or "(no goal text available)",
            "",
            "## Recent cycle outcomes (most recent last — do not repeat done/failed work)",
            "\n".join(f"- {line}" for line in digest_lines) or "(no ledger history yet)",
        ]
        guardrail_parts = [
            "",
            "## Recently proposed (rejected as duplicates — do NOT propose these themes again)",
            "\n".join(f"- {title}" for title in recent_proposed_titles) or "(none yet)",
        ]
        # #716: only appended when non-empty — with no recent failures this section
        # is absent (keeps output byte-identical to pre-#716 on that axis).
        if recent_failed_titles:
            guardrail_parts += [
                "",
                "## Recently attempted but NOT integrated (failed/rejected — "
                "do NOT re-propose the same approach; choose different work)",
                "\n".join(f"- {title}" for title in recent_failed_titles),
            ]
        # #825/#826: surface_rule + the guardrail sections are never truncated.
        # Reserve their length, trim only the goal+outcomes blob to what remains,
        # then append guardrails and surface_rule after the trim.
        guardrail_parts.append("")  # blank line before surface_rule (spacing parity)
        guardrail_tail = "\n".join(guardrail_parts)
        # #827 review: guarantee the ≤_MAX_CONTEXT_CHARS invariant even in the
        # pathological case where the (bounded) guardrails + surface_rule alone
        # would exceed the budget — trim the guardrail body, but NEVER surface_rule.
        max_guardrail = max(0, _MAX_CONTEXT_CHARS - len(surface_rule) - 2)
        if len(guardrail_tail) > max_guardrail:
            guardrail_tail = guardrail_tail[:max_guardrail]
        reserved = len(guardrail_tail) + len(surface_rule) + 2  # two joining "\n"
        blob = "\n".join(blob_parts)
        budget = max(0, _MAX_CONTEXT_CHARS - reserved)
        if len(blob) > budget:
            blob = blob[:budget]
        context = blob + "\n" + guardrail_tail + "\n" + surface_rule

        if demand_items:
            demand_body = _demand_section(demand_items)
            if demand_body:
                context = (
                    "## Demand (the ONLY valid work sources this cycle)\n"
                    + demand_body
                    + "\n\nSelect ONE demand item above and propose a bounded "
                    "task that addresses it; set serves to the demand id "
                    "(e.g. 'demand defect-1a2b3c4d5e6f'). If no demand item "
                    "is addressable, reply no_valuable_task.\n\n"
                    + context
                )

        inventory_section = _system_map_inventory_section(
            selfevo_repo,
            state_dir=state_dir,
            query=_inventory_query(demand_items, filtered_goal),
        )
        if inventory_section:
            context += (
                "\n\n## Existing scripts (do not duplicate — reuse or extend "
                "one of these instead of writing a new file)\n"
                + inventory_section
            )

        try:
            hypothesis_section = hypothesis_backlog.context_section(state_dir)
        except Exception:
            hypothesis_section = ""
        if hypothesis_section:
            context += (
                "\n\n## Hypothesis backlog (candidate value sources)\n"
                + hypothesis_section
            )

        if force_proposal:
            context += "\n\n" + _FORCE_PROPOSAL_NOTE

        return context
    except Exception:
        return ""


def _model_name() -> str:
    raw = os.environ.get("SUBAGENT_BRIDGE_MODEL", "cl/gemini-3.5-flash-low").strip()
    raw = raw or "cl/gemini-3.5-flash-low"
    if raw.startswith("openai/"):
        raw = raw[len("openai/"):]
    return raw


_VALID_REASONING_EFFORTS = frozenset({"low", "medium", "high"})


def bridge_reasoning_effort() -> str | None:
    """Operator-selected reasoning effort for the bridge model (#832).

    Reads ``SUBAGENT_BRIDGE_REASONING_EFFORT``; returns ``"low"``/``"medium"``/
    ``"high"`` when set to a valid tier, else ``None`` (send no param — the
    pre-#832 behavior). Lets models without a baked ``-high`` name variant
    (e.g. ``cl/gpt-5.6-luna``) still run at high reasoning. Shared with the
    materializer path in ``tool_harness``.
    """
    raw = os.environ.get("SUBAGENT_BRIDGE_REASONING_EFFORT", "").strip().lower()
    return raw if raw in _VALID_REASONING_EFFORTS else None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    stripped = text.strip()
    fence_match = _JSON_FENCE_RE.search(stripped)
    candidate = fence_match.group(1) if fence_match else None
    if candidate is None:
        obj_match = _JSON_OBJ_RE.search(stripped)
        candidate = obj_match.group(0) if obj_match else None
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def propose(
    context: str,
    *,
    rejection_reason: str | None = None,
    timeout: float = 120.0,
    system_prompt: str | None = None,
) -> dict[str, Any] | None:
    """One chat completion via the same LiteLLM gateway the bridge uses.

    ``system_prompt`` (#760): demand-driven callers pass
    :data:`_DEMAND_PROPOSER_SYSTEM_PROMPT`; default (``None``) keeps the
    pre-#760 :data:`_PROPOSER_SYSTEM_PROMPT` for the kill-switch-off path.

    Fails open (returns ``None``) on any missing config, network error, or
    unparseable reply — never raises.
    """
    try:
        from openai import OpenAI
    except Exception:
        return None
    base_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    api_key = os.environ.get("LITELLM_API_KEY", "").strip()
    if not base_url or not api_key:
        return None
    user_content = context
    if rejection_reason:
        user_content = (
            f"{context}\n\n"
            f"Your previous proposal was rejected: {rejection_reason}. "
            "Propose a different, valid one that fixes this problem."
        )
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        create_kwargs: dict[str, Any] = dict(
            model=_model_name(),
            messages=[
                {"role": "system", "content": system_prompt or _PROPOSER_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=400,
            temperature=0.4,
        )
        effort = bridge_reasoning_effort()  # #832: opt-in high-reasoning proposals
        if effort:
            create_kwargs["reasoning_effort"] = effort
        response = client.chat.completions.create(**create_kwargs)
        reply = response.choices[0].message.content or ""
    except Exception:
        return None
    return _extract_json_object(reply)


def _validate_serves(proposal: dict[str, Any]) -> tuple[bool, str]:
    """#751: goal-alignment field validation, same reject/retry path as the
    other schema checks in :func:`validate_sizing`. ``serves`` must be a
    non-empty string, at most :data:`_MAX_SERVES_CHARS` (160) characters,
    starting (case-insensitively) with one of :data:`_SERVES_PREFIXES` —
    ``'priority '``, ``'vector 1'``, ``'vector 2'``, or ``'hypothesis '``.
    ``'vector 1'``/``'vector 2'`` alone are accepted (the optional
    justification suffix, e.g. ``': reduces cycle disk writes'``, is not
    separately validated beyond the length cap)."""
    serves = proposal.get("serves")
    if isinstance(serves, (list, tuple, dict)):
        return False, "serves must be a single string, not a list/object"
    serves = str(serves or "").strip()
    if not serves:
        return False, "serves is missing"
    if len(serves) > _MAX_SERVES_CHARS:
        return False, f"serves exceeds {_MAX_SERVES_CHARS} chars"
    if not serves.lower().startswith(_SERVES_PREFIXES):
        return False, (
            "serves must start with one of "
            f"{_SERVES_PREFIXES} (goal alignment): got {serves!r}"
        )
    return True, ""


def _demand_id_from_serves(serves: Any) -> str:
    """#760: extract the demand id from a ``serves`` value of the form
    ``demand <id>`` (case-insensitive); ``""`` for legacy forms or garbage."""
    try:
        match = _SERVES_DEMAND_RE.match(str(serves or "").strip())
        return match.group(1) if match else ""
    except Exception:
        return ""


def validate_sizing(proposal: dict[str, Any] | None) -> tuple[bool, str]:
    """Pre-spawn checkable sizing (#707 C2; extended by #751's ``serves``
    goal-alignment field). Returns ``(ok, reason)``."""
    if not isinstance(proposal, dict):
        return False, "proposal is not a JSON object"

    title = str(proposal.get("task_title") or "").strip()
    if not title:
        return False, "task_title is empty"
    if len(title) > _MAX_TITLE_CHARS:
        return False, f"task_title exceeds {_MAX_TITLE_CHARS} chars"

    rationale = str(proposal.get("rationale") or "").strip()
    if not rationale:
        return False, "rationale is missing"

    target_path = proposal.get("target_path")
    if isinstance(target_path, (list, tuple)):
        return False, "target_path must be exactly one path, not a list"
    target_path = str(target_path or "").strip()
    if not target_path:
        return False, "target_path is missing"
    if "," in target_path or ";" in target_path or "\n" in target_path:
        return False, "target_path must name exactly one path"
    # #823: accept a script-surface prefix OR an operator-approved runtime-slice
    # path (#812). A deny-set path is never acceptable even if it were listed in
    # the slice env (fail-closed); with the slice env empty (default) this is
    # byte-identical to the old prefix-only check.
    _norm_target = target_path.replace("\\", "/")
    _in_script_surface = any(target_path.startswith(prefix) for prefix in _ALLOWED_PATH_PREFIXES)
    _in_runtime_slice = _norm_target in _runtime_slice_paths()
    if not (_in_script_surface or _in_runtime_slice):
        return False, f"target_path outside allowed surfaces {_ALLOWED_PATH_PREFIXES}: {target_path}"
    if _in_runtime_slice and _is_runtime_deny(_norm_target):
        return False, f"target_path is a runtime deny-set path (immutable safety shell): {target_path}"

    ok, reason = _validate_serves(proposal)
    if not ok:
        return False, reason

    return True, ""


_PERMANENT_DEDUP_MAX_COMMITS = 3000


def _all_built_subjects(selfevo_repo: Path | None) -> str:
    """Full-history commit subjects of the instance repo (#834 permanent novelty).

    Unlike :func:`cycle_planning._recent_git_log`'s 14-day window, this is the
    complete catalogue of everything ever integrated into ``main``, so a
    throwaway script cannot be silently rebuilt once its creation commit ages
    out of the recency window. Bounded to the most recent
    :data:`_PERMANENT_DEDUP_MAX_COMMITS` subjects. Fail-open: ``""`` on any
    error.
    """
    if not selfevo_repo:
        return ""
    import subprocess as _sp_hist

    repo = Path(selfevo_repo)
    try:
        return _sp_hist.check_output(
            [
                "git", "-c", f"safe.directory={repo}", "-C", str(repo),
                "log", "--format=%s", f"-n{_PERMANENT_DEDUP_MAX_COMMITS}",
            ],
            stderr=_sp_hist.DEVNULL,
            timeout=15,
        ).decode(errors="replace")
    except Exception:
        return ""


def _proposal_creates_new_file(selfevo_repo: Path | None, proposal: dict[str, Any]) -> bool:
    """True when the proposal would create a target_path that does NOT yet exist
    in the instance repo (#834).

    The permanent novelty guard applies only to NEW-file creation — edits and
    improvements to an existing (possibly consumed) artifact are iteration, not
    churn, and must never be newly blocked. Fail-open to ``False`` (treat as an
    edit, i.e. do not apply the permanent guard) on any error.
    """
    if not selfevo_repo:
        return False
    target = str(proposal.get("target_path") or "").strip()
    if not target:
        return False
    try:
        repo = Path(selfevo_repo).resolve()
        candidate = (repo / target).resolve()
        # Reject anything that escapes the repo (absolute target_path, ``..``):
        # pathlib's ``/`` drops the left operand on an absolute right operand,
        # so probe containment explicitly and treat an escape as "not a repo
        # new-file" (skip the guard; validate_sizing rejects such paths anyway).
        candidate.relative_to(repo)
    except Exception:
        return False
    return not candidate.exists()


def _is_duplicate_proposal(
    state_dir: Path, selfevo_repo: Path | None, proposal: dict[str, Any]
) -> tuple[bool, str, str]:
    """Pre-write self-dedup (#707 canary novelty collapse; extended by #716).

    Reuses the SAME per-line proportional word-overlap heuristic the bridge
    and deterministic planner already use for "is this title already done"
    (``cycle_planning._title_already_done_in_git_log``), fed with three
    sources concatenated: the recent git log (already-DONE work), this
    proposer's own recent ``'proposed'`` ledger titles (already-REJECTED-as-
    duplicate work, which never reaches git log since no commit is ever made
    for it), and (#716) recent titles that WERE attempted but never
    integrated — failed, timed out, or gate-blocked
    (:func:`_recent_failed_titles`), which also never reach git log. Any
    source matching is sufficient to flag a duplicate.

    #716 policy note: :func:`_recent_failed_titles` only looks back a small,
    recent window, so this is a temporary "don't immediately re-hit the same
    dead end" guard, not a permanent ban — an old failure ages out and a
    retry is allowed again once it exits the window.

    Returns ``(True, feedback_text, matched_against)`` on a match —
    ``feedback_text`` is meant to be passed as ``propose()``'s
    ``rejection_reason`` on retry, and ``matched_against`` (#762) is the
    single git-log/ledger line the heuristic actually matched (the same
    "what it actually matched, not an echo of the proposal's own title"
    discipline the bridge's dedup rows follow per #757), so a
    ``proposer_reject``/``self_dedup`` ledger row can record it. Fail-open:
    any error is treated as "not a duplicate".
    """
    try:
        title = str(proposal.get("task_title") or "").strip()
        if not title:
            return False, "", ""
        git_log = _recent_git_log(Path(selfevo_repo)) if selfevo_repo else ""
        ledger_rows = _load_ledger_rows(state_dir)
        recent_titles = _recent_proposed_titles(ledger_rows)
        recent_failed_titles = _recent_failed_titles(ledger_rows)
        combined_log = (
            "\n".join([git_log] + recent_titles + recent_failed_titles)
            if (git_log or recent_titles or recent_failed_titles)
            else ""
        )
        if combined_log and _title_already_done_in_git_log(title, combined_log):
            # The heuristic is per-line (>= a proportional share of the
            # title's words on ONE line), so re-running it line-by-line
            # recovers exactly which line matched.
            matched_against = next(
                (
                    line.strip()
                    for line in combined_log.splitlines()
                    if line.strip() and _title_already_done_in_git_log(title, line)
                ),
                "",
            )
            return True, (
                f"your proposal '{title}' duplicates already-done, "
                "recently-rejected, or recently-failed (non-integrated) "
                "work; propose something from a DIFFERENT area, preferring "
                "the numbered Current priority targets"
            ), matched_against

        # #834 permanent novelty guard: for proposals that CREATE A NEW file,
        # also reject against the full commit history (not just the 14-day
        # window above), so a throwaway artifact is not silently rebuilt once
        # its creation commit ages out. Edits/improvements to an existing file
        # are iteration, not churn, and are never blocked here.
        if _proposal_creates_new_file(selfevo_repo, proposal):
            built_subjects = _all_built_subjects(selfevo_repo)
            if built_subjects and _title_already_done_in_git_log(title, built_subjects):
                matched_built = next(
                    (
                        line.strip()
                        for line in built_subjects.splitlines()
                        if line.strip() and _title_already_done_in_git_log(title, line)
                    ),
                    "",
                )
                return True, (
                    f"your proposal '{title}' re-creates an artifact that "
                    "ALREADY EXISTS in the repo history (built previously); "
                    "improve/reuse the existing one, or propose genuinely NEW "
                    "work from the numbered Current priority targets"
                ), matched_built
        return False, "", ""
    except Exception:
        return False, "", ""


def _active_goal_id(state_dir: Path) -> str:
    try:
        path = Path(state_dir) / "goals" / "registry.json"
        if not path.is_file():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return str(data.get("active_goal_id") or "")
    except Exception:
        pass
    return ""


def _display_title(task_title: str) -> str:
    """Format a proposal's ``task_title`` the way it is stored in the
    written request's own ``task_title`` field (``write_request``) — the
    single formatting rule shared by the persisted request and the
    ``maybe_propose`` return value, so the two can never drift (#741)."""
    task_title = task_title.strip()
    return f"Implement and commit: {task_title}" if task_title else task_title


def _consecutive_noop_streak(state_dir: Path) -> int:
    """#751 kill-switch bound: count trailing ``no_valuable_task`` skip
    events among this module's own proposer-decision ledger rows
    (``'proposed'`` and ``'proposer_skip'`` phases only), most-recent-first,
    stopping at the first ``'proposed'`` row or the start of the ledger.
    Tracked via the ledger (not in-memory) so the cap survives process
    restarts. Fail-open: any error reads as ``0`` (no streak), which only
    ever makes the caller MORE willing to allow another no-op — never less
    safe than the ledger being unreadable."""
    try:
        rows = _load_ledger_rows(state_dir)
        relevant = [r for r in rows if r.get("phase") in ("proposed", "proposer_skip")]
        count = 0
        for row in reversed(relevant):
            if row.get("phase") == "proposer_skip":
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


def _record_noop_skip(state_dir: Path, reason: str) -> None:
    """#751 honest no-op: a distinct ``'proposer_skip'`` ledger phase (NOT a
    ``'proposed'`` row with a placeholder title) so this event never pollutes
    title-based dedup (``_recent_proposed_titles``) or the ``'proposed'``-row
    goal-alignment counts in ``scripts/loop_metrics_report.py``. No
    ``cycle_id`` — no cycle/subagent request exists for a skipped cycle."""
    append_event(
        state_dir,
        {
            "phase": "proposer_skip",
            "reason": (reason or "").strip()[:200] or "(no reason given)",
        },
    )


def _record_proposer_reject(
    state_dir: Path,
    reason: str,
    *,
    task_title: str = "",
    target_path: str = "",
    matched_against: str = "",
    detail: str = "",
    demand_id: str = "",
) -> None:
    """#762 silent-exit observability: a distinct ``'proposer_reject'``
    ledger phase for `maybe_propose`'s formerly-silent ``return None`` exits
    (``reason`` ∈ ``empty_context`` / ``sizing_rejected`` / ``self_dedup`` /
    ``error``), so a saturated loop burning LLM calls on rejected proposals
    is visible in the ledger instead of requiring by-hand diagnosis on the
    host. Like ``_record_noop_skip`` this is NOT a ``'proposed'`` row — it
    must never pollute title-based dedup (``_recent_proposed_titles``) or
    the goal-alignment counts — and carries no ``cycle_id`` (no
    cycle/subagent request exists for a rejected proposal). For
    ``self_dedup``, ``matched_against`` records what the heuristic actually
    matched (same shape/spirit as the bridge's dedup rows, #757). Fail-open:
    recording must never raise or block a cycle — ``append_event`` is
    already best-effort, and this wrapper is belt-and-suspenders on top
    (it is also called from inside ``maybe_propose``'s final except block,
    where a raise would escape the safety net entirely)."""
    with contextlib.suppress(Exception):
        event: dict[str, Any] = {
            "phase": "proposer_reject",
            "reason": (reason or "").strip() or "error",
        }
        if task_title:
            event["task_title"] = task_title.strip()[:200]
        if target_path:
            event["target_path"] = target_path.strip()[:200]
        if matched_against:
            event["matched_against"] = matched_against.strip()[:200]
        if detail:
            event["detail"] = detail.strip()[:200]
        if demand_id:
            # #760: which demand item the rejected proposal claimed to serve
            # — demand.py's exhaustion tracking counts self_dedup rejects per
            # demand_id to stop re-presenting a saturated item.
            event["demand_id"] = demand_id.strip()[:120]
        append_event(state_dir, event)


def _consecutive_self_dedup_rejects(state_dir: Path) -> int:
    """#762 saturation signal (consumed by #760's demand-exhaustion
    escalation): count trailing ``'proposer_reject'`` rows with reason
    ``'self_dedup'`` among this module's own proposer-decision ledger rows
    (``'proposed'``, ``'proposer_skip'``, ``'proposer_reject'`` phases),
    most-recent-first, stopping at the first other decision row or the
    start of the ledger. Same construction (and the same
    ``_load_ledger_rows`` active-file read bound) as
    :func:`_consecutive_noop_streak`. Fail-open: any error reads as ``0``
    (no saturation) — never MORE aggressive than the ledger being
    unreadable."""
    try:
        rows = _load_ledger_rows(state_dir)
        relevant = [
            r for r in rows if r.get("phase") in ("proposed", "proposer_skip", "proposer_reject")
        ]
        count = 0
        for row in reversed(relevant):
            if row.get("phase") == "proposer_reject" and row.get("reason") == "self_dedup":
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


def write_request(state_dir: Path, proposal: dict[str, Any]) -> str:
    """Write the request JSON in the ``subagent-request-v1`` shape the
    subagent bridge consumes (#707 C1) — same keys, ``request_status:
    "queued"`` — so the bridge's ``find_pending_request`` picks it up. Since
    #747 deleted the deterministic planner's request-minting lane, the
    proposer is the sole writer of these requests.

    ``target_path``/``rationale`` are carried WITHOUT changing the request
    schema: they are embedded in a small companion artifact (a
    ``next_bounded_candidate`` shape under a distinct ``llm-proposed-*``
    filename) and ``source_artifact`` points at it — an existing,
    already-optional field the bridge already dereferences.

    Also appends a ``'proposed'`` ledger row so proposer cycles are visible
    in ``scripts/loop_metrics_report.py``. #751: that row also carries
    ``serves`` (the goal-alignment field, already schema-validated by
    :func:`validate_sizing` before this is ever called) so the report can
    compute a per-serves-class distribution; deliberately NOT added to the
    request ``payload`` itself, to keep the C1 request-schema stable.
    """
    state_dir = Path(state_dir)
    cycle_id = f"cycle-{uuid.uuid4().hex[:12]}"
    goal_id = _active_goal_id(state_dir)
    task_title = str(proposal.get("task_title") or "").strip()
    rationale = str(proposal.get("rationale") or "").strip()
    target_path = str(proposal.get("target_path") or "").strip()
    serves = str(proposal.get("serves") or "").strip()

    improvements_dir = state_dir / "improvements"
    improvements_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = improvements_dir / f"llm-proposed-{cycle_id}.json"
    artifact_payload = {
        "schema_version": "llm-proposed-improvement-v1",
        "source_artifact": "llm_proposer",
        "next_bounded_candidate": {
            "title": task_title,
            "backlog_instructions": (
                f"{rationale}\n\nTarget path: {target_path}" if rationale else f"Target path: {target_path}"
            ),
            "backlog_priority": None,
        },
        "recommended_next_action": f"Implement and commit: {task_title} (target: {target_path})",
    }
    artifact_path.write_text(json.dumps(artifact_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    request_id = f"llm-proposer-{cycle_id}"
    request_dir = _requests_dir(state_dir)
    request_dir.mkdir(parents=True, exist_ok=True)
    request_path = request_dir / f"request-{cycle_id}.json"
    payload = {
        "schema_version": "subagent-request-v1",
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "task_id": "llm-proposed-improvement",
        "semantic_task_id": "llm-proposed-improvement",
        "request_id": request_id,
        "verification_task_id": request_id,
        "verification_role": "materialized_improvement_implementation",
        "task_title": _display_title(task_title),
        # #760 follow-up: the 'Serves: <serves>' marker line (same task-text
        # marker mechanism as '#736 Target path:') lets the bridge recognize
        # demand-vetted requests ('Serves: demand <id>') — the demand
        # collector already applied the strong done-filter (#748/#769), so
        # the bridge must not second-guess with its weaker word heuristic
        # (live false kill of the P14 proposal, 2026-07-15 20:42Z). Kept out
        # of the payload keys to preserve the C1 schema-equality invariant.
        "task": (
            f"{rationale}\n\nTarget path: {target_path}"
            + (f"\nServes: {serves}" if serves else "")
        ).strip(),
        "recommended_next_action": f"Implement and commit: {task_title} (target: {target_path})",
        "request_status": "queued",
        "profile": "bounded_execution",
        "budget": "standard",
        "source_artifact": str(artifact_path),
        "feedback_decision": None,
        "lessons_context": {},
    }
    request_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    proposed_event: dict[str, Any] = {
        "phase": "proposed",
        "cycle_id": cycle_id,
        "request_id": request_id,
        "task_title": task_title,
        "target_path": target_path,
        "serves": serves,
        "source_artifact": "llm_proposer",
    }
    # #760: demand traceability — when serves is 'demand <id>', the proposed
    # row also carries the id, so proposal→demand-item is queryable.
    demand_id = _demand_id_from_serves(serves)
    if demand_id:
        proposed_event["demand_id"] = demand_id
    append_event(state_dir, proposed_event)

    return str(request_path)


def maybe_propose(state_dir: Path, selfevo_repo: Path | None) -> str | None:
    """Single public entrypoint (#707, hardened for the canary novelty-
    collapse defect): build context, propose, validate sizing (with one
    retry on rejection), self-dedup (with one retry-with-feedback), and
    write the request if valid.

    At most :data:`_MAX_LLM_CALLS` (3) chat completions are made per call:
    an initial proposal, an optional sizing-rejection retry, and an optional
    dedup-rejection retry — the two retry budgets share this one cap rather
    than stacking additively, so a proposal that fails sizing twice never
    also gets a dedup retry, and a proposal that fails dedup after a sizing
    retry gets exactly one more try.

    #751: an honest ``{"no_valuable_task": true, "reason": ...}`` reply (only
    honored while :func:`_consecutive_noop_streak` is under
    :data:`_MAX_CONSECUTIVE_NOOP_SKIPS`) records a ``'proposer_skip'`` ledger
    event and returns ``None`` immediately — no subagent request is minted.
    ``should_propose`` firing at most once per bridge cycle (timer-paced,
    ~10 min) is itself sufficient pacing for this path; no extra guard is
    needed to keep a run of skips from tight-looping.

    Returns the just-written request's ``task_title`` (the same string
    ``write_request`` persists) iff a request was written this call, else
    ``None``. #741: the caller must log THIS return value, not a post-write
    ``find_pending_request`` lookup — the queue is oldest-first, so a lookup
    after this write can return a stale, unrelated request's title whenever
    older requests are still pending. The return value is still truthy iff
    a request was written (a written title is never empty — ``validate_sizing``
    rejects an empty ``task_title``), so existing ``if maybe_propose(...)``
    call sites keep working unchanged. Never raises — every step is
    individually fail-open, and the whole function is wrapped in a final
    safety net so a bug here can never break the bridge cycle that calls it.
    """
    try:
        # #749: keep the instance repo's SYSTEM_MAP.md fresh once per cycle,
        # regardless of the kill-switch or should_propose's verdict below —
        # the bridge calls maybe_propose() unconditionally every cycle (see
        # bridge.py's comments at each call site), so this is the cheapest
        # once-per-cycle hook available. update_system_map is itself a
        # watermark-gated no-op when the instance repo's HEAD hasn't moved,
        # and is fail-open (never raises) on its own, but is wrapped here too
        # as defense-in-depth — a system-map bug must never block a proposal.
        if selfevo_repo is not None:
            try:
                system_map.update_system_map(Path(selfevo_repo), Path(state_dir))
            except Exception:
                pass

        if not should_propose(state_dir, selfevo_repo):
            return None

        # #751: an LLM that has honestly skipped 3 cycles in a row (no
        # valuable task) must not be allowed to idle the loop forever — the
        # 4th call is forced into normal proposal mode (the no-op reply is
        # not offered in the context, and even if the model replies with one
        # anyway it is ignored below and treated as a schema violation,
        # belt-and-suspenders). Tracked via trailing ledger rows, not
        # in-memory, so the cap survives process restarts.
        allow_no_op = _consecutive_noop_streak(state_dir) < _MAX_CONSECUTIVE_NOOP_SKIPS

        # #760 demand-driven mode: collect the demand items once (a second
        # deterministic pass after should_propose's gate — the py_compile
        # scan inside is watermark-gated so the repeat costs one git
        # rev-parse plus small file reads), present them as the ONLY valid
        # work sources, and swap in the select-and-refine system prompt.
        demand_mode = demand.demand_driven_enabled()
        # #815: emit_split=True ONLY here — the single context-build call
        # site per cycle — so the operator-visible demand_vector_split
        # ledger event fires exactly once per cycle (the should_propose gate
        # probe above leaves it default False).
        demand_items = (
            demand.collect_demand(state_dir, selfevo_repo, emit_split=True) if demand_mode else None
        )

        context = build_context(
            state_dir,
            selfevo_repo,
            force_proposal=not allow_no_op,
            demand_items=demand_items,
        )
        if not context:
            # #762: formerly a silent exit — a context-builder failure looked
            # identical to a healthy idle cycle in the ledger.
            _record_proposer_reject(state_dir, "empty_context")
            return None

        def _call_propose(rejection_reason: str | None = None) -> dict[str, Any] | None:
            if demand_mode:
                return propose(
                    context,
                    rejection_reason=rejection_reason,
                    system_prompt=_DEMAND_PROPOSER_SYSTEM_PROMPT,
                )
            return propose(context, rejection_reason=rejection_reason)

        def _proposal_demand_id(p: Any) -> str:
            return _demand_id_from_serves(p.get("serves")) if isinstance(p, dict) else ""

        def _is_noop_reply(p: Any) -> bool:
            # #760 roll-out fix: the weak host model emits no_valuable_task
            # as the string "true" (or 1) rather than a JSON boolean; such a
            # reply then fell through to validate_sizing and burned a retry
            # call on "task_title is empty". Accept the common truthy forms.
            if not isinstance(p, dict):
                return False
            v = p.get("no_valuable_task")
            if v is True or v == 1:
                return True
            return isinstance(v, str) and v.strip().lower() in ("true", "yes", "1")

        calls_made = 0

        proposal = _call_propose()
        calls_made += 1

        if allow_no_op and _is_noop_reply(proposal):
            _record_noop_skip(state_dir, str(proposal.get("reason") or ""))
            return None

        ok, reason = validate_sizing(proposal)
        if not ok and calls_made < _MAX_LLM_CALLS:
            proposal = _call_propose(rejection_reason=reason)
            calls_made += 1
            if allow_no_op and _is_noop_reply(proposal):
                _record_noop_skip(state_dir, str(proposal.get("reason") or ""))
                return None
            ok, reason = validate_sizing(proposal)
        def _sizing_detail(reason_text: str, p: Any) -> str:
            # #760 roll-out fix: "task_title is empty" alone was
            # undiagnosable — include a snippet of the raw reply so the
            # ledger shows WHAT the model actually sent (capped by the
            # recorder's own detail limit).
            try:
                snippet = json.dumps(p, ensure_ascii=False)[:120] if isinstance(p, dict) else repr(p)[:120]
            except Exception:
                snippet = "<unserializable>"
            return f"{reason_text}; reply={snippet}"

        if not ok:
            # #762: double sizing failure — record what was rejected and why.
            _record_proposer_reject(
                state_dir,
                "sizing_rejected",
                task_title=str((proposal or {}).get("task_title") or "") if isinstance(proposal, dict) else "",
                target_path=str((proposal or {}).get("target_path") or "") if isinstance(proposal, dict) else "",
                detail=_sizing_detail(reason, proposal),
                demand_id=_proposal_demand_id(proposal),
            )
            return None

        dup, dup_reason, dup_matched = _is_duplicate_proposal(state_dir, selfevo_repo, proposal)
        if dup and calls_made < _MAX_LLM_CALLS:
            proposal = _call_propose(rejection_reason=dup_reason)
            calls_made += 1
            # #760 follow-up (live 2026-07-15 20:42-21:02Z): a model told
            # "your proposal duplicates X" may honestly answer
            # no_valuable_task — this path lacked the no-op check, so three
            # honest refusals were recorded as sizing_rejected instead of
            # proposer_skip.
            if allow_no_op and _is_noop_reply(proposal):
                _record_noop_skip(state_dir, str(proposal.get("reason") or ""))
                return None
            ok, reason = validate_sizing(proposal)
            if not ok:
                # #762: the dedup retry came back mis-sized — still a sizing
                # rejection, recorded as such.
                _record_proposer_reject(
                    state_dir,
                    "sizing_rejected",
                    task_title=str((proposal or {}).get("task_title") or "") if isinstance(proposal, dict) else "",
                    target_path=str((proposal or {}).get("target_path") or "") if isinstance(proposal, dict) else "",
                    detail=_sizing_detail(reason, proposal),
                    demand_id=_proposal_demand_id(proposal),
                )
                return None
            dup, dup_reason, dup_matched = _is_duplicate_proposal(state_dir, selfevo_repo, proposal)
        if dup:
            # #762: double self-dedup rejection — the live-saturation case
            # (every cycle burning 2-3 LLM calls with zero ledger trace).
            # matched_against records what it actually matched (#757 spirit).
            # #760: demand_id lets demand.py's exhaustion tracking stop
            # presenting an item whose proposals keep self-dedup-rejecting.
            _record_proposer_reject(
                state_dir,
                "self_dedup",
                task_title=str(proposal.get("task_title") or ""),
                target_path=str(proposal.get("target_path") or ""),
                matched_against=dup_matched,
                demand_id=_proposal_demand_id(proposal),
            )
            return None

        write_request(state_dir, proposal)
        return _display_title(str(proposal.get("task_title") or ""))
    except Exception as exc:
        # #762: the catch-all safety net now leaves a trace. The recorder is
        # itself fail-open (contextlib.suppress), so this can never raise out
        # of the except block and break the bridge cycle.
        _record_proposer_reject(state_dir, "error", detail=f"{type(exc).__name__}: {exc}")
        return None
