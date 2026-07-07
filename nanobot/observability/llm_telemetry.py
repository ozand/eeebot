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
"""

from __future__ import annotations

import contextlib
import json
import os
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_CALL_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("_llm_call_context", default=None)


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
