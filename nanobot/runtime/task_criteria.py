"""Task Definition of Ready (DoR) and Definition of Done (DoD) validation.

Issue #1859: Give tasks their own DoR and DoD tied to verifiable criteria.
Acceptance criteria:
1. Tied to _HARNESS_METRICS registry or objective artifact tests.
2. Structural falsifiability: Verification of metric/claim MUST NOT be computed
   solely from the modified target artifact itself (external verification).
3. No lexical checks (no 'word appears in text', 'length increased', 'has heading').
"""

from __future__ import annotations

from typing import Any

from nanobot.runtime.benchmark_evidence import _HARNESS_METRICS

_VALID_EVAL_KINDS = frozenset({
    "metric",
    "script_exit_zero",
    "test_count_increase",
    "file_exists",
})

_MAX_TEXT_CHARS = 300


def validate_metric_reference(metric: str) -> bool:
    """Validate that a metric is formally registered in _HARNESS_METRICS."""
    return metric in _HARNESS_METRICS


def is_structurally_falsifiable(target_path: str, evaluation_target: str) -> bool:
    """Ensure evaluation target does not compute solely from the modified target path.

    If a task mutates `target_path`, the acceptance metric/eval cannot be the same
    file unless it is an external test or runner testing it.
    """
    t = (target_path or "").strip().lower()
    e = (evaluation_target or "").strip().lower()
    if not t or not e:
        return True
    return t != e


def sanitize_criteria(
    raw: Any,
    target_path: str = "",
) -> dict[str, Any] | None:
    """Sanitize and validate DoR or DoD criteria object.

    Expected structure:
    {
      "metric": "<one of _HARNESS_METRICS>", # optional if check is provided
      "eval_kind": "metric" | "script_exit_zero" | "test_count_increase" | "file_exists",
      "target": "<eval target path or command>",
      "description": "<non-empty string>"
    }
    """
    if not isinstance(raw, dict):
        return None

    eval_kind = str(raw.get("eval_kind") or "").strip()
    metric = str(raw.get("metric") or "").strip()

    if eval_kind and eval_kind not in _VALID_EVAL_KINDS:
        return None

    if metric and not validate_metric_reference(metric):
        return None

    eval_target = str(raw.get("target") or "").strip()
    if eval_target and target_path:
        if not is_structurally_falsifiable(target_path, eval_target):
            return None

    desc = str(raw.get("description") or raw.get("claim") or "").strip()
    if not desc:
        return None

    sanitized: dict[str, Any] = {"description": desc[:_MAX_TEXT_CHARS]}
    if eval_kind:
        sanitized["eval_kind"] = eval_kind
    if metric:
        sanitized["metric"] = metric
    if eval_target:
        sanitized["target"] = eval_target[:_MAX_TEXT_CHARS]

    return sanitized
