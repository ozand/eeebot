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
from nanobot.runtime.mutation_policy import (
    MUTATION_POLICY,
    MutationPolicy,
    paths_in_commit_policy,
)

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


def _normalize_path(path: str) -> str:
    if not path or not isinstance(path, str):
        return ""
    p = path.strip().replace("\\", "/")
    parts: list[str] = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        elif part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def is_structurally_falsifiable(
    evaluation_target: str,
    target_path: str = "",
    policy: MutationPolicy | None = None,
) -> bool:
    """Ensure evaluation target is structurally falsifiable.

    #1859: A verification target is structurally falsifiable only if the loop
    CANNOT commit to it. If the loop can commit to the evaluation target
    (e.g. tests/, scripts/, nanobot/ code), the measurement is self-referential
    because the agent can shift the goalposts within the same cycle.

    Evaluation targets outside commit policy (such as harness metrics,
    state/ ledger invariants, ops/ assertions, or release-owned files)
    cannot be mutated by the loop and are structurally falsifiable.
    An empty evaluation target is rejected (never falsifiable).
    """
    if not evaluation_target or not isinstance(evaluation_target, str):
        return False
    norm = _normalize_path(evaluation_target)
    if not norm:
        return False

    pol = policy or MUTATION_POLICY
    if pol.is_forbidden_path(norm):
        return True

    # If it is inside commit policy, the loop can commit to it -> NOT falsifiable
    return not paths_in_commit_policy([norm], policy=pol)



def sanitize_criteria(
    raw: Any,
    target_path: str = "",
    policy: MutationPolicy | None = None,
) -> dict[str, Any] | None:
    """Sanitize and validate DoR or DoD criteria object.

    Expected structure:
    {
      "metric": "<one of _HARNESS_METRICS>", # optional if check is provided
      "eval_kind": "metric" | "script_exit_zero" | "test_count_increase" | "file_exists",
      "target": "<eval target path or metric name>",
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
    if not eval_target:
        # #1859: empty evaluation_target must be rejected
        return None
    if not is_structurally_falsifiable(eval_target, target_path=target_path, policy=policy):
        return None

    desc = str(raw.get("description") or raw.get("claim") or "").strip()
    if not desc:
        return None

    sanitized: dict[str, Any] = {"description": desc[:_MAX_TEXT_CHARS]}
    if eval_kind:
        sanitized["eval_kind"] = eval_kind
    if metric:
        sanitized["metric"] = metric
    sanitized["target"] = eval_target[:_MAX_TEXT_CHARS]

    return sanitized

