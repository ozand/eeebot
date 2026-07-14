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

Everything here is fail-open/fail-closed by design, never raises: a broken
environment, a network error, or a malformed LLM reply degrades to "nothing
proposed this cycle" — identical to today's idle-safe behavior when the
deterministic generator has nothing to offer.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from nanobot.runtime import hypothesis_backlog, system_map
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

_MAX_CONTEXT_CHARS = 4000
_MAX_INVENTORY_CHARS = 4000
_MAX_INVENTORY_ENTRIES = 90
_LEDGER_DIGEST_ROWS = 15
_DUP_STREAK_K = 3
_MAX_TITLE_CHARS = 120
_RECENT_PROPOSED_TITLES_N = 10
_MAX_LLM_CALLS = 3
_MAX_CONSECUTIVE_NOOP_SKIPS = 3
_MAX_SERVES_CHARS = 160

_PRIORITY_PATTERN = re.compile(
    r"\([A-Za-z]\)\s*Priority\s+(\d+)\s*[—-]\s*(.+?):\s*(.+?)(?=\n\([A-Za-z]\)|\Z)",
    re.DOTALL,
)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_SERVES_PREFIXES = ("priority ", "vector 1", "vector 2", "hypothesis ")

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


def _last_k_all_duplicate(state_dir: Path, k: int = _DUP_STREAK_K) -> bool:
    terminal = _terminal_rows(_load_ledger_rows(state_dir))
    if len(terminal) < k:
        return False
    last_k = terminal[-k:]
    return all(r.get("outcome") == "skipped-duplicate" for r in last_k)


