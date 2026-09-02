"""State-light LLM proposer (#707).

Fills the gap left when the deterministic generator that used to live in
``nanobot.runtime.cycle_planning`` (``next_bounded_candidate`` /
``_derive_generated_candidates``, retired with the coordinator module web,
#916) ran out of hand-maintained ``goal_text.json`` priorities to propose.
Reuses the same downstream
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
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nanobot.observability.llm_telemetry import call_context, record_llm_call, record_llm_prompt
from nanobot.runtime import archive, demand, existence_index, hypothesis_backlog, system_map
from nanobot.runtime.cycle_ledger import append_event
from nanobot.runtime.goal_text_utils import (
    _recent_git_log,
    _title_already_done_in_git_log,
    filter_completed_priorities_from_goal_text,
)
from nanobot.runtime.lessons_context import build_lessons_context
from nanobot.runtime.model_registry import resolve_model
from nanobot.runtime.reflection_context import build_reflection_hints

ENABLED_ENV = "SELFEVO_LLM_PROPOSER_ENABLED"
_RELEASE_ROOT_DEFAULT = "/opt/eeepc-agent/runtimes/self-evolving-agent/current"
_TRUTHY = {"1", "true", "yes", "on"}

# Mirrors nanobot.runtime.bridge._ALLOWED_PATH_PREFIXES exactly (#707 C2 —
# checkable sizing). Not imported from bridge.py to avoid a circular import
# (bridge.py imports this module for the invocation hook); duplicated as a
# small literal instead of a shared constant, per the "minimal wiring, no new
# config surface" scope of this change.
# 'skills/' opens the workspace/instance skill tree (SKILL.md + bundled resources).
_ALLOWED_PATH_PREFIXES = ("surfaces/", "scripts/", "memory/", "lessons/", "docs/", "tests/", "skills/")

# Root AGENTS.md is operator-owned and is not a mutable proposal target.
_ALLOWED_EXACT_PATHS = frozenset()

# #944: explicitly blocked paths (immutable files that proposals may never
# target), mirroring bridge._BLOCKED_EXACT_PATHS. goals.md is the immutable
# operator charter shipped in the release tree.
_BLOCKED_EXACT_PATHS = frozenset({'goals.md', 'IDENTITY.md'})
# #947 (fix-pass): mirror of bridge structural filename policy. Keep this
# module-level copy behaviorally identical because bridge imports proposer.
# Backward-compat tuple used by test extraction:
_BLOCKED_FILE_PATTERNS = (
    '.env', '.git', '.npmrc', 'package-lock', 'yarn.lock', 'id_rsa', 'private_key',
)
_BLOCKED_WORD_PATTERNS = frozenset({'secret', 'credential', 'token'})
_SENSITIVE_WORDS = _BLOCKED_WORD_PATTERNS
_ALLOWED_SENSITIVE_BASENAMES = frozenset({
    'token_report.py', 'summarize_token_costs.py', 'token_budget_check.py',
    'analyze_token_usage.py', 'check_token_budget.py', 'validate_no_secrets.py',
    'count_tokens.py',
})


def _is_blocked_filename(f: str) -> bool:
    """Return True if *f* matches any blocked-file pattern.

    Two-tier check (#947 fix-pass, mirror of bridge._is_blocked_filename):

    1. Structural hard-blocks: ``.env``, ``.git``, ``.npmrc``,
       ``package-lock``, ``yarn.lock``, ``id_rsa``, ``private_key``.

    2. Sensitive-word rule: split stem on ``._-``; singularize trailing ``s``
       when the result is in ``_SENSITIVE_WORDS``; block when the last segment
       is sensitive, unless immediately preceded by ``no``.

    ``_ALLOWED_SENSITIVE_BASENAMES`` names explicit exceptions.
    """
    import re as _re_blk
    lower = f.lower().replace('\\', '/')
    basename = lower.rsplit('/', 1)[-1]
    stem = basename.rsplit('.', 1)[0]

    # Named exception: counting/reporting utilities.
    if basename in _ALLOWED_SENSITIVE_BASENAMES:
        return False

    # Structural hard-blocks (path-level and exact basename families).
    structural_blocked = (
        '.git' in lower.split('/')
        or basename == '.env' or basename.startswith('.env.')
        or basename == '.npmrc' or basename.startswith('.npmrc.')
        or basename == 'package-lock.json' or basename.startswith('package-lock.')
        or basename == 'yarn.lock' or basename.startswith('yarn.lock.')
        or stem == 'id_rsa' or stem.startswith('id_rsa_')
        or 'private_key' in stem or 'secret_key' in stem
    )
    if structural_blocked:
        return True

    # Sensitive-word rule: final segment, singular-normalised.
    segments = [part for part in _re_blk.split(r'[._-]', stem) if part]
    if not segments:
        return False
    last = segments[-1]
    if last.endswith('s') and last[:-1] in _SENSITIVE_WORDS:
        last = last[:-1]
    if last in _SENSITIVE_WORDS:
        return True

    return False

# #823: runtime-slice tier mirror. #812 widened the bounded GATE
# (bridge._classify_mutation_surface) to allow an operator-approved slice of
# nanobot/runtime/*.py modules, but the proposer keeps its own hard-rejection
# of a runtime target_path before the gate ever sees it.
#
# #875 update: this used to be a hand-duplicated copy of bridge.py's deny-set
# logic (comment here previously explained "not imported — bridge.py imports
# this module, so importing back would be circular"). #875 extracted that
# logic into the stdlib-only nanobot.runtime.runtime_deny module specifically
# so it has NO dependency on bridge.py or this module — the circular-import
# obstacle no longer exists, so this now imports the SAME canonical functions
# bridge.py, the root promotion verifier, and the agent-side overlay loader
# all use, instead of maintaining a fourth copy that could silently drift.
_RUNTIME_SLICE_ENV = "SELFEVO_RUNTIME_SLICE"
from nanobot.runtime.promoted_overlay import effective_runtime_slice  # noqa: E402
from nanobot.runtime.runtime_deny import _is_runtime_deny  # noqa: E402


def _runtime_slice_paths() -> "set[str]":
    """Operator-approved + trust-ladder-earned runtime slice (#823, #876) —
    thin env-reading wrapper around
    :func:`nanobot.runtime.promoted_overlay.effective_runtime_slice`, so the
    proposer advertises/validates against the SAME earned-rung slice the
    gate (``bridge.py``) and the root verifier use. Empty env + no earned
    rungs -> empty set (byte-identical to pre-#876 pre-#823 behaviour)."""
    return effective_runtime_slice(os.environ.get(_RUNTIME_SLICE_ENV))


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
_DEDUP_EXHAUSTION_K_ENV = "SELFEVO_DEDUP_EXHAUSTION_K"
_DEDUP_EXHAUSTION_DAYS_ENV = "SELFEVO_DEDUP_EXHAUSTION_DAYS"
_DEFAULT_DEDUP_EXHAUSTION_K = 2
_DEFAULT_DEDUP_EXHAUSTION_DAYS = 3

# Set only for the duration of a propose() call.  maybe_propose uses this
# small process-local signal to distinguish a failed gateway call from a
# response that arrived but was not valid JSON, without changing propose()'s
# existing dict-or-None public contract.
_last_propose_failure: str | None = None

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

# #902: assigned-demand rotation. Default ON — the proposer is steered to one
# item per cycle instead of freely choosing among everything presented; the
# kill switch restores full-list presentation (pre-#902 behavior).
_DEMAND_ROTATION_ENABLED_ENV = "SELFEVO_DEMAND_ROTATION_ENABLED"
_ROTATION_SCHEMA = "demand-rotation-v1"

# #902: saturated-themes guard. K = minimum unconfirmed same-subject scripts
# before a subject is presented as CLOSED for new scripts.
_SATURATED_THEME_K_ENV = "SELFEVO_SATURATED_THEME_K"
_DEFAULT_SATURATED_THEME_K = 3
_MAX_SATURATED_SECTION_CHARS = 1200
_MAX_SATURATED_FILES_SHOWN = 4
_SATURATED_VERB_STOPLIST = frozenset(
    {
        "check", "audit", "analyze", "monitor", "track", "validate", "verify",
        "prevent", "detect", "report", "scan", "inspect", "review",
    }
)

# #903: verb-invariant subject dedup for NEW-file proposals. Default ON —
# only "0"/"false" disable it (falls back to the pre-#903 lexical-only dedup
# in _is_duplicate_proposal). Reuses _SATURATED_VERB_STOPLIST (#902) rather
# than a second, drifting copy of the same stoplist.
_SUBJECT_DEDUP_ENABLED_ENV = "SELFEVO_SUBJECT_DEDUP_ENABLED"
_SUBJECT_DEDUP_MAX_HITS = 5
_SUBJECT_DEDUP_MAX_GLOB = 200

# #903: per-file edit budget without confirmed use. M <= 0 disables the check.
_EDIT_BUDGET_M_ENV = "SELFEVO_EDIT_BUDGET_M"
_DEFAULT_EDIT_BUDGET_M = 5

_PROPOSER_SYSTEM_PROMPT = (
    "You are proposing exactly ONE small, bounded engineering improvement for a "
    "self-evolving codebase. Reply with ONLY a JSON object with keys "
    "task_title, rationale, target_path, serves — no prose, no markdown code "
    "fences. Optionally also include expected_outcome: {\"claim\": \"<short "
    "falsifiable statement of what this change should achieve>\", \"check\": "
    "{\"kind\": \"script_exit_zero\"|\"test_count_increase\"|\"file_exists\"|"
    "\"free_text\", ...}} — omit it entirely if you cannot state a falsifiable "
    "claim; it is never required. task_title must be non-empty and at most 120 characters, "
    "describing a single behavior/bug (not a bundle). target_path must name "
    "exactly ONE path (file or directory) under one of these mutable "
    "surfaces: surfaces/, scripts/, memory/, lessons/, docs/, tests/, skills/ — no "
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
    "target_path, serves — no prose, no markdown code fences. Optionally also "
    "include expected_outcome: {\"claim\": \"<short falsifiable statement of "
    "what this change should achieve>\", \"check\": {\"kind\": \"script_exit_zero\"|"
    "\"test_count_increase\"|\"file_exists\"|\"free_text\", ...}} — omit it entirely "
    "if you cannot state a falsifiable claim; it is never required. task_title "
    "must be non-empty and at most 120 characters, describing a single "
    "behavior/bug (not a bundle). target_path must name exactly ONE path "
    "(file or directory) under one of these mutable surfaces: surfaces/, "
    "scripts/, memory/, lessons/, docs/, tests/, skills/ — no other path is "
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


def _release_root_from_env() -> Path:
    """Resolve the immutable release tree, independently of writable workspace."""
    configured = os.environ.get("RELEASE_ROOT", "").strip()
    return Path(configured or _RELEASE_ROOT_DEFAULT)


def _load_goal_text(state_dir: Path, release_root: "Path | None" = None) -> str:
    """Assembled goal text for the proposer context.

    #944: reads the immutable operator charter from ``goals.md`` in the
    release tree (``release_root`` arg or ``RELEASE_ROOT`` env var).
    Derived priorities from ``state/goals/derived_priorities.json`` are
    folded in by :func:`goal_review.merged_goal_text` (#860). Falls back
    to the pre-#944 behavior (``goal_text.json`` in state dir holds the
    full text) when ``goals.md`` is absent.
    """
    from nanobot.runtime.goal_review import read_charter_text

    # Resolve release root: explicit arg wins, then RELEASE_ROOT/default.
    if release_root is None:
        release_root = _release_root_from_env()

    charter = read_charter_text(release_root)
    if charter:
        raw_text = charter
    else:
        # Legacy fallback: goal_text.json holds the full text (charter + priorities).
        state_path = Path(state_dir) / "goals" / "goal_text.json"
        if not state_path.is_file():
            return ""
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        raw_text = str(data.get("text") or "")

    if not raw_text:
        return ""

    # #860/#944: fold in goal_review's harness-owned derived priorities —
    # the sidecar deploy_release.sh never touches — so a deploy's reseed
    # can't erase an already-accepted priority out from under the proposer's
    # context or should_propose's filter_completed path.
    try:
        from nanobot.runtime import goal_review

        raw_text = goal_review.merged_goal_text(state_dir, raw_text)
    except Exception:
        pass
    return raw_text


def _priorities_remain(filtered_goal_text: str) -> bool:
    marker = "Current priority targets:"
    idx = filtered_goal_text.find(marker)
    if idx == -1:
        return False
    section = filtered_goal_text[idx + len(marker):]
    return bool(_PRIORITY_PATTERN.search(section))


# #1175: the proposer's ledger-derived signals (recent titles, no-op streak,
# self-dedup saturation, dedup exhaustion) read this many days across the live
# file and its rotated archives, so none of them resets at the 00:00 UTC
# rotation (observed live at 00:10 UTC; the live file then holds ~3 rows).
_LEDGER_HORIZON_DAYS = 3


def _load_ledger_rows(state_dir: Path, *, days: int = _LEDGER_HORIZON_DAYS) -> list[dict[str, Any]]:
    """Rows of the last ``days`` via ``state_access.ledger_window``, oldest
    first. Fail-open to ``[]`` when the window is unavailable — every consumer
    here treats an empty list as "no streak / no saturation / nothing recent",
    which is the less aggressive reading."""
    from nanobot.runtime.state_access import ledger_window

    since = datetime.now(timezone.utc) - timedelta(days=days)
    return list(ledger_window(Path(state_dir), since_ts=since.isoformat().replace("+00:00", "Z")).rows)


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
        # #944: the pre-#760 supply path also works when goals.md exists
        # at the release root (RELEASE_ROOT env), even without goal_text.json.
        _release_root = _release_root_from_env()
        _has_goals_md = (_release_root / "goals.md").is_file()
        if not goal_text_path.is_file() and not _has_goals_md:
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
    """Render recent outcomes with their proposed task meaning when available.

    The outcome row is joined to its same-cycle ``proposed`` row. Legacy or
    planner-authored outcomes without a proposal retain the branch fallback.
    """
    tail = _terminal_rows(rows)[-n:]
    title_by_cycle: dict[str, tuple[str, str]] = {}
    for row in _proposed_rows(rows):
        cycle_id = str(row.get("cycle_id") or "").strip()
        title = str(row.get("task_title") or "").strip()
        target = str(row.get("target_path") or "").strip()
        if cycle_id and title:
            title_by_cycle[cycle_id] = (title, target)

    lines: list[str] = []
    for row in tail:
        outcome = str(row.get("outcome") or "unknown")
        cycle_id = str(row.get("cycle_id") or "").strip()
        proposal = title_by_cycle.get(cycle_id)
        if proposal:
            title, target = proposal
            detail = f"{title[:120]} [{target}]" if target else title[:140]
        else:
            reason = str(row.get("reason") or "").strip()
            detail = reason or str(row.get("branch") or cycle_id or "").strip() or "(no detail)"
        lines.append(f"{outcome}: {detail}"[:160])
    return lines


# #862: harness-usage-evidence annotation for the inventory section. #840
# made the inventory relevance-RANK against the current demand/query, but
# the proposer still could not SEE which surviving entries are
# proven-useful vs never touched — so it had no signal to prefer extending
# a proven tool over minting a new one-shot (the behavioral root of a
# confirmed_integration_ratio stuck around 0.07 against a 0.5 target,
# instrumentation-exhausted). This block decorates each line with a tag
# derived ONLY from the harness-owned usage-evidence sidecar
# (<state_dir>/usage/last_used.json, #789-protected FITNESS_SIDECARS member
# "usage/last_used.json", written by usage_evidence.refresh_usage) — the
# instance cannot forge these tags through this read-only path, and a
# misleading hint only steers the prompt; every proposal still passes the
# full gate unchanged. Steering only, no gate/fitness/scorecard changes.
_INVENTORY_STEERING_LINE = (
    "Prefer EXTENDING a verified-used tool ([used:output], [used:reference], or another behavioral signal) over creating a new file when the task allows."
)


def _load_inventory_usage_entries(state_dir: Path | None) -> dict[str, Any]:
    """Single, as-is read of the usage-evidence sidecar's ``entries`` map for
    inventory annotation. Read-only — never triggers a rescan (the sidecar
    is kept fresh elsewhere on its own 6h/HEAD watermark, see
    :mod:`nanobot.runtime.usage_evidence`). Fail-open to ``{}`` on a missing,
    unreadable, or malformed file — that degrades to "no annotations,
    byte-identical to pre-#862 output", never a raise.
    """
    if state_dir is None:
        return {}
    try:
        path = Path(state_dir) / "usage" / "last_used.json"
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else None
        return entries if isinstance(entries, dict) else {}
    except Exception:
        return {}


def _parse_inventory_ts(value: Any) -> datetime | None:
    """Fail-open ISO-timestamp parse: a missing/malformed value reads as
    absent (``None``) rather than raising, per #862's "malformed timestamp
    -> treated as absent" spec."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _inventory_days_ago(ts: datetime, now: datetime) -> int:
    """Whole days between ``ts`` and ``now``, clamped to 0 (never negative —
    a clock-skewed/future timestamp reads as "0d ago" rather than a
    confusing negative)."""
    try:
        return max((now - ts).days, 0)
    except Exception:
        return 0


def _annotate_inventory_line(
    line: str, rel: str, usage_entries: dict[str, Any], now: datetime,
) -> str:
    """Append one compact usage-evidence tag to ``line``: ``[used:<signal>
    <N>d ago]`` when the harness observed the artifact being consumed,
    ``[edited <N>d ago, never used]`` when it was only ever touched, or
    ``[no usage evidence]`` when the sidecar has nothing on it at all (entry
    absent, or present but with no parseable timestamp of either kind).
    Appended at the line's END so existing substring assertions on the
    ``path — description`` prefix are unaffected."""
    entry = usage_entries.get(rel)
    if isinstance(entry, dict):
        last_used = _parse_inventory_ts(entry.get("last_used"))
        if last_used is not None:
            signal = str(entry.get("signal") or "").strip() or "unknown"
            if signal.lower() == "pycache":
                return f"{line} [unverified {_inventory_days_ago(last_used, now)}d ago]"
            return f"{line} [used:{signal} {_inventory_days_ago(last_used, now)}d ago]"
        last_touched = _parse_inventory_ts(entry.get("last_touched"))
        if last_touched is not None:
            return f"{line} [edited {_inventory_days_ago(last_touched, now)}d ago, never used]"
    return f"{line} [no usage evidence]"


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

        def _rel_for_line(line: str) -> str:
            try:
                return line[2:].split(" — ", 1)[0].strip()
            except Exception:
                return ""

        total = len(lines)
        if total > _MAX_INVENTORY_ENTRIES:
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
                # Defensive final clamp (#840 review): even if `ordered` ever
                # exceeds the cap (e.g. related_scripts' limit raised above
                # _MAX_INVENTORY_ENTRIES), never emit more than the cap.
                lines = (ordered + remaining[:slots])[:_MAX_INVENTORY_ENTRIES]
            else:
                lines = sorted(lines, key=_mtime_for_line, reverse=True)[:_MAX_INVENTORY_ENTRIES]

            note = f"({total} scripts total; showing the {_MAX_INVENTORY_ENTRIES} most recently modified)"
            prefix = note + "\n"
        else:
            prefix = ""

        # #862: decorate surviving lines with harness-observed usage
        # evidence — one sidecar read for the whole call, fail-open to {}
        # (absent/unreadable sidecar -> no annotations, output byte-identical
        # to pre-#862). The relevance-ranking and cap logic above are
        # unchanged; this only decorates the lines that already survived it.
        usage_entries = _load_inventory_usage_entries(state_dir)
        if usage_entries:
            now = datetime.now(timezone.utc)
            lines = [
                _annotate_inventory_line(line, _rel_for_line(line), usage_entries, now)
                for line in lines
            ]

        section = prefix + "\n".join(lines)
        if usage_entries:
            section = _INVENTORY_STEERING_LINE + "\n\n" + section

        if len(section) > _MAX_INVENTORY_CHARS:
            # #862 review: cut at a line boundary so an annotation tag is
            # never half-truncated ("[used:pyc"). Annotations inflate line
            # length, so the char cap drops a few more TAIL entries than
            # pre-#862 — accepted: tail = lowest-relevance (#840 ranking),
            # and the steering value lives on the top-ranked lines.
            cut = section.rfind("\n", 0, _MAX_INVENTORY_CHARS)
            section = section[:cut] if cut > 0 else section[:_MAX_INVENTORY_CHARS]
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


def _rotation_enabled() -> bool:
    """#902 kill switch: default ON; only "0"/"false" disable it (falls back
    to presenting the full demand list, byte-identical to pre-#902)."""
    raw = os.environ.get(_DEMAND_ROTATION_ENABLED_ENV, "1").strip().lower()
    return raw not in ("0", "false")


def _rotation_path(state_dir: Path) -> Path:
    return Path(state_dir) / "demand" / "rotation.json"


def _load_rotation(state_dir: Path) -> dict[str, Any]:
    """Load ``rotation.json``; a MISSING file (the common, expected "first
    cycle ever" case) reads as "nothing served yet". Deliberately does NOT
    swallow a read/parse error itself — an unreadable or malformed file
    propagates to :func:`_select_assigned_demand`'s own fail-open wrapper,
    which then returns the full original demand list rather than silently
    resetting rotation state and picking as if nothing had ever been
    served."""
    path = _rotation_path(state_dir)
    if not path.is_file():
        return {"schema_version": _ROTATION_SCHEMA, "served": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("served"), dict):
        return {"schema_version": _ROTATION_SCHEMA, "served": {}}
    return data


def _write_rotation(state_dir: Path, data: dict[str, Any]) -> None:
    """Write-temp-then-``os.replace`` so a crash mid-write never leaves a
    half-written ``rotation.json`` behind for the next cycle to trip over."""
    path = _rotation_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def _select_assigned_demand(
    state_dir: Path, demand_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """#902: pick ONE demand item via least-recently-served rotation.

    State lives in ``state_dir/demand/rotation.json`` — ``{"schema_version":
    "demand-rotation-v1", "served": {"<demand_id>": "<iso-ts>"}}``. The item
    whose id is absent from ``served`` wins first; when every presented id
    has already been served, the OLDEST-stamped one wins, ties broken by
    original list order (the list is already trust-ordered — priority >
    defect > goal-gap > hypothesis > decay, see :func:`demand.collect_demand`).

    Selecting stamps ``served[id] = now`` and persists immediately —
    stamping happens at PRESENTATION time, so a skip, a rejection, or a
    success all equally advance the rotation (the #902 acceptance criterion
    that ``no_valuable_task`` must not stall the loop on one unservable
    item). Stale ids (no longer present in ``demand_items``) are pruned from
    ``served`` on every call, keeping the file bounded.

    Fail-open: returns ``demand_items`` UNCHANGED (the original object, by
    reference — callers use this to detect "rotation did not run") on any
    error, on an empty input, or when the kill switch
    (:data:`_DEMAND_ROTATION_ENABLED_ENV`) is off.
    """
    if not demand_items:
        return demand_items
    if not _rotation_enabled():
        return demand_items
    try:
        state_dir = Path(state_dir)
        valid_items = [
            item for item in demand_items if isinstance(item, dict) and item.get("id")
        ]
        if not valid_items:
            return demand_items

        data = _load_rotation(state_dir)
        served: dict[str, str] = dict(data.get("served") or {})

        current_ids = {str(item["id"]) for item in valid_items}
        served = {k: v for k, v in served.items() if k in current_ids}

        unserved = [item for item in valid_items if str(item["id"]) not in served]
        if unserved:
            selected = unserved[0]
        else:
            def _served_ts_key(item: dict[str, Any]) -> datetime:
                ts = _parse_inventory_ts(served.get(str(item["id"])))
                return ts or datetime.min.replace(tzinfo=timezone.utc)

            # min() keeps the FIRST minimal element on ties, matching the
            # "tie-break: first in list order" spec.
            selected = min(valid_items, key=_served_ts_key)

        served[str(selected["id"])] = datetime.now(timezone.utc).isoformat()
        _write_rotation(state_dir, {"schema_version": _ROTATION_SCHEMA, "served": served})

        return [selected]
    except Exception:
        return demand_items


def _saturated_theme_k() -> int:
    """#902: ``SELFEVO_SATURATED_THEME_K`` env override for K, default 3;
    unset, empty, non-numeric, or non-positive falls back to the default."""
    raw = os.environ.get(_SATURATED_THEME_K_ENV, "").strip()
    if not raw:
        return _DEFAULT_SATURATED_THEME_K
    try:
        value = int(raw)
    except Exception:
        return _DEFAULT_SATURATED_THEME_K
    return value if value > 0 else _DEFAULT_SATURATED_THEME_K


def _saturated_subject_key(stem: str) -> str:
    """Subject key for a script filename stem (#902): split on ``_``, drop
    the leading verb token when it's in the fixed stoplist, rejoin the rest.
    ``check_repeat_failures`` and ``audit_repeat_failures`` both key to
    ``repeat_failures`` — the whole point (they are the SAME subject under a
    different verb). Falls back to the untouched stem when dropping the verb
    would leave nothing (e.g. a bare ``check.py``)."""
    tokens = stem.split("_")
    if len(tokens) > 1 and tokens[0].lower() in _SATURATED_VERB_STOPLIST:
        tokens = tokens[1:]
    key = "_".join(t for t in tokens if t)
    return key or stem


def _saturated_themes_section(state_dir: Path, selfevo_repo: Path | None) -> str:
    """#902: bounded ``## Saturated themes`` guardrail — subjects with ``>=
    K`` top-level ``scripts/*.py`` files that the usage-evidence sidecar
    (:func:`_load_inventory_usage_entries`, #862) never recorded a
    ``last_used`` for, meaning nothing ever confirmed they are actually
    used. Steering only: the model is told not to mint a new script for a
    CLOSED subject, but nothing here blocks or gates anything.

    Deliberately narrow scope, matching the motivating #902 case
    (``check_repeat_failures.py`` / ``audit_repeat_failures.py`` /
    ``analyze_repeat_failures.py`` / ``prevent_repeat_failures.py``):
    top-level ``scripts/`` only (no recursion), ``__init__.py`` and test
    files (``test_*.py``, ``conftest.py``) excluded — they are not
    proposable "new script" targets in the first place.

    Capped at :data:`_MAX_SATURATED_SECTION_CHARS` (cut at a line boundary,
    same discipline as the other bounded sections); each theme lists at most
    :data:`_MAX_SATURATED_FILES_SHOWN` filenames, then ``"…"``.

    Fail-open: returns ``""`` on any error, when ``selfevo_repo`` is not
    given, when ``scripts/`` does not exist, or when nothing is saturated.
    """
    if not selfevo_repo:
        return ""
    try:
        repo = Path(selfevo_repo)
        scripts_dir = repo / "scripts"
        if not scripts_dir.is_dir():
            return ""
        usage_entries = _load_inventory_usage_entries(state_dir)

        by_subject: dict[str, list[str]] = {}
        for path in sorted(scripts_dir.glob("*.py")):
            name = path.name
            if name == "__init__.py" or name == "conftest.py" or name.startswith("test_"):
                continue
            entry = usage_entries.get(f"scripts/{name}")
            if isinstance(entry, dict) and entry.get("last_used"):
                continue  # confirmed used — never counts toward saturation
            key = _saturated_subject_key(path.stem)
            by_subject.setdefault(key, []).append(name)

        k = _saturated_theme_k()
        lines: list[str] = []
        for subject in sorted(by_subject):
            files = by_subject[subject]
            if len(files) < k:
                continue
            shown = files[:_MAX_SATURATED_FILES_SHOWN]
            files_text = ", ".join(shown)
            if len(files) > _MAX_SATURATED_FILES_SHOWN:
                files_text += ", …"
            lines.append(
                f"- {subject}: {len(files)} scripts with no confirmed usage "
                f"({files_text}) — do NOT propose new scripts for this "
                "subject; propose confirmed-use follow-ups for an existing "
                "one, or work on a DIFFERENT subject."
            )
        if not lines:
            return ""

        section = "## Saturated themes (subjects CLOSED for new scripts)\n" + "\n".join(lines)
        if len(section) > _MAX_SATURATED_SECTION_CHARS:
            cut = section.rfind("\n", 0, _MAX_SATURATED_SECTION_CHARS)
            section = section[:cut] if cut > 0 else section[:_MAX_SATURATED_SECTION_CHARS]
        return section
    except Exception:
        return ""


def _stepping_stones_section(state_dir: Path) -> str:
    """#844: render the diversity archive as optional stepping-stones the
    proposer MAY extend to explore a different area (escape greedy single-
    lineage). Returns '' when empty or on any error (fail-open)."""
    try:
        stones = archive.read_stepping_stones(state_dir)
        lines: list[str] = []
        for stone in stones:
            if not isinstance(stone, dict):
                continue
            signature = str(stone.get("signature") or "").strip()
            summary = str(stone.get("summary") or "").strip()
            cycle_id = str(stone.get("cycle_id") or "").strip()
            if not signature:
                continue
            line = f"- {signature} — {summary} (cycle {cycle_id})"
            lines.append(line)
        if not lines:
            return ""
        return "\n".join(lines)
    except Exception:
        return ""


def _captured_pattern_hint(ledger_rows: list[dict[str, Any]]) -> str:
    """Return a deterministic hint when the same target path repeats."""
    counts: dict[str, int] = {}
    for row in ledger_rows[-20:]:
        if str(row.get("phase") or row.get("event") or "") != "outcome":
            continue
        if str(row.get("outcome") or "") != "success":
            continue
        paths = row.get("files_changed", []) if isinstance(row.get("files_changed"), list) else []
        row_paths = set()
        for path in paths:
            rel = str(path).replace("\\", "/").strip()
            if rel.startswith(("scripts/", "skills/", "surfaces/")):
                row_paths.add(rel)
        for rel in row_paths:
            counts[rel] = counts.get(rel, 0) + 1
    repeated = sorted(path for path, count in counts.items() if count >= 2)
    if not repeated:
        return ""
    return "Repeated successful work touched " + ", ".join(repeated[:3]) + "; bundle this repeated pattern as a skill."


def build_context(
    state_dir: Path,
    selfevo_repo: Path | None,
    *,
    force_proposal: bool = False,
    demand_items: list[dict[str, str]] | None = None,
    assigned: bool = False,
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

    ``assigned`` (#902, default ``False`` — every existing call site and
    golden-prompt test is byte-identical unless it opts in): when the caller
    (``maybe_propose``) has run :func:`_select_assigned_demand` and narrowed
    ``demand_items`` to the ONE rotation-picked item, it passes
    ``assigned=True`` and the ``## Demand`` instruction wording changes from
    "select one of these" to "you are ASSIGNED this one item — propose only
    work for it, or reply no_valuable_task". Ignored when ``demand_items``
    is empty/absent.

    Fail-open: returns an empty string on any error.
    """
    try:
        state_dir = Path(state_dir)
        now = datetime.now(timezone.utc)
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
            "one of: " + ", ".join(_ALLOWED_PATH_PREFIXES) + " — no other path is acceptable."
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
        captured_hint = _captured_pattern_hint(ledger_rows)
        guardrail_parts = [
            "",
            "## Recently proposed (window: last 3 days; rejected as duplicates — do NOT propose these themes again)",
            "\n".join(f"- {title}" for title in recent_proposed_titles) or "(none yet)",
        ]
        if captured_hint:
            guardrail_parts += ["", "## CAPTURED pattern hint (steering only)", captured_hint]
        # #958: warn about re-creation of recently-retired skill paths.
        try:
            _cooldown_paths = demand.retired_skill_paths_in_cooldown(state_dir, now)
            if _cooldown_paths:
                _warn_lines = [
                    f"- {p} (retired {ts[:10]})" for p, ts in sorted(_cooldown_paths.items())
                ]
                guardrail_parts += [
                    "",
                    "## WARNING: recently-retired skill paths (re-creation not recommended)",
                    "\n".join(_warn_lines),
                ]
        except Exception:
            pass
        # #716: only appended when non-empty — with no recent failures this section
        # is absent (keeps output byte-identical to pre-#716 on that axis).
        if recent_failed_titles:
            guardrail_parts += [
                "",
                "## Recently attempted but NOT integrated (window: last 3 days; failed/rejected — "
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

        # #844: PROTECTED (never-truncated) stepping-stones section — optional
        # diversity archive entries the model MAY extend instead of repeating
        # the greedy single-lineage path. Omitted entirely when empty, keeping
        # context byte-identical to pre-#844 in that case.
        _stones = _stepping_stones_section(state_dir)
        if _stones:
            context += (
                "\n\n## Stepping stones (validated variants — you MAY extend "
                "one to explore a different area instead of repeating the "
                "greedy path)\n" + _stones
            )

        if demand_items:
            demand_body = _demand_section(demand_items)
            if demand_body:
                if assigned and len(demand_items) == 1 and isinstance(demand_items[0], dict):
                    assigned_id = str(demand_items[0].get("id") or "<id>")
                    instruction = (
                        "\n\nThis cycle you are ASSIGNED this one item — "
                        "propose ONLY a bounded task that serves it; set "
                        f"serves to the demand id (e.g. 'demand {assigned_id}'). "
                        "If nothing bounded/valuable remains for it, reply "
                        "no_valuable_task.\n\n"
                    )
                else:
                    instruction = (
                        "\n\nSelect ONE demand item above and propose a bounded "
                        "task that addresses it; set serves to the demand id "
                        "(e.g. 'demand defect-1a2b3c4d5e6f'). If no demand item "
                        "is addressable, reply no_valuable_task.\n\n"
                    )
                context = (
                    "## Demand (the ONLY valid work sources this cycle)\n"
                    + demand_body
                    + instruction
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
            saturated_section = _saturated_themes_section(state_dir, selfevo_repo)
        except Exception:
            saturated_section = ""
        if saturated_section:
            context += "\n\n" + saturated_section

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
    """Model used for the proposer's reasoning step (WHAT to improve).

    #897: the proposer may run on a different (stronger) model than the
    executor code-writer. Precedence: ``SELFEVO_PROPOSER_MODEL`` when set
    (non-empty), else ``SUBAGENT_BRIDGE_MODEL`` (the executor's knob,
    reused as the default so the proposer keeps working unconfigured),
    else the built-in ``cl/gemini-3.5-flash-low`` default. The executor
    itself always reads ``SUBAGENT_BRIDGE_MODEL`` directly and is
    unaffected by this knob.

    #899: precedence now lives centrally in
    :func:`nanobot.runtime.model_registry.resolve_model` — this wrapper is
    kept for the existing call sites/tests, behavior unchanged.
    """
    return resolve_model("proposer", strip_openai=True)


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


def _dedup_exhaustion_k() -> int:
    try:
        return max(1, int(os.environ.get(_DEDUP_EXHAUSTION_K_ENV, _DEFAULT_DEDUP_EXHAUSTION_K)))
    except ValueError:
        return _DEFAULT_DEDUP_EXHAUSTION_K


def _dedup_exhaustion_days() -> int:
    try:
        return max(1, int(os.environ.get(_DEDUP_EXHAUSTION_DAYS_ENV, _DEFAULT_DEDUP_EXHAUSTION_DAYS)))
    except ValueError:
        return _DEFAULT_DEDUP_EXHAUSTION_DAYS


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _dedup_exhausted(state_dir: Path, demand_id: str) -> bool:
    """Return whether this assigned demand has recent self-dedup exhaustion."""
    if not demand_id:
        return False
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_dedup_exhaustion_days())
        count = 0
        for row in reversed(_load_ledger_rows(state_dir, days=max(_LEDGER_HORIZON_DAYS, _dedup_exhaustion_days()))):
            if row.get("phase") != "proposer_reject":
                continue
            if row.get("reason") != "self_dedup" or str(row.get("demand_id") or "").strip() != demand_id:
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is not None and ts < cutoff:
                break
            count += 1
            if count >= _dedup_exhaustion_k():
                return True
        return False
    except Exception:
        return False


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
    global _last_propose_failure
    _last_propose_failure = None
    try:
        from openai import OpenAI
    except Exception as exc:
        _last_propose_failure = type(exc).__name__
        return None
    base_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    api_key = os.environ.get("LITELLM_API_KEY", "").strip()
    if not base_url or not api_key:
        _last_propose_failure = "MissingGatewayConfiguration"
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
        call_start = time.monotonic()
        response = client.chat.completions.create(**create_kwargs)
        duration_ms = (time.monotonic() - call_start) * 1000
        usage_obj = getattr(response, "usage", None)
        usage = {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
        }
        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", "") or ""
        model = str(getattr(response, "model", "") or create_kwargs["model"])
        content = getattr(getattr(choice, "message", None), "content", "") or ""
        try:
            with call_context(None, "proposer"):
                record_llm_call(
                    model=model, duration_ms=duration_ms, usage=usage,
                    finish_reason=finish_reason, retries=0,
                )
                record_llm_prompt(
                    messages=create_kwargs["messages"], content=content,
                    reasoning_content=None, finish_reason=finish_reason, model=model,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                )
        except Exception:
            pass
        reply = content
    except Exception as exc:
        _last_propose_failure = f"{type(exc).__name__}: {exc}"
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


_MAX_CLAIM_CHARS = 300
_VALID_CHECK_KINDS = frozenset({
    "script_exit_zero", "test_count_increase", "file_exists", "free_text",
})


def _sanitized_expected_outcome(proposal: dict[str, Any]) -> dict[str, Any] | None:
    """#1118: extract and validate the OPTIONAL ``expected_outcome`` claim
    from a raw proposal reply, returning a small sanitized dict ready to
    freeze into the artifact, or ``None`` when absent/malformed. Never
    rejects the whole proposal — an invalid ``expected_outcome`` is simply
    dropped (fail-open), since B is optional and steering-only per the
    issue's acceptance criteria ("artifacts without it remain valid").

    Shape: ``{"claim": "<non-empty string, capped>", "check": {"kind": ...}}``
    — ``check`` itself is optional; when present, only ``kind`` (one of
    :data:`_VALID_CHECK_KINDS`) is required, any other keys the model added
    (e.g. a path/threshold for its chosen kind) are carried through
    verbatim but capped in total size so a malformed/huge reply can't bloat
    the artifact.
    """
    raw = proposal.get("expected_outcome")
    if not isinstance(raw, dict):
        return None
    claim = str(raw.get("claim") or "").strip()
    if not claim:
        return None
    result: dict[str, Any] = {"claim": claim[:_MAX_CLAIM_CHARS]}
    check = raw.get("check")
    if isinstance(check, dict):
        kind = str(check.get("kind") or "").strip()
        if kind in _VALID_CHECK_KINDS:
            try:
                sanitized_check = json.loads(json.dumps(check, ensure_ascii=False)[:_MAX_CLAIM_CHARS * 2])
            except Exception:
                sanitized_check = {"kind": kind}
            if isinstance(sanitized_check, dict) and sanitized_check.get("kind") == kind:
                result["check"] = sanitized_check
            else:
                result["check"] = {"kind": kind}
    return result


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
    # #944: goals.md is the immutable operator charter — explicitly rejected
    # here before the prefix check runs, independent of where it appears.
    _norm_target = target_path.replace("\\", "/")
    _target_basename = _norm_target.rsplit("/", 1)[-1] if "/" in _norm_target else _norm_target
    if _is_blocked_filename(_norm_target):
        return False, f"target_path matches a blocked filename pattern: {target_path}"
    if _target_basename in _BLOCKED_EXACT_PATHS or _norm_target in _BLOCKED_EXACT_PATHS:
        return False, f"target_path is an immutable file that proposals may never modify: {target_path}"
    if _norm_target == "AGENTS.md":
        return False, "operator_owned_path"
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

    Unlike :func:`goal_text_utils._recent_git_log`'s 14-day window, this is the
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


def _subject_dedup_enabled() -> bool:
    """#903 kill switch: default ON; only "0"/"false" disable it (falls back
    to the pre-#903 lexical-only dedup)."""
    raw = os.environ.get(_SUBJECT_DEDUP_ENABLED_ENV, "1").strip().lower()
    return raw not in ("0", "false")


def _subject_tokens(text: str) -> set[str]:
    """Verb-invariant subject tokens (#903): lowercase alphabetic tokens with
    the fixed generic-verb stoplist (:data:`_SATURATED_VERB_STOPLIST`, #902)
    and short (<=2 char) tokens removed. Applied identically to the proposed
    target's filename stem and a candidate script's filename stem so that a
    verb paraphrase (check vs audit vs analyze) never changes the derived
    subject key.
    """
    tokens = re.findall(r"[a-zA-Z]+", text.lower())
    return {t for t in tokens if len(t) > 2 and t not in _SATURATED_VERB_STOPLIST}


def _subject_dedup_fallback_candidates(selfevo_repo: Path) -> list[str]:
    """#903 bounded plain-glob fallback for when the FTS existence index is
    disabled or returns no hits: top-level ``scripts/*.py`` paths only (no
    recursion), same exclusions as the #902 saturated-themes scan
    (``__init__.py``/``conftest.py``/``test_*.py`` are not proposable "new
    script" targets). Bounded to :data:`_SUBJECT_DEDUP_MAX_GLOB` entries.
    Fail-open: ``[]`` on any error.
    """
    try:
        scripts_dir = Path(selfevo_repo) / "scripts"
        if not scripts_dir.is_dir():
            return []
        out: list[str] = []
        for path in sorted(scripts_dir.glob("*.py")):
            name = path.name
            if name in ("__init__.py", "conftest.py") or name.startswith("test_"):
                continue
            out.append(f"scripts/{name}")
            if len(out) >= _SUBJECT_DEDUP_MAX_GLOB:
                break
        return out
    except Exception:
        return []


def _subject_duplicate_match(
    state_dir: Path, selfevo_repo: Path | None, proposal: dict[str, Any], title: str,
) -> str:
    """#903: verb-invariant subject dedup for NEW-file proposals.

    Complements (does not replace) the #834 exact-title permanent-novelty
    guard above: that guard only catches near-identical TITLES, so a
    paraphrase like "audit repeat failures" vs an existing
    ``analyze_repeat_failures.py`` clears the lexical word-overlap threshold
    in :func:`goal_text_utils._title_already_done_in_git_log` because the verb
    dilutes the overlap below `max(2, ceil(0.6*N))`. This check instead
    compares SUBJECT-KEY token sets (filename words with the generic-verb
    stoplist stripped) between the proposed target filename and candidate
    scripts.

    Scope (#903 review B1): only proposals whose ``target_path`` is under
    ``scripts/`` with a non-``test_`` basename are in scope. ``docs/``,
    ``memory/``, ``lessons/``, ``tests/`` targets, and ``scripts/test_*.py``
    "tests for X" proposals, are exempt — matching a tests-for-X title
    against the SCRIPT it tests would re-break the #757 test-for-X carve-out
    (a test's subject legitimately overlaps the script under test; that is
    not churn, it's coverage).

    Candidates come from :func:`existence_index.related_scripts` queried with
    the bare title (deliberately no ``target_path`` — passing one would
    trigger the #798 cross-target exemption, which is exactly the gap this
    check exists to close for genuinely NEW-file proposals), capped to
    :data:`_SUBJECT_DEDUP_MAX_HITS`. When the index is disabled or returns
    nothing, falls back to a bounded plain ``scripts/*.py`` glob
    (:func:`_subject_dedup_fallback_candidates`, capped to
    :data:`_SUBJECT_DEDUP_MAX_GLOB`) — the larger glob cap is safe precisely
    BECAUSE matching is strict equality, which cannot over-block the way a
    subset/overlap rule would.

    #903 review M2: matching is strict, non-empty SET EQUALITY of the two
    filename stems' subject tokens — not a subset/overlap check. A
    subset-or->=2-overlap rule closes an entire subject after just ONE
    existing script (e.g. "summarize_repeat_failures_weekly" would have
    wrongly matched "repeat_failures" candidates via subset containment, and
    two single-token subjects sharing nothing but that one token would
    wrongly match on the ">= 2 overlap" arm once title words were folded
    in). Equality keeps exactly the intended case
    (``audit_repeat_failures`` == ``analyze_repeat_failures`` -> blocked)
    while letting a genuinely broader or narrower subject through
    (``summarize_repeat_failures_weekly`` != ``repeat_failures`` -> allowed).
    The proposal's TITLE is no longer part of the match at all (it is only
    used to query the FTS index above) — simpler, and the filename is the
    only thing that actually determines what gets built.

    Returns the matched candidate's repo-relative path, or ``""`` when out
    of scope, nothing matches, disabled, or on any error (fail-open).
    """
    if not selfevo_repo or not _subject_dedup_enabled():
        return ""
    target = str(proposal.get("target_path") or "").strip()
    if not target.startswith("scripts/"):
        return ""
    basename = target.rsplit("/", 1)[-1]
    if basename.startswith("test_"):
        return ""
    try:
        proposal_tokens = _subject_tokens(Path(target).stem)
        if not proposal_tokens:
            return ""
        candidates = existence_index.related_scripts(
            state_dir, selfevo_repo, title, limit=_SUBJECT_DEDUP_MAX_HITS,
        )
        if not candidates:
            candidates = _subject_dedup_fallback_candidates(Path(selfevo_repo))
        else:
            candidates = candidates[:_SUBJECT_DEDUP_MAX_HITS]
        for path in candidates:
            if not path or path == target:
                continue
            candidate_tokens = _subject_tokens(Path(path).stem)
            if not candidate_tokens:
                continue
            if candidate_tokens == proposal_tokens:
                return path
        return ""
    except Exception:
        return ""


def _edit_budget_m() -> int:
    """#903: ``SELFEVO_EDIT_BUDGET_M`` env override for M, default 5; unset,
    empty, or non-numeric falls back to the default. ``M <= 0`` disables the
    check entirely (kill switch)."""
    raw = os.environ.get(_EDIT_BUDGET_M_ENV, "").strip()
    if not raw:
        return _DEFAULT_EDIT_BUDGET_M
    try:
        return int(raw)
    except Exception:
        return _DEFAULT_EDIT_BUDGET_M


def _git_commit_count_for_path(repo_root: Path, rel_path: str, since_iso: str | None) -> int:
    """#903: count commits touching ``rel_path`` in ``repo_root``, since
    ``since_iso`` (``None`` -> full history). Matches the subprocess style of
    :func:`goal_text_utils._recent_git_log` (10s timeout, stderr discarded).
    Fail-open: ``0`` (never blocks a cycle) on any subprocess error/timeout.
    """
    import subprocess as _sp

    git_cmd = [
        "git", "-c", f"safe.directory={repo_root}", "-C", str(repo_root),
        "log", "--oneline",
    ]
    if since_iso:
        git_cmd.append(f"--since={since_iso}")
    git_cmd += ["--", rel_path]
    try:
        out = _sp.check_output(git_cmd, stderr=_sp.DEVNULL, timeout=10).decode(errors="replace")
    except Exception:
        return 0
    return sum(1 for line in out.splitlines() if line.strip())


def _edit_budget_match(
    state_dir: Path, selfevo_repo: Path | None, proposal: dict[str, Any],
) -> tuple[str, int, str]:
    """#903: per-file edit budget without confirmed use.

    Applies only to EDIT proposals (the caller gates this on NOT
    :func:`_proposal_creates_new_file`) targeting a ``scripts/`` path. Looks
    up the target's usage-evidence sidecar entry
    (:func:`_load_inventory_usage_entries`, #862) for a ``last_used``
    timestamp; counts git commits touching the target since that timestamp
    (or the full history when never confirmed used). A confirmed use resets
    the budget automatically because the count window starts at
    ``last_used`` — no extra state file is needed.

    #903 review m1: when the file was NEVER confirmed used, the full-history
    count includes the file's own creation commit, which is not a "revision
    with no confirmed usage" — it's how the file came to exist. Subtracted
    so the reported/compared count means "edits after creation" in both
    branches (the ``last_used`` branch's ``--since`` window already excludes
    anything at/before that timestamp, so no adjustment is needed there).

    Returns ``(target_path, count, since_display)`` when the (possibly
    creation-commit-adjusted) count is ``>=`` the configured M
    (:func:`_edit_budget_m`), or ``("", 0, "")`` when under budget, when the
    check is disabled (``M <= 0``), when the target is not under
    ``scripts/``, or on any error (fail-open).
    """
    if not selfevo_repo:
        return "", 0, ""
    m = _edit_budget_m()
    if m <= 0:
        return "", 0, ""
    target = str(proposal.get("target_path") or "").strip()
    if not target or not target.startswith("scripts/"):
        return "", 0, ""
    try:
        usage_entries = _load_inventory_usage_entries(state_dir)
        entry = usage_entries.get(target)
        last_used_raw = entry.get("last_used") if isinstance(entry, dict) else None
        last_used_str = str(last_used_raw).strip() if last_used_raw else ""
        last_used_ts = _parse_inventory_ts(last_used_raw) if last_used_raw else None
        since_iso = last_used_ts.isoformat() if last_used_ts else None
        count = _git_commit_count_for_path(Path(selfevo_repo), target, since_iso)
        if since_iso is None:
            count = max(0, count - 1)  # exclude the creation commit itself
        if count >= m:
            return target, count, (last_used_str or "creation")
        return "", 0, ""
    except Exception:
        return "", 0, ""


def _refuted_hypothesis_titles(state_dir: Path) -> list[str]:
    """Titles of hypotheses the harness VERDICT-marked ``"refuted"`` (#878),
    newest ``verdict_at`` first, capped to
    :data:`hypothesis_backlog.SUPPORTED_TOP_N` entries.

    Reads ``hypotheses/lifecycle.json`` directly — the harness-owned verdict
    field written by ``hypothesis_backlog.reconcile`` from a MEASURED source
    only (see ``hypothesis_verdict.classify_hypothesis_verdict``; never
    anything the instance itself claimed). A refuted hypothesis must never
    be re-proposed under the same title: unlike #716's windowed
    ``_recent_failed_titles`` (an old failure ages out of its recency
    window), this is PERMANENT — like #834's full-history built-subject
    guard — because the harness already spent a measured experiment
    disproving the idea; re-litigating it once a window happens to expire
    would just spend another one on the same dead end.

    #878 opus-review Y2 fix: uncapped, this list only grows over a long RSI
    run, monotonically widening the word-overlap false-positive surface
    every future proposal is checked against. Bounded to the same small N
    the ``supported`` side already uses (``hypothesis_backlog.SUPPORTED_TOP_N``)
    so the surface stays flat — a title that ages out of the most-recent N
    simply stops being permanently blocked (a live-with tradeoff, not a
    behavior regression: it was never re-checked against anything BUT this
    list to begin with).

    Fail-open: a missing/corrupt ``lifecycle.json`` yields ``[]`` (never
    blocks a proposal)."""
    try:
        path = Path(state_dir) / "hypotheses" / "lifecycle.json"
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return []
        ranked: list[tuple[str, str]] = []
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("verdict") or "") != "refuted":
                continue
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            ranked.append((str(entry.get("verdict_at") or ""), title))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [title for _, title in ranked[: hypothesis_backlog.SUPPORTED_TOP_N]]
    except Exception:
        return []


def _is_duplicate_proposal(
    state_dir: Path, selfevo_repo: Path | None, proposal: dict[str, Any]
) -> tuple[bool, str, str]:
    """Pre-write self-dedup (#707 canary novelty collapse; extended by #716).

    Reuses the SAME per-line proportional word-overlap heuristic the bridge
    and deterministic planner already use for "is this title already done"
    (``goal_text_utils._title_already_done_in_git_log``), fed with three
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

    #878: a harness-VERDICT-refuted hypothesis title (:func:`_refuted_hypothesis_titles`)
    is checked separately, unconditionally (not gated on new-file creation
    like the #834 check below — a refuted hypothesis experiment need not
    have created a new file) and is a PERMANENT block, same rationale as
    #834's full-history guard.

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
        # #1184: a target on the lever surface of a futile goal gap is refused
        # before any title heuristic — the loop has already integrated
        # ``attempt_count`` changes there without moving the metric. Recorded
        # as ``proposer_reject reason=futile_surface`` by the caller.
        from nanobot.runtime import goal_gap_futility

        target = str(proposal.get("target_path") or "").strip()
        futile = goal_gap_futility.futile_surface_for(state_dir, target) if target else None
        if futile:
            return True, (
                f"your proposal targets '{target}', which lies on the lever surface of the "
                f"futile goal gap {futile.get('metric')} ({futile.get('gap_id')}): "
                f"{futile.get('attempt_count')} integrated attempts since {futile.get('first_seen_ts')} "
                f"moved the metric by {futile.get('metric_delta')}; propose work on a DIFFERENT surface"
            ), f"futile_surface:{futile.get('gap_id')}"
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

        # #878 permanent refuted-hypothesis guard: applies unconditionally
        # (not gated on _proposal_creates_new_file — a hypothesis experiment
        # may well have edited an existing file). ``matched_against`` is
        # prefixed "refuted-hypothesis:" so a ``proposer_reject`` ledger row
        # can distinguish this source at a glance while still recording
        # which title it actually matched.
        refuted_titles = _refuted_hypothesis_titles(state_dir)
        if refuted_titles:
            refuted_block = "\n".join(refuted_titles)
            if _title_already_done_in_git_log(title, refuted_block):
                matched_refuted = next(
                    (
                        t for t in refuted_titles
                        if t.strip() and _title_already_done_in_git_log(title, t)
                    ),
                    "",
                )
                return True, (
                    f"your proposal '{title}' re-proposes a hypothesis the "
                    "harness already REFUTED via measured evidence (verdict: "
                    "refuted); propose something from a DIFFERENT area, "
                    "preferring the numbered Current priority targets"
                ), f"refuted-hypothesis:{matched_refuted}"

        # #834 permanent novelty guard: for proposals that CREATE A NEW file,
        # also reject against the full commit history (not just the 14-day
        # window above), so a throwaway artifact is not silently rebuilt once
        # its creation commit ages out. Edits/improvements to an existing file
        # are iteration, not churn, and are never blocked here.
        creates_new_file = _proposal_creates_new_file(selfevo_repo, proposal)
        if creates_new_file:
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

            # #903: verb-invariant subject dedup. Complements the exact-title
            # #834 guard above — a paraphrase (check/audit/analyze) clears
            # the lexical word-overlap threshold, so this compares SUBJECT
            # tokens (generic verbs stripped) instead. Only applies to
            # NEW-file proposals; edits are handled by the edit-budget check
            # below.
            subject_match = _subject_duplicate_match(state_dir, selfevo_repo, proposal, title)
            if subject_match:
                return True, (
                    f"your proposal '{title}' duplicates the subject of "
                    f"existing script `{subject_match}`; extend "
                    f"`{subject_match}` instead of creating a new script, or "
                    "pick a different subject"
                ), f"subject-duplicate:{subject_match}"
        else:
            # #903: per-file edit budget without confirmed use. Only applies
            # to proposals that target an EXISTING scripts/ file (the #834
            # permanent novelty guard above already covers new-file churn).
            edit_path, edit_count, edit_since = _edit_budget_match(
                state_dir, selfevo_repo, proposal,
            )
            if edit_path:
                return True, (
                    f"target `{edit_path}` already has {edit_count} "
                    "revisions with no confirmed usage since "
                    f"{edit_since}; do not revise it again — demonstrate "
                    "usage of the existing version or pick a different "
                    "target"
                ), f"edit-budget:{edit_path}"
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
    restarts; since #1175 the read spans the rotated archives, so a streak
    that crosses 00:00 UTC keeps counting. Fail-open: any error or an
    unavailable window reads as ``0`` (no streak), which only ever makes the
    caller MORE willing to allow another no-op — it never forces a proposal
    from a ledger it could not read."""
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
    ``_load_ledger_rows`` 3-day window) as
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


def write_request(
    state_dir: Path, proposal: dict[str, Any], selfevo_repo: Path | None = None
) -> str:
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

    #912: ``selfevo_repo`` (optional — ``None`` from any caller that lacks
    it, e.g. legacy tests) is used to select up to one relevant error card
    and one relevant lesson card via :func:`build_lessons_context`, filling
    the ``lessons_context`` field that ``bridge.py``'s executor-prompt
    renderer has always been ready to consume. Fully fail-open: any
    lookup failure degrades to ``{}``, identical to the pre-#912 hardcode.
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
    # #1118: an OPTIONAL, FROZEN falsifiable claim — written once, here, at
    # proposal time, and NEVER rewritten afterwards (this is the only call
    # site that ever creates this artifact file). Modeled on #878's
    # hypothesis-lane claim shape: steering-only, never a hard gate.
    # Absent entirely (no key at all) when the model didn't supply one —
    # existing/older artifacts and this key's absence are equally valid, so
    # no downstream reader needs a default/backward-compat branch.
    _expected_outcome = _sanitized_expected_outcome(proposal)
    if _expected_outcome:
        artifact_payload["expected_outcome"] = _expected_outcome
    artifact_path.write_text(json.dumps(artifact_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    lessons_context = build_lessons_context(selfevo_repo, task_title, target_path)
    reflection_hints = build_reflection_hints(state_dir, task_title, target_path)
    if reflection_hints:
        lessons_context["reflection_hints"] = reflection_hints

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
        "lessons_context": lessons_context,
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
        if demand.should_escalate(state_dir, demand_id):
            escalation_model = demand.escalation_model()
            if escalation_model and demand.record_escalation(
                state_dir, demand_id, cycle_id, escalation_model
            ):
                proposed_event["escalated_model"] = escalation_model
    # #1118: carry the frozen claim's TEXT (not the full check dict) into the
    # ledger row — the reflector reads 'proposed' rows as ledger context
    # (nanobot.runtime.reflector._messages), so this is how the claim reaches
    # the reflector prompt without the reflector needing to re-read the
    # artifact file from disk. Absent entirely when no claim was made.
    if _expected_outcome and _expected_outcome.get("claim"):
        proposed_event["expected_outcome_claim"] = str(_expected_outcome["claim"])[:300]
    # #912: telemetry for the lessons-context re-close — record WHICH cards
    # (if any) were injected into this request, so effectiveness is
    # auditable from the ledger alone. Absent entirely when nothing matched
    # (today's exact ledger shape), never an empty list.
    if lessons_context:
        injected = []
        if lessons_context.get("relevant_error"):
            injected.append(f"error:{lessons_context['relevant_error'].get('id')}")
        if lessons_context.get("relevant_lesson"):
            injected.append(f"lesson:{lessons_context['relevant_lesson'].get('id')}")
        if injected:
            proposed_event["lessons_context"] = injected
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

    #902: in demand-driven mode, the collected demand items are narrowed to
    ONE rotation-picked item (:func:`_select_assigned_demand`) before
    ``build_context`` ever sees them — the model is told it is ASSIGNED that
    one item this cycle, not free to pick among everything presented.
    Rotation stamps its state at SELECTION time (inside
    ``_select_assigned_demand``, before the LLM is even called), so a skip,
    a rejection, or a success all equally advance it. Fails open to
    presenting the full list (today's pre-#902 behavior) on any error or
    when the kill switch is off.

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

        # #902: narrow the presented demand to ONE rotation-picked item
        # (least-recently-served). ``_select_assigned_demand`` returns the
        # SAME object (by reference) when it did not run — disabled,
        # empty input, or any internal error — which is how ``assigned``
        # below distinguishes "rotation actually picked this" from
        # "nothing changed, full list still stands".
        assigned = False
        if demand_mode and demand_items:
            rotated_items = _select_assigned_demand(state_dir, demand_items)
            assigned = rotated_items is not demand_items
            demand_items = rotated_items

        context = build_context(
            state_dir,
            selfevo_repo,
            force_proposal=not allow_no_op,
            demand_items=demand_items,
            assigned=assigned,
        )
        if not context:
            # #762: formerly a silent exit — a context-builder failure looked
            # identical to a healthy idle cycle in the ledger.
            _record_proposer_reject(state_dir, "empty_context")
            return None

        def _noop_skip_reason(raw_reason: str) -> str:
            # #902: when this cycle was narrowed to one assigned demand
            # item, prefix the skip reason with which item it was — a
            # no_valuable_task reply advances rotation just like a
            # proposal would (see _select_assigned_demand's stamp-at-
            # selection-time docstring), so this is the only per-cycle
            # trace of WHICH item that no-op applied to.
            reason = str(raw_reason or "")
            if assigned and demand_items:
                assigned_id = str(demand_items[0].get("id") or "") if isinstance(demand_items[0], dict) else ""
                if assigned_id:
                    return f"assigned={assigned_id}: {reason}"
            return reason

        def _call_propose(rejection_reason: str | None = None) -> dict[str, Any] | None:
            global _last_propose_failure
            _last_propose_failure = None
            try:
                if demand_mode:
                    return propose(
                        context,
                        rejection_reason=rejection_reason,
                        system_prompt=_DEMAND_PROPOSER_SYSTEM_PROMPT,
                    )
                return propose(context, rejection_reason=rejection_reason)
            except Exception as exc:
                _last_propose_failure = f"{type(exc).__name__}: {exc}"
                return None

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
        assigned_id = ""
        if assigned and demand_items and isinstance(demand_items[0], dict):
            assigned_id = str(demand_items[0].get("id") or "").strip()
        if assigned_id and _dedup_exhausted(state_dir, assigned_id):
            _record_noop_skip(state_dir, f"dedup_exhausted: demand {assigned_id}")
            return None

        proposal = _call_propose()
        calls_made += 1

        if allow_no_op and _is_noop_reply(proposal):
            _record_noop_skip(state_dir, _noop_skip_reason(str(proposal.get("reason") or "")))
            return None

        ok, reason = validate_sizing(proposal)
        if not ok and calls_made < _MAX_LLM_CALLS:
            proposal = _call_propose(rejection_reason=reason)
            calls_made += 1
            if allow_no_op and _is_noop_reply(proposal):
                _record_noop_skip(state_dir, _noop_skip_reason(str(proposal.get("reason") or "")))
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
            reject_reason = "llm_unavailable" if _last_propose_failure else ("operator_owned_path" if reason == "operator_owned_path" else "sizing_rejected")
            detail = (
                f"{_last_propose_failure}"
                if _last_propose_failure
                else _sizing_detail(reason, proposal)
            )
            _record_proposer_reject(
                state_dir,
                reject_reason,
                task_title=str((proposal or {}).get("task_title") or "") if isinstance(proposal, dict) else "",
                target_path=str((proposal or {}).get("target_path") or "") if isinstance(proposal, dict) else "",
                detail=detail,
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
                _record_noop_skip(state_dir, _noop_skip_reason(str(proposal.get("reason") or "")))
                return None
            ok, reason = validate_sizing(proposal)
            if not ok:
                # #762: the dedup retry came back mis-sized — still a sizing
                # rejection, recorded as such.
                _record_proposer_reject(
                    state_dir,
                    "operator_owned_path" if reason == "operator_owned_path" else "sizing_rejected",
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
                # #1184: a futile-surface refusal is its own ledger reason so a
                # suppressed lever surface is distinguishable from title dedup.
                "futile_surface" if str(dup_matched).startswith("futile_surface:") else "self_dedup",
                task_title=str(proposal.get("task_title") or ""),
                target_path=str(proposal.get("target_path") or ""),
                matched_against=dup_matched,
                demand_id=_proposal_demand_id(proposal),
            )
            return None

        write_request(state_dir, proposal, selfevo_repo)
        return _display_title(str(proposal.get("task_title") or ""))
    except Exception as exc:
        # #762: the catch-all safety net now leaves a trace. The recorder is
        # itself fail-open (contextlib.suppress), so this can never raise out
        # of the except block and break the bridge cycle.
        _record_proposer_reject(state_dir, "error", detail=f"{type(exc).__name__}: {exc}")
        return None
