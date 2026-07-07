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
import gzip
import json
import os
import re
import shutil
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any, Iterator

_CALL_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("_llm_call_context", default=None)

# Monotonic within-process sequence per cycle_id, used to order prompt
# captures belonging to the same self-evolving cycle. Process-local is
# sufficient: each cycle runs in its own process/subprocess.
_PROMPT_SEQ: dict[str, "count[int]"] = {}

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

        counter = _PROMPT_SEQ.setdefault(cycle_id, count(1))
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

        line = _redact_secrets(json.dumps(record, ensure_ascii=False, default=str))

        prompts_dir = _llm_calls_dir() / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _rotate_and_prune(prompts_dir, today, _prompts_retention_days())

        out_path = prompts_dir / f"{today}.jsonl"
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        # Capture must never break the LLM call it is observing.
        pass
