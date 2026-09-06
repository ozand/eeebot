"""Per-LLM-call telemetry: one JSONL line per call through ``chat_with_retry``.

Issue #675: the operator wants visibility into per-model speed/cost and how
much of a self-evolving cycle's wall-time is actually spent waiting on the
LLM. ``nanobot.providers.base.LLMProvider.chat_with_retry`` is the single
choke point every LLM call in nanobot goes through, so that's where this
module is hooked in.

This module deliberately does very little:

- a ``contextvars.ContextVar`` that entry points (bridge/tool_harness/
  coordinator) set so calls can be attributed to a ``cycle_id``/``component``;
- ``record_llm_call()``, which appends ONE JSON line to a daily-rotated file.

No OpenTelemetry, no metrics DB, no cost computation (pricing lives in the
LiteLLM proxy — this JSONL complements the proxy's own spend/latency logs by
adding the cycle_id/component context the proxy lacks). Everything here is
best-effort: a telemetry failure must never break the actual LLM call.

Issue #693 extends this with ``record_llm_prompt()``: the token-count
telemetry above answers "how much/how long" but not "what's actually in the
~23k prompt tokens" — this records the full assembled ``messages`` + response
per call (gzip-archived, retention-pruned) so the large subagent context can
actually be inspected (see ``scripts/llm_prompt_inspect.py``).
"""

from __future__ import annotations

import contextlib
import copy
import gzip
import json
import os
import re
import shutil
import time
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any, Iterator

_CALL_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("_llm_call_context", default=None)

# Monotonic within-process sequence per cycle_id, used to order prompt
# captures belonging to the same self-evolving cycle. Process-local is
# sufficient: each cycle runs in its own process/subprocess.
_PROMPT_SEQ: dict[tuple[str, str], "count[int]"] = {}

_DEFAULT_PROMPTS_RETENTION_DAYS = 14

# Best-effort secret scrub for the persisted record. Kept tiny and local
# (rather than importing nanobot.runtime.subagent_materializer._redact_secret_text)
# to avoid coupling nanobot.observability to nanobot.runtime.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9._-]{8,}"),
    re.compile(r"gh[oprsu]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}"),
)


def _redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def set_call_context(cycle_id: str | None, component: str | None) -> Token:
    """Set the current (cycle_id, component) attribution context.

    Returns a token that must be passed to :func:`reset_call_context` to
    restore whatever context was active before (so nested calls don't leak
    into the caller's context once they finish).
    """
    return _CALL_CONTEXT.set({"cycle_id": cycle_id or "", "component": component or ""})


def reset_call_context(token: Token) -> None:
    """Restore the call context captured by the matching ``set_call_context``."""
    with contextlib.suppress(Exception):
        _CALL_CONTEXT.reset(token)


def current_cycle_id(component: str | None = None) -> str:
    """The ``cycle_id`` of the ambient call context, ``""`` when none is set.

    #1374: lets an inner entry point (e.g. ``llm_proposer.propose``) re-enter
    :func:`call_context` with its own ``component`` while KEEPING the cycle its
    caller attributed — ``call_context(None, ...)`` would erase it to ``""``,
    which is how every proposer telemetry row lost its cycle.

    With ``component`` given, the id is returned only when the ambient context
    belongs to that component — a ledger writer asking for the proposer's
    attempt id must never pick up the bridge's executing-cycle context by
    accident. Never raises; any malformed context reads as ``""``.
    """
    try:
        ctx = _CALL_CONTEXT.get() or {}
        if component is not None and str(ctx.get("component") or "") != component:
            return ""
        return str(ctx.get("cycle_id") or "")
    except Exception:
        return ""


@contextlib.contextmanager
def call_context(cycle_id: str | None, component: str | None) -> Iterator[None]:
    """Context manager form of :func:`set_call_context` / :func:`reset_call_context`."""
    token = set_call_context(cycle_id, component)
    try:
        yield
    finally:
        reset_call_context(token)


