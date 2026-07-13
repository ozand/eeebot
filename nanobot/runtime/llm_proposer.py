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
_LEDGER_DIGEST_ROWS = 15
_DUP_STREAK_K = 3
_MAX_TITLE_CHARS = 120
_RECENT_PROPOSED_TITLES_N = 10
_MAX_LLM_CALLS = 3

_PRIORITY_PATTERN = re.compile(
    r"\([A-Za-z]\)\s*Priority\s+(\d+)\s*[—-]\s*(.+?):\s*(.+?)(?=\n\([A-Za-z]\)|\Z)",
    re.DOTALL,
)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)

_PROPOSER_SYSTEM_PROMPT = (
    "You are proposing exactly ONE small, bounded engineering improvement for a "
    "self-evolving codebase. Reply with ONLY a JSON object with keys "
    "task_title, rationale, target_path — no prose, no markdown code fences. "
    "task_title must be non-empty and at most 120 characters, describing a "
    "single behavior/bug (not a bundle). target_path must name exactly ONE "
    "path (file or directory) under one of these mutable surfaces: "
    "surfaces/, scripts/, memory/, lessons/, docs/, tests/ — no other path is "
    "acceptable. rationale must briefly justify the change and must NOT "
    "repeat any already-done or recently-failed work described in the "
    "context below. If the goal text lists numbered 'Current priority "
    "targets', propose EXACTLY one of them (the first not yet done) "
    "VERBATIM as the task; only invent a new task when no numbered "
    "priorities remain."
)


def _enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "0").strip().lower() in _TRUTHY


def _requests_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "subagents" / "requests"


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
    """Anti-stacking guard: is there already a queued proposer-written request?

    Deliberately narrower than "any queued request" (#707 canary finding):
    in production the deterministic planner mints stale duplicate requests
    faster than the bridge consumes them, so the queue never empties and a
    "no queued request at all" clause meant the proposer could never fire —
    a queue full of planner duplicates IS novelty exhaustion, not a reason to
    stay silent. Only a request this module already queued blocks a new one,
    preventing unbounded proposer stacking while still firing on planner-only
    queues.
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
        if status in ("queued", "pending") and _is_proposer_request(req):
            return True
    return False


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
    """Invocation policy (#707): fires only on proven novelty exhaustion.

    ``(no queued proposer request) AND (filtered goal_text has no remaining
    "Current priority targets" entries OR the last K=3 terminal ledger
    outcome rows are all "skipped-duplicate")``. The queue clause is an
    anti-stacking guard on the proposer's OWN requests only (see
    ``_has_queued_proposer_request``) — a queued planner request no longer
    blocks proposing, since a queue full of stale planner duplicates is
    itself the novelty-exhaustion signal this function exists to catch.
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


def build_context(state_dir: Path, selfevo_repo: Path | None) -> str:
    """Compact, bounded proposer context (#707 C3).

    Exactly two read-only inputs, kept separate: the filtered (done-items
    stripped) goal_text, and a bounded digest of the last N terminal ledger
    rows (done/failure signal, so the LLM does not re-propose already-
    handled work). Hard-capped to ~4000 chars total. Fail-open: returns an
    empty string on any error.
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


def validate_sizing(proposal: dict[str, Any] | None) -> tuple[bool, str]:
    """Pre-spawn checkable sizing (#707 C2). Returns ``(ok, reason)``."""
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
    ``scripts/loop_metrics_report.py``.
    """
    state_dir = Path(state_dir)
    cycle_id = f"cycle-{uuid.uuid4().hex[:12]}"
    goal_id = _active_goal_id(state_dir)
    task_title = str(proposal.get("task_title") or "").strip()
    rationale = str(proposal.get("rationale") or "").strip()
    target_path = str(proposal.get("target_path") or "").strip()

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
        if not should_propose(state_dir, selfevo_repo):
            return None

        context = build_context(state_dir, selfevo_repo)
        if not context:
            return None

        calls_made = 0

        proposal = propose(context)
        calls_made += 1
        ok, reason = validate_sizing(proposal)
        if not ok and calls_made < _MAX_LLM_CALLS:
            proposal = propose(context, rejection_reason=reason)
            calls_made += 1
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