def should_propose(state_dir: Path, selfevo_repo: Path | None) -> bool:
    """Invocation policy (#707, extended by #745): fires on proven novelty
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
        goal_text_path = state_dir / "goals" / "goal_text.json"
        if not goal_text_path.is_file():
            return False
        if _queue_effectively_empty(state_dir):
            return True
        raw_goal_text = _load_goal_text(state_dir)
        filtered = filter_completed_priorities_from_goal_text(raw_goal_text, selfevo_repo)
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


def _system_map_inventory_section(selfevo_repo: Path | None) -> str:
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

    Capped at :data:`_MAX_INVENTORY_ENTRIES` entries (the most recently
    modified scripts by ``st_mtime`` when over the cap, prefixed with a
    total-count note) and :data:`_MAX_INVENTORY_CHARS` characters — kept
    separate from :data:`_MAX_CONTEXT_CHARS` so this section never eats into
    the goal_text/ledger budget.
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
            def _mtime_for_line(line: str) -> float:
                try:
                    rel = line[2:].split(" — ", 1)[0].strip()
                    return (repo / rel).stat().st_mtime
                except Exception:
                    return 0.0

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


def build_context(
    state_dir: Path, selfevo_repo: Path | None, *, force_proposal: bool = False
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

    Fail-open: returns an empty string on any error.
    """
    try:
        state_dir = Path(state_dir)
        raw_goal_text = _load_goal_text(state_dir)
        filtered_goal = filter_completed_priorities_from_goal_text(raw_goal_text, selfevo_repo)
        ledger_rows = _load_ledger_rows(state_dir)
        digest_lines = _digest_ledger(ledger_rows)
        recent_proposed_titles = _recent_proposed_titles(ledger_rows)
        surface_rule = (
            "Mutable surface rule: target_path MUST be a single path under "
            "one of: " + ", ".join(_ALLOWED_PATH_PREFIXES) + " — no other "
            "path is acceptable."
        )
        parts = [
            "## Goal (filtered — already-completed priorities removed)",
            filtered_goal.strip() or "(no goal text available)",
            "",
            "## Recent cycle outcomes (most recent last — do not repeat done/failed work)",
            "\n".join(f"- {line}" for line in digest_lines) or "(no ledger history yet)",
            "",
            "## Recently proposed (rejected as duplicates — do NOT propose these themes again)",
            "\n".join(f"- {title}" for title in recent_proposed_titles) or "(none yet)",
            "",
            surface_rule,
        ]
        context = "\n".join(parts)
        if len(context) > _MAX_CONTEXT_CHARS:
            context = context[:_MAX_CONTEXT_CHARS]

        inventory_section = _system_map_inventory_section(selfevo_repo)
        if inventory_section:
            context += (
                "\n\n## Existing scripts (do not duplicate — extend or skip instead)\n"
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


def propose(context: str, *, rejection_reason: str | None = None, timeout: float = 120.0) -> dict[str, Any] | None:
    """One chat completion via the same LiteLLM gateway the bridge uses.

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
        response = client.chat.completions.create(
            model=_model_name(),
            messages=[
                {"role": "system", "content": _PROPOSER_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=400,
            temperature=0.4,
        )
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
    if not any(target_path.startswith(prefix) for prefix in _ALLOWED_PATH_PREFIXES):
        return False, f"target_path outside allowed surfaces {_ALLOWED_PATH_PREFIXES}: {target_path}"

    ok, reason = _validate_serves(proposal)
    if not ok:
        return False, reason

    return True, ""


def _is_duplicate_proposal(
    state_dir: Path, selfevo_repo: Path | None, proposal: dict[str, Any]
) -> tuple[bool, str]:
    """Pre-write self-dedup (#707 canary novelty collapse).

    Reuses the SAME per-line proportional word-overlap heuristic the bridge
    and deterministic planner already use for "is this title already done"
    (``cycle_planning._title_already_done_in_git_log``), fed with two sources
    concatenated: the recent git log (already-DONE work) and this proposer's
    own recent ``'proposed'`` ledger titles (already-REJECTED-as-duplicate
    work, which never reaches git log since no commit is ever made for it).
    Either source matching is sufficient to flag a duplicate.

    Returns ``(True, feedback_text)`` on a match — ``feedback_text`` is meant
    to be passed as ``propose()``'s ``rejection_reason`` on retry. Fail-open:
    any error is treated as "not a duplicate".
    """
    try:
        title = str(proposal.get("task_title") or "").strip()
        if not title:
            return False, ""
        git_log = _recent_git_log(Path(selfevo_repo)) if selfevo_repo else ""
        recent_titles = _recent_proposed_titles(_load_ledger_rows(state_dir))
        combined_log = "\n".join([git_log] + recent_titles) if (git_log or recent_titles) else ""
        if combined_log and _title_already_done_in_git_log(title, combined_log):
            return True, (
                f"your proposal '{title}' duplicates already-done or "
                "recently-rejected work; propose something from a DIFFERENT "
                "area, preferring the numbered Current priority targets"
            )
        return False, ""
    except Exception:
        return False, ""


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


def write_request(state_dir: Path, proposal: dict[str, Any]) -> str:
    """Write the request JSON in the IDENTICAL shape
    ``_write_subagent_request_artifact`` (nanobot.runtime.cycle_planning)
    writes (#707 C1) — same keys, ``request_status: "queued"``. From the
    bridge's ``find_pending_request`` point of view this file is
    indistinguishable from a planner-written one.

    ``target_path``/``rationale`` are carried WITHOUT changing the request
    schema: they are embedded in a small companion artifact (the same
    ``next_bounded_candidate`` shape the deterministic planner's own
    materialized-improvement artifacts use, under a distinct
    ``llm-proposed-*`` filename so it is never mistaken for a planner
    materialization) and ``source_artifact`` points at it — an existing,
    already-optional field the bridge already dereferences.

    Also appends a ``'proposed'`` ledger row so proposer cycles are
    distinguishable from planner cycles in
    ``scripts/loop_metrics_report.py``. #751: that row also carries
    ``serves`` (the goal-alignment field, already schema-validated by
    :func:`validate_sizing` before this is ever called) so the report can
    compute a per-serves-class distribution; deliberately NOT added to the
    request ``payload`` itself, to keep the C1 request-schema-equality
    invariant with ``cycle_planning._write_subagent_request_artifact``.
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
        "task": f"{rationale}\n\nTarget path: {target_path}".strip(),
        "recommended_next_action": f"Implement and commit: {task_title} (target: {target_path})",
        "request_status": "queued",
        "profile": "bounded_execution",
        "budget": "standard",
        "source_artifact": str(artifact_path),
        "feedback_decision": None,
        "lessons_context": {},
    }
    request_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    append_event(
        state_dir,
        {
            "phase": "proposed",
            "cycle_id": cycle_id,
            "request_id": request_id,
            "task_title": task_title,
            "target_path": target_path,
            "serves": serves,
            "source_artifact": "llm_proposer",
        },
    )

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

        context = build_context(state_dir, selfevo_repo, force_proposal=not allow_no_op)
        if not context:
            return None

        calls_made = 0

        proposal = propose(context)
        calls_made += 1

        if allow_no_op and isinstance(proposal, dict) and proposal.get("no_valuable_task") is True:
            _record_noop_skip(state_dir, str(proposal.get("reason") or ""))
            return None

        ok, reason = validate_sizing(proposal)
        if not ok and calls_made < _MAX_LLM_CALLS:
            proposal = propose(context, rejection_reason=reason)
            calls_made += 1
            if allow_no_op and isinstance(proposal, dict) and proposal.get("no_valuable_task") is True:
                _record_noop_skip(state_dir, str(proposal.get("reason") or ""))
                return None
            ok, reason = validate_sizing(proposal)
        if not ok:
            return None

        dup, dup_reason = _is_duplicate_proposal(state_dir, selfevo_repo, proposal)
        if dup and calls_made < _MAX_LLM_CALLS:
            proposal = propose(context, rejection_reason=dup_reason)
            calls_made += 1
            ok, reason = validate_sizing(proposal)
            if not ok:
                return None
            dup, dup_reason = _is_duplicate_proposal(state_dir, selfevo_repo, proposal)
        if dup:
            return None

        write_request(state_dir, proposal)
        return _display_title(str(proposal.get("task_title") or ""))
    except Exception:
        return None