def _llm_calls_dir() -> Path:
    env_dir = os.environ.get("LLM_CALLS_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    state_dir = os.environ.get("STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir) / "llm_calls"
    return Path.home() / ".nanobot" / "llm_calls"


def record_llm_call(
    *,
    model: str | None,
    duration_ms: float,
    usage: dict[str, Any] | None,
    finish_reason: str | None,
    retries: int,
) -> None:
    """Append one JSONL line describing an LLM call. Best-effort — never raises.

    Reads the ambient (cycle_id, component) context set by the caller's entry
    point (see :func:`call_context`); both default to "" when unset.
    """
    try:
        ctx = _CALL_CONTEXT.get() or {}
        usage = usage or {}
        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "model": model or "",
            "duration_ms": round(duration_ms, 3),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "finish_reason": finish_reason or "",
            "retries": int(retries),
            "cycle_id": ctx.get("cycle_id") or "",
            "component": ctx.get("component") or "",
        }

        out_dir = _llm_calls_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = out_dir / f"{day}.jsonl"
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Telemetry must never break the LLM call it is observing.
        pass


def _prompts_retention_days() -> int:
    raw = os.environ.get("LLM_PROMPTS_RETENTION_DAYS", "").strip()
    if not raw:
        return _DEFAULT_PROMPTS_RETENTION_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_PROMPTS_RETENTION_DAYS


def _day_str(name: str) -> str | None:
    """Extract the YYYY-MM-DD stem from a ``*.jsonl``/``*.jsonl.gz`` filename."""
    if name.endswith(".jsonl.gz"):
        candidate = name[: -len(".jsonl.gz")]
    elif name.endswith(".jsonl"):
        candidate = name[: -len(".jsonl")]
    else:
        return None
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return None
    return candidate


