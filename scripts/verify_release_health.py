#!/usr/bin/env python3
"""Model-free and dashboard-free release health gate (ADR-036 D3 Part B).

Replaces HTTP curl checks on :8080 (/api/health, /api/metrics, /)
by directly executing collection and rendering logic against local state.
Cites: ADR-036 Rule 6.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# Vocabulary allowlist matching dashboard.ARTIFACT_SOURCE_STATUSES
# (class: vocabulary has two owners)
ALLOWED_SOURCE_STATUSES = frozenset({
    "fresh", "stale", "missing", "permission", "unreadable",
    "malformed", "valid-empty", "retired", "unavailable",
})

def validate_health_and_metrics(health: dict[str, Any], metrics: dict[str, Any]) -> None:
    required_health = {"overall", "dimensions", "goal", "active_task", "reward_average"}
    required_metrics = {
        "goal", "active_task", "approval_gate_state",
        "reward_source", "goal_source", "active_task_source", "approval_gate_source",
    }
    source_keys = ("goal_source", "active_task_source", "approval_gate_source", "reward_source")

    for payload, required in ((health, required_health), (metrics, required_metrics)):
        if not all(isinstance(payload.get(k), (str, dict, int, float, list)) for k in required):
            raise SystemExit("dashboard endpoint has invalid bounded field types")

    if not required_health <= health.keys() or not isinstance(health["dimensions"], dict):
        raise SystemExit("dashboard health JSON missing bounded fields")
    if not required_metrics <= metrics.keys():
        raise SystemExit("dashboard metrics JSON missing bounded fields")

    raw_markers = ("evolution-", "materialized-cycle-", "reward_signal")
    payload = json.dumps({"health": health, "metrics": metrics}, sort_keys=True)
    if any(marker in payload for marker in raw_markers):
        raise SystemExit("dashboard endpoint contains raw retired artifact payload")

    for key in source_keys:
        source = metrics.get(key)
        if not isinstance(source, dict) or not {"status", "age_hours", "authoritative", "context_only"} <= source.keys():
            raise SystemExit(f"dashboard endpoint missing bounded source metadata for {key}")
        if source["authoritative"] is not False or source["context_only"] is not True:
            raise SystemExit(f"dashboard endpoint source authority flags are unsafe for {key}")
        if source["status"] not in {"fresh", "stale", "missing", "permission", "unreadable", "malformed", "valid-empty", "retired", "unavailable"}:
            raise SystemExit(f"dashboard endpoint source status is invalid: {source['status']}")
        if source["status"] != "fresh" and source.get("age_hours") is not None and not isinstance(source["age_hours"], (int, float)):
            raise SystemExit(f"dashboard endpoint source age is invalid for {key}")

    if metrics.get("latest_report_path") is not None or metrics.get("materialized_path") is not None:
        raise SystemExit("dashboard endpoint exposes an artifact path")

    source_status = {key: metrics[key]["status"] for key in source_keys}
    for dimension, source_key in (("reward", "reward_source"), ("gate", "approval_gate_source")):
        detail = health.get("dimensions", {}).get(dimension, {})
        if not isinstance(detail, dict) or detail.get("status") not in {"WARN", "OK", "CRIT"}:
            raise SystemExit(f"dashboard health dimension {dimension} is not structured")
        expected = "OK" if source_status[source_key] == "fresh" else "WARN"
        if detail.get("status") != expected:
            raise SystemExit(f"dashboard {dimension} status does not match source state: {detail.get('status')} != {expected}")
        if source_status[source_key] != "fresh" and "source=" + source_status[source_key] not in detail.get("detail", ""):
            raise SystemExit(f"dashboard {dimension} detail lacks bounded source state")

    if source_status["reward_source"] != "fresh":
        label = metrics.get("reward_average")
        match = re.fullmatch(r"([a-z-]+)(?:; age=([0-9]+(?:\.[0-9]+)?)h)? \(context-only artifact\)", label) if isinstance(label, str) else None
        age = metrics["reward_source"].get("age_hours")
        if (match is None or match[1] != source_status["reward_source"]
                or (match[2] is None) != (age is None)
                or (age is not None and float(match[2]) != round(max(0.0, age), 1))):
            raise SystemExit("dashboard reward payload is not bounded")

    if source_status["approval_gate_source"] != "fresh" and metrics.get("approval_gate_state", "").startswith("materialize_"):
        raise SystemExit("dashboard gate payload is not bounded")

    if "0.88 avg over 5 sample(s)" in payload or "materialize_synthesized_improvement" in payload:
        raise SystemExit("dashboard endpoint contains raw legacy dashboard values")

    if any(token in payload for token in ("cycle-2f305bf18b42", "0.88 avg over 5 sample(s)", "materialize_synthesized_improvement")):
        raise SystemExit("dashboard endpoint contains a stale cycle id or retired artifact value")



def verify_release_health(state_dir: Path | None = None) -> dict[str, Any]:
    """Run model-free, dashboard-free release health verification (ADR-036)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    old_state_env = os.environ.get("EEEBOT_STATE_DIR")
    if state_dir is not None:
        os.environ["EEEBOT_STATE_DIR"] = str(state_dir)

    try:
        from scripts.eeebot_dashboard import (
            collect_metrics,
            render_health_json,
            render_html,
            render_json,
        )

        metrics_raw = collect_metrics()
        health_json = render_health_json(metrics_raw)
        metrics_json = render_json(metrics_raw)
        html_content = render_html(metrics_raw)
    finally:
        if state_dir is not None:
            if old_state_env is not None:
                os.environ["EEEBOT_STATE_DIR"] = old_state_env
            else:
                os.environ.pop("EEEBOT_STATE_DIR", None)

    html_bytes = len(html_content.encode("utf-8"))
    if html_bytes < 1024:
        raise SystemExit(
            f"dashboard page / body is {html_bytes} bytes (< 1024); the HTML renderer produced a stub"
        )

    try:
        health = json.loads(health_json)
        metrics = json.loads(metrics_json)
    except Exception as exc:
        raise SystemExit(f"health or metrics payload is not valid JSON: {exc}") from exc

    validate_health_and_metrics(health, metrics)
    return {
        "status": "ok",
        "overall": health.get("overall", "UNKNOWN"),
        "html_bytes": html_bytes,
        "health": health,
        "metrics": metrics,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for deploy release health gate."""
    args = argv if argv is not None else sys.argv[1:]
    state_dir: Path | None = None
    for i, arg in enumerate(args):
        if arg == "--state-dir" and i + 1 < len(args):
            state_dir = Path(args[i + 1])

    try:
        res = verify_release_health(state_dir=state_dir)
        print(f"[remote] release health gate passed (HTML {res['html_bytes']} bytes, health {res['overall']})")
        return 0
    except Exception as exc:
        print(f"CRITICAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
