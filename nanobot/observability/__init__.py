"""Observability helpers for the nanobot runtime (issue #675).

Currently just per-LLM-call telemetry (:mod:`nanobot.observability.llm_telemetry`).
Kept intentionally small — no tracing/metrics framework, just a JSONL append
that complements the LiteLLM proxy's own spend/latency logs with the
cycle_id/component context the proxy doesn't have.
"""