def _rotate_and_prune(prompts_dir: Path, today: str, retention_days: int) -> None:
    """Gzip previous-day plain files and prune ``.jsonl.gz`` older than retention.

    Best-effort: any single file's failure is swallowed so it can't block the
    others or the caller.
    """
    for path in prompts_dir.glob("*.jsonl"):
        day = _day_str(path.name)
        if not day or day == today:
            continue
        try:
            gz_path = path.parent / f"{path.name}.gz"
            with open(path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            path.unlink()
        except Exception:
            continue

    cutoff_ordinal = None
    try:
        from datetime import timedelta

        cutoff_ordinal = (datetime.now(timezone.utc).date() - timedelta(days=retention_days)).toordinal()
    except Exception:
        return

    for path in prompts_dir.glob("*.jsonl.gz"):
        day = _day_str(path.name)
        if not day:
            continue
        try:
            day_ordinal = datetime.strptime(day, "%Y-%m-%d").date().toordinal()
            if day_ordinal < cutoff_ordinal:
                path.unlink(missing_ok=True)
        except Exception:
            continue


MAX_LLM_PROMPT_PAYLOAD_BYTES = 32 * 1024  # 32KB per-record cap (#1039)


# #1319 -- how the cap is met. Until #1319 the first pass clamped every message
# to a fixed 1,000 chars on any overage, so a record one byte over lost ~97% of
# its content; on the host that left a median 6.6 KB of the 32 KiB budget and
# 581 of 582 reflector inputs were cut that way. Passes 2-5 below are unchanged
# and now fire only when the record's structure alone (message count, keys)
# exceeds the budget.
_CAP_FIELDS = ("messages", "content", "reasoning_content")
# A string levelled below this is noise, so when even this level does not fit
# the record takes BOTH losses on purpose: near-total levelling here, then the
# structural passes drop messages. That only happens when the structure alone
# (message count, keys) exceeds the budget; on 334 real over-cap records from
# the 2026-08-25 archive it happened 0 times.
_CAP_MIN_KEEP_CHARS = 32
_CAP_MARKER_RE = re.compile(
    r"…\[truncated \d+ chars\]|…\[truncated\]|…\[intermediate messages omitted\]|…\[payload truncated to fit 32KB budget\]"
)


def _string_leaves(value: Any, out: list[tuple[Any, Any, str]], container: Any = None, key: Any = None) -> None:
    """Collect ``(container, key, string)`` for every string leaf so it can be replaced in place."""
    if isinstance(value, str):
        if container is not None:
            out.append((container, key, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            _string_leaves(v, out, value, k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _string_leaves(v, out, value, i)


def _content_chars(value: Any) -> int:
    """Characters of string content in ``value``, not counting the recorder's own markers."""
    if isinstance(value, str):
        return len(_CAP_MARKER_RE.sub("", value))
    if isinstance(value, dict):
        return sum(_content_chars(v) for v in value.values())
    if isinstance(value, list):
        return sum(_content_chars(v) for v in value)
    return 0


def _level_cut(rec: dict[str, Any], max_bytes: int) -> None:
    """Pass 1 of :func:`_cap_payload_record`: level the string leaves under :data:`_CAP_FIELDS`.

    Property guaranteed: every string longer than a level ``L`` is cut to ``L``
    and suffixed with ``…[truncated N chars]``; strings at or below ``L`` are
    untouched; ``L`` is the LARGEST level at which the whole record serializes
    within ``max_bytes`` (found by bisection on the actual serialized size, so
    bytes-per-character and marker overhead are accounted for exactly). Hence
    the record loses only the overage plus the bookkeeping that records it
    (the ``truncated`` / ``truncated_chars`` keys and one marker per cut
    string, ~70 bytes), taken from its longest strings first: a record one
    byte over loses ~70 characters, never a fixed fraction. If even
    ``L = _CAP_MIN_KEEP_CHARS`` does not fit, that level is applied and the
    structural passes that follow do the rest.

    Cost, measured on the host (i686, Python 3.11.2, 120 real pre-cap records
    of median 102 KB): about 17 full serializations per record, p50 78 ms,
    p90 124 ms, max 192 ms, against 12 ms for the old clamp -- once per LLM
    call that itself waits seconds, on ~90% of executor calls. If that ever
    matters, the lever is the number of probes, not the property.
    """
    leaves: list[tuple[Any, Any, str]] = []
    for field in _CAP_FIELDS:
        if field in rec:
            _string_leaves(rec[field], leaves, rec, field)
    lengths = [len(s) for _, _, s in leaves if len(s) > _CAP_MIN_KEEP_CHARS]
    if not lengths:
        return

    def apply(level: int) -> None:
        for container, key, s in leaves:
            container[key] = s[:level] + f"…[truncated {len(s) - level} chars]" if len(s) > level else s

    def fits(level: int) -> bool:
        apply(level)
        return len(json.dumps(rec, ensure_ascii=False, default=str).encode("utf-8")) <= max_bytes

    low, high = _CAP_MIN_KEEP_CHARS, max(lengths)  # the record is known not to fit at `high` (nothing cut)
    if fits(low):
        while high - low > 1:  # largest level that fits; fits() is monotone in the level
            mid = (low + high) // 2
            if fits(mid):
                low = mid
            else:
                high = mid
    apply(low)


def _cap_payload_record(record: dict[str, Any], max_bytes: int = MAX_LLM_PROMPT_PAYLOAD_BYTES) -> dict[str, Any]:
    """Ensure record JSON serialization does not exceed max_bytes.

    If the serialized record exceeds max_bytes, shortens string values in place
    under ``messages`` / ``content`` / ``reasoning_content`` -- never slicing
    the JSON text, so the record stays parseable at every pass (#1039) -- and
    adds ``truncated: True`` plus ``truncated_chars`` (characters of content
    removed, markers excluded) so a reader learns how much was lost, not only
    that something was (#1319). Pass 1 (:func:`_level_cut`) removes only the
    overage, from the longest strings first; passes 2-5 are the structural
    fallbacks, gentlest first, and fire only when levelling alone cannot fit
    the record.
    """
    serialized = json.dumps(record, ensure_ascii=False, default=str)
    if len(serialized.encode("utf-8")) <= max_bytes:
        return record

    rec = dict(record)
    for field in _CAP_FIELDS:
        if field in rec:
            rec[field] = copy.deepcopy(rec[field])  # the caller's live messages must not be shortened
    rec["truncated"] = True
    # Placeholder as wide as any real count, so the fit below budgets for the key; the true value is shorter.
    rec["truncated_chars"] = 999_999_999

    # First pass: level the string content down to the budget (#1319)
    _level_cut(rec, max_bytes)

    serialized = json.dumps(rec, ensure_ascii=False, default=str)
    if len(serialized.encode("utf-8")) <= max_bytes:
        rec["truncated_chars"] = _content_chars(record) - _content_chars(rec)
        return rec

    # Second pass: aggressively trim intermediate messages
    if isinstance(rec.get("messages"), list) and len(rec["messages"]) > 2:
        rec["messages"] = [rec["messages"][0], {"role": "system", "content": "…[intermediate messages omitted]"}, rec["messages"][-1]]

    # Third pass: drop message list to single summary
    serialized = json.dumps(rec, ensure_ascii=False, default=str)
    if len(serialized.encode("utf-8")) > max_bytes:
        rec["messages"] = [{"role": "info", "content": "…[payload truncated to fit 32KB budget]"}]
        if isinstance(rec.get("content"), str):
            rec["content"] = rec["content"][:500] + "…[truncated]"
        if isinstance(rec.get("reasoning_content"), str):
            rec["reasoning_content"] = rec["reasoning_content"][:500] + "…[truncated]"

    # Fourth pass: if any field (including cycle_id, model, component, etc.) makes the payload exceed max_bytes,
    # trim all non-essential and long fields progressively until it fits strictly within max_bytes.
    serialized = json.dumps(rec, ensure_ascii=False, default=str)
    if len(serialized.encode("utf-8")) > max_bytes:
        for k in list(rec.keys()):
            if k not in ("truncated", "ts", "seq"):
                if isinstance(rec[k], str) and len(rec[k]) > 100:
                    rec[k] = rec[k][:100] + "…[truncated]"
                elif isinstance(rec[k], (list, dict)):
                    rec[k] = "…[truncated]"

    # Hard emergency clamp: if STILL over max_bytes (e.g. huge cycle_id or keys), hard-clamp every string
    serialized = json.dumps(rec, ensure_ascii=False, default=str)
    if len(serialized.encode("utf-8")) > max_bytes:
        for k in list(rec.keys()):
            if k not in ("truncated", "seq"):
                if isinstance(rec[k], str):
                    rec[k] = rec[k][:20] + "…[truncated]"
                elif isinstance(rec[k], (list, dict)):
                    rec[k] = "…[truncated]"

    rec["truncated_chars"] = max(0, _content_chars(record) - _content_chars(rec))
    return rec


def _format_capped_jsonl_line(record: dict[str, Any], max_bytes: int = MAX_LLM_PROMPT_PAYLOAD_BYTES) -> str:
    """Format and serialize record after secret redaction ensuring total UTF-8 encoded bytes <= max_bytes."""
    record = _cap_payload_record(record, max_bytes=max_bytes)
    raw_json = json.dumps(record, ensure_ascii=False, default=str)
    redacted = _redact_secrets(raw_json)

    # In case secret redaction slightly altered size or if record is still over max_bytes
    if len(redacted.encode("utf-8")) <= max_bytes:
        return redacted

    # If post-redaction still exceeds max_bytes, reduce budget and re-serialize
    reduced_budget = max(512, max_bytes - (len(redacted.encode("utf-8")) - max_bytes) - 256)
    rec = _cap_payload_record(record, max_bytes=reduced_budget)
    redacted = _redact_secrets(json.dumps(rec, ensure_ascii=False, default=str))

    # Absolute guarantee clamp on UTF-8 bytes
    if len(redacted.encode("utf-8")) > max_bytes:
        # Fallback minimal valid JSON line
        minimal_rec = {
            "ts": record.get("ts", ""),
            "cycle_id": str(record.get("cycle_id", ""))[:50],
            "component": str(record.get("component", ""))[:50],
            "seq": record.get("seq", 0),
            "truncated": True,
            "error": "…[record payload exceeded max budget after redaction]",
        }
        redacted = _redact_secrets(json.dumps(minimal_rec, ensure_ascii=False, default=str))
        if len(redacted.encode("utf-8")) > max_bytes:
            # Ultimate safety fallback if minimal_rec somehow exceeded budget
            minimal_rec["cycle_id"] = "truncated"
            minimal_rec["component"] = "truncated"
            redacted = _redact_secrets(json.dumps(minimal_rec, ensure_ascii=False, default=str))

    return redacted



def record_llm_prompt(
    *,
    messages: list[dict[str, Any]],
    content: str | None,
    reasoning_content: str | None,
    finish_reason: str | None,
    model: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> None:
    """Append one JSONL line with the full assembled prompt + response.

    Issue #693: complements :func:`record_llm_call` (counts/timing only) by
    persisting the actual ``messages`` sent and the response content, so the
    large subagent context can be inspected offline (see
    ``scripts/llm_prompt_inspect.py``). Best-effort — never raises.

    Honors ``LLM_CAPTURE_PROMPTS`` (default ON; set to "0"/"false"/"" to
    disable, e.g. for privacy or perf-sensitive runs).
    """
    try:
        toggle = os.environ.get("LLM_CAPTURE_PROMPTS")
        if toggle is not None and toggle.strip().lower() in ("0", "false", ""):
            return

        ctx = _CALL_CONTEXT.get() or {}
        cycle_id = ctx.get("cycle_id") or ""
        component = ctx.get("component") or ""

        # #1374: keyed by (cycle_id, component) — the proposer's prompts and
        # the executor's prompts for one cycle are recorded by different
        # processes, each restarting at 1; sharing one key would interleave
        # two independent sequences under one cycle.
        counter = _PROMPT_SEQ.setdefault((cycle_id, component), count(1))
        seq = next(counter)

        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "model": model or "",
            "cycle_id": cycle_id,
            "component": component,
            "seq": seq,
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "finish_reason": finish_reason or "",
            "messages": messages or [],
            "content": content,
            "reasoning_content": reasoning_content,
        }

        line = _format_capped_jsonl_line(record)

        prompts_dir = _llm_calls_dir() / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # #1005 / #1059: distill complete cycle records into durable action index.
        # Bounded to recent days (yesterday + today) and rate-limited via a persistent
        # marker file under writable state/action_index to avoid repeated scans during
        # burst LLM invocations across separate processes in a single cycle.
        try:
            state_root = prompts_dir.parent.parent
            index_dir = state_root / "action_index"
            index_dir.mkdir(parents=True, exist_ok=True)
            marker_file = index_dir / ".last_hook_run"
            rate_limit_secs = float(os.environ.get("LLM_ACTION_INDEX_HOOK_MIN_INTERVAL", "60"))

            should_run = True
            now_epoch = time.time()
            if marker_file.exists():
                try:
                    mtime = marker_file.stat().st_mtime
                    if now_epoch - mtime < rate_limit_secs:
                        should_run = False
                except OSError:
                    # Fail-open on marker stat errors
                    should_run = True

            if should_run:
                try:
                    marker_file.touch(exist_ok=True)
                except OSError:
                    pass
                from nanobot.runtime.action_index import build_action_index

                build_action_index(state_root, prompts_dir, max_days=2)
        except Exception:
            pass
        _rotate_and_prune(prompts_dir, today, _prompts_retention_days())

        out_path = prompts_dir / f"{today}.jsonl"
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        # Capture must never break the LLM call it is observing.
        pass
