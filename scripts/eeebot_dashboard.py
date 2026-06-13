#!/usr/bin/env python3
"""Minimal status dashboard for the eeebot self-evolving runtime.

The dashboard intentionally stays dependency-free so it can run on the weak
host even when richer TUI libraries are unavailable.
Supports CLI plain text output and a web interface (--serve).
"""

from __future__ import annotations

import heapq
import html
import http.server
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.getenv("EEEBOT_STATE_DIR", str(REPO_ROOT / "state")))
IMPROVEMENT_DIR = Path(
    os.getenv(
        "EEEBOT_IMPROVEMENTS_DIR",
        "/var/lib/eeepc-agent/self-evolving-agent/state/improvements",
    )
)
REPORTS_DIR = Path(
    os.getenv(
        "EEEBOT_REPORTS_DIR",
        "/var/lib/eeepc-agent/self-evolving-agent/state/reports",
    )
)
METRICS_CACHE_TTL_SECONDS = 3.0
TREE_SCAN_CACHE_TTL_SECONDS = 3.0
HOST_CAPS_CACHE_TTL_SECONDS = 30.0
_METRICS_CACHE: dict[str, Any] = {"loaded_at": 0.0, "metrics": None}
_SUBAGENT_TREE_CACHE: dict[str, Any] = {"loaded_at": 0.0, "hours": None, "root_mtime_ns": None, "stats": None}
_HOST_CAPS_CACHE: dict[str, Any] = {"loaded_at": 0.0, "host_caps": None}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def latest_file(directory: Path, pattern: str) -> Path | None:
    try:
        return max(directory.glob(pattern), key=lambda p: p.stat().st_mtime, default=None)
    except Exception:
        return None


def scan_report_artifacts(limit: int = 5) -> tuple[Path | None, dict[str, Any], list[tuple[str, float]]]:
    """Scan report artifacts once and reuse the result for latest-report and trend views."""

    latest_path: Path | None = None
    latest_mtime = -1.0
    recent_heap: list[tuple[float, Path]] = []

    try:
        for path in REPORTS_DIR.glob("evolution-*.json"):
            mtime = path.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_path = path
            item = (mtime, path)
            if len(recent_heap) < limit:
                heapq.heappush(recent_heap, item)
            else:
                heapq.heappushpop(recent_heap, item)
    except Exception:
        return None, {}, []

    if latest_path is None:
        return None, {}, []

    latest_report: dict[str, Any] = {}
    recent_rewards: list[tuple[str, float]] = []
    for _, report in sorted(recent_heap, reverse=True):
        data = load_json(report, {})
        reward = data.get("reward_signal", {}).get("value")
        if isinstance(reward, (int, float)):
            recent_rewards.append((data.get("cycle_id", report.stem), float(reward)))
        if report == latest_path:
            latest_report = data

    if not latest_report:
        latest_report = load_json(latest_path, {})
    return latest_path, latest_report, recent_rewards


def load_latest_materialized() -> tuple[Path | None, dict[str, Any]]:
    latest = latest_file(IMPROVEMENT_DIR, "materialized-cycle-*.json")
    if not latest:
        return None, {}
    return latest, load_json(latest, {})


def load_latest_report() -> tuple[Path | None, dict[str, Any]]:
    latest_path, latest_report, _ = scan_report_artifacts(limit=1)
    return latest_path, latest_report


def load_recent_rewards(limit: int = 5) -> list[tuple[str, float]]:
    _, _, recent_rewards = scan_report_artifacts(limit=limit)
    return recent_rewards


def scan_subagent_tree_stats(hours: int = 24) -> tuple[int, int, float | None, int, Path | None]:
    now = time.monotonic()
    queue_root = STATE_DIR / "subagents"
    cached_hours = _SUBAGENT_TREE_CACHE.get("hours")
    cached_root_mtime_ns = _SUBAGENT_TREE_CACHE.get("root_mtime_ns")
    cached_stats = _SUBAGENT_TREE_CACHE.get("stats")
    loaded_at = float(_SUBAGENT_TREE_CACHE.get("loaded_at", 0.0) or 0.0)
    try:
        root_mtime_ns = queue_root.stat().st_mtime_ns if queue_root.exists() else None
    except Exception:
        root_mtime_ns = None
    if (
        cached_stats is not None
        and cached_hours == hours
        and cached_root_mtime_ns == root_mtime_ns
        and now - loaded_at < TREE_SCAN_CACHE_TTL_SECONDS
    ):
        return cached_stats

    if not queue_root.exists():
        stats = (0, 0, None, 0, None)
        _SUBAGENT_TREE_CACHE.update({"loaded_at": now, "hours": hours, "root_mtime_ns": root_mtime_ns, "stats": stats})
        return stats

    cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
    queue_depth = 0
    stale_count = 0
    archived_count = 0
    oldest_stale: float | None = None
    oldest_stale_path: Path | None = None
    try:
        for path in queue_root.rglob("*"):
            if not path.is_file():
                continue
            if "archive" in path.parts:
                archived_count += 1
                continue
            queue_depth += 1
            mtime = path.stat().st_mtime
            if mtime < cutoff:
                stale_count += 1
                if oldest_stale is None or mtime < oldest_stale:
                    oldest_stale = mtime
                    oldest_stale_path = path
    except Exception:
        stats = (0, 0, None, 0, None)
        _SUBAGENT_TREE_CACHE.update({"loaded_at": now, "hours": hours, "root_mtime_ns": root_mtime_ns, "stats": stats})
        return stats
    oldest_stale_age_hours = None
    if oldest_stale is not None:
        oldest_stale_age_hours = (datetime.now(timezone.utc).timestamp() - oldest_stale) / 3600
    stats = (queue_depth, stale_count, oldest_stale_age_hours, archived_count, oldest_stale_path)
    _SUBAGENT_TREE_CACHE.update({"loaded_at": now, "hours": hours, "root_mtime_ns": root_mtime_ns, "stats": stats})
    return stats


def scan_queue_stats(hours: int = 24) -> tuple[int, int, float | None]:
    queue_depth, stale_count, oldest_stale_age_hours, _, _ = scan_subagent_tree_stats(hours)
    return queue_depth, stale_count, oldest_stale_age_hours


def queue_tree_stats(hours: int = 24) -> tuple[int, int, float | None, int, Path | None]:
    return scan_subagent_tree_stats(hours)


def count_queue_depth() -> int:
    queue_depth, _, _, _, _ = queue_tree_stats()
    return queue_depth


def count_stale_queue_requests(hours: int = 24) -> int:
    _, stale_count, _, _, _ = queue_tree_stats(hours)
    return stale_count


def count_archived_requests() -> int:
    _, _, _, archived_count, _ = queue_tree_stats()
    return archived_count


def format_reward_trend(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "no recent reward samples"
    return ", ".join(f"{cycle}={reward:.2f}" for cycle, reward in trend)


def format_reward_momentum(trend: list[tuple[str, float]]) -> str:
    if len(trend) < 2:
        return "insufficient samples"
    latest = trend[0][1]
    previous = trend[1][1]
    delta = latest - previous
    direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return f"{direction} {delta:+.2f} vs previous"


def format_reward_average(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "no recent reward samples"
    average = sum(reward for _, reward in trend) / len(trend)
    return f"{average:.2f} avg over {len(trend)} sample(s)"


def format_reward_range(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "no recent reward samples"
    rewards = [reward for _, reward in trend]
    return f"{min(rewards):.2f}–{max(rewards):.2f} range"


def format_reward_trend_html(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "<em>No recent cycles</em>"
    badges: list[str] = []
    for cycle, reward in trend:
        color = "#2ecc71" if reward >= 1.2 else ("#3498db" if reward >= 1.0 else "#e74c3c")
        badges.append(
            f"""
        <div class=\"trend-badge\" style=\"background: {color};\">
            <strong>{html.escape(cycle)}</strong>: {reward:.2f}
        </div>
        """
        )
    return "".join(badges)


def format_queue_state(queue_depth: int, stale_count: int) -> str:
    if queue_depth <= 0 and stale_count <= 0:
        return "queue idle"
    if stale_count > 0:
        return f"{stale_count}/{queue_depth} stale"
    return f"{queue_depth} pending"


def format_operator_attention(m: dict[str, Any]) -> str:
    queue_part = format_queue_state(m["queue_depth"], m["stale_queue_requests"])
    gate = m["approval_gate_state"]
    momentum = m["reward_momentum"]
    return f"{queue_part} · gate={gate} · momentum={momentum}"


def format_dashboard_summary(m: dict[str, Any]) -> str:
    queue_part = format_queue_state(m["queue_depth"], m["stale_queue_requests"])
    archived = m.get("archived_count", 0)
    cleanup_count = m.get("last_cleanup_count", "unknown")
    cleanup_recency = m.get("last_cleanup_recency", "unknown")
    cleanup_status = m.get("last_cleanup_status", "unknown")
    queue_hygiene = m.get("queue_hygiene", "unknown")
    queue_priority = m.get("queue_priority", "unknown")
    archive_target = m.get("queue_archive_target", "none")
    cleanup_summary = f"{cleanup_count} cleaned" if cleanup_count != "unknown" else "cleanup unknown"
    return (
        f"{queue_part} · host={m['host_capability_coverage']} · probe={m.get('host_capability_probe_age', 'unknown')} · "
        f"cleanup={cleanup_summary}, {cleanup_recency}/{cleanup_status} · hygiene={queue_hygiene} · "
        f"priority={queue_priority} · archive={archive_target} · archived={archived} · gate={m['approval_gate_state']}"
    )


def format_focus_line(m: dict[str, Any]) -> str:
    return (
        f"goal={m['goal']} · task={m['active_task']} · "
        f"queue={m['queue_depth']}/{m['stale_queue_requests']} stale · gate={m['approval_gate_state']} · "
        f"momentum={m['reward_momentum']}"
    )


def format_queue_snapshot(
    queue_depth: int,
    stale_count: int,
    archived_count: int,
    approval_gate_state: str,
    last_cleanup_recency: str,
) -> str:
    queue_part = format_queue_state(queue_depth, stale_count)
    return (
        f"{queue_part} · archived={archived_count} · cleanup={last_cleanup_recency} · "
        f"gate={approval_gate_state}"
    )


def format_artifact_freshness(materialized_recency: str, latest_report_recency: str) -> str:
    return f"materialized={materialized_recency}, report={latest_report_recency}"


def format_queue_hygiene(
    queue_depth: int,
    stale_count: int,
    last_cleanup_recency: str,
    last_cleanup_status: str,
) -> str:
    queue_freshness = format_queue_freshness(queue_depth, stale_count)
    return f"{queue_freshness} · cleanup={last_cleanup_recency}/{last_cleanup_status}"


def format_queue_health(health: dict[str, Any]) -> str:
    cleanup_count = health.get("last_subagent_cleanup_count", "unknown")
    cleanup_ts = health.get("last_subagent_cleanup_timestamp", "unknown")
    return f"last cleanup {cleanup_count} @ {cleanup_ts}"


def _cleanup_age_hours(last_cleanup_timestamp: Any) -> float | None:
    if not last_cleanup_timestamp or str(last_cleanup_timestamp).strip().lower() == "unknown":
        return None
    try:
        timestamp = str(last_cleanup_timestamp).replace("Z", "+00:00")
        dt = datetime.fromisoformat(timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


def format_cleanup_recency_from_age(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours < 0:
        return "future"
    if age_hours < 1:
        return f"{age_hours * 60:.0f}m ago"
    return f"{age_hours:.1f}h ago"


def format_cleanup_recency(last_cleanup_timestamp: Any) -> str:
    return format_cleanup_recency_from_age(_cleanup_age_hours(last_cleanup_timestamp))


def format_cleanup_status_from_age(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours < 0:
        return "future"
    if age_hours >= 24:
        return "stale"
    return "fresh"


def format_cleanup_status(last_cleanup_timestamp: Any) -> str:
    return format_cleanup_status_from_age(_cleanup_age_hours(last_cleanup_timestamp))


def format_queue_pressure(queue_depth: int, stale_count: int, oldest_stale_age_hours: float | None) -> str:
    if queue_depth <= 0:
        return "idle"
    if stale_count <= 0:
        return f"{queue_depth} pending, no stale requests"
    oldest_age = format_oldest_stale_request_age(oldest_stale_age_hours)
    stale_ratio = stale_count / queue_depth
    return f"{stale_count}/{queue_depth} stale ({stale_ratio:.0%}), oldest {oldest_age}"


def format_queue_freshness(queue_depth: int, stale_count: int) -> str:
    if queue_depth <= 0:
        return "idle"
    stale_ratio = stale_count / queue_depth
    return f"{stale_count}/{queue_depth} stale ({stale_ratio:.0%})"


def format_queue_action(
    queue_depth: int,
    stale_count: int,
    oldest_stale_age_hours: float | None,
    oldest_stale_request_path: Path | None,
    last_cleanup_recency: str,
) -> str:
    if queue_depth <= 0:
        return "no queue work pending"
    if stale_count > 0:
        age_text = format_oldest_stale_request_age(oldest_stale_age_hours)
        path_text = format_oldest_stale_request_path(oldest_stale_request_path)
        return f"archive {stale_count} stale request(s) — oldest {age_text} @ {path_text}"
    if queue_depth >= 20:
        cleanup_text = last_cleanup_recency if last_cleanup_recency else "unknown"
        return f"watch queue pressure — last cleanup {cleanup_text}"
    return "queue healthy; no archive action needed"



def format_queue_archive_target(
    stale_count: int,
    oldest_stale_age_hours: float | None,
    oldest_stale_request_path: Path | None,
) -> str:
    if stale_count <= 0:
        return "none"
    age_text = format_oldest_stale_request_age(oldest_stale_age_hours)
    path_text = format_oldest_stale_request_path(oldest_stale_request_path)
    return f"{path_text} ({age_text})"



def format_queue_priority(queue_depth: int, stale_count: int, oldest_stale_age_hours: float | None) -> str:
    if queue_depth <= 0:
        return "idle"
    if stale_count > 0:
        if oldest_stale_age_hours is None:
            return "urgent"
        if oldest_stale_age_hours >= 48:
            return "urgent"
        if oldest_stale_age_hours >= 24:
            return "elevated"
        return "watch"
    if queue_depth >= 20:
        return "elevated"
    if queue_depth >= 5:
        return "watch"
    return "normal"



def format_oldest_stale_request_age(oldest_stale_age_hours: float | None) -> str:
    if oldest_stale_age_hours is None:
        return "unknown"
    return f"{oldest_stale_age_hours:.1f}h"


def format_oldest_stale_request_path(oldest_stale_request_path: Path | None) -> str:
    if oldest_stale_request_path is None:
        return "none"
    return str(oldest_stale_request_path)


def format_age_hours(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    return f"{age_hours:.1f}h"


def format_probe_status_from_age(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours < 0:
        return "future"
    if age_hours >= 24:
        return "stale"
    return "fresh"


def format_refresh_timestamp(timestamp: Any) -> str:
    if timestamp is None:
        return "unknown"
    text = str(timestamp).strip()
    return text or "unknown"


def file_age_hours(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
        return (datetime.now(timezone.utc) - datetime.fromtimestamp(mtime, tz=timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


def format_file_recency(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours < 1:
        return f"{age_hours * 60:.0f}m ago"
    return f"{age_hours:.1f}h ago"


def format_recent_cycles(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "no recent cycles"
    return ", ".join(cycle for cycle, _ in trend)


def format_materialized_cycle(materialized: dict[str, Any]) -> str:
    if not materialized:
        return "unknown"
    return str(materialized.get("cycle_id", "unknown"))


def format_materialized_status(materialized: dict[str, Any]) -> str:
    if not materialized:
        return "no materialized improvement loaded"
    cycle_id = str(materialized.get("cycle_id", "unknown"))
    summary = str(materialized.get("summary", "")).strip()
    if summary:
        return f"{cycle_id} — {summary}"
    return cycle_id


def format_next_candidate(materialized: dict[str, Any]) -> str:
    candidate = materialized.get("next_bounded_candidate", {})
    if not isinstance(candidate, dict) or not candidate:
        return "no next candidate recorded"
    task_id = str(candidate.get("task_id", "unknown"))
    title = str(candidate.get("title", "")).strip()
    if title:
        return f"{task_id} — {title}"
    return task_id


def format_goal_artifact_signature(materialized: dict[str, Any]) -> str:
    signature = materialized.get("feedback_decision", {}).get("goal_artifact_signature", [])
    if not isinstance(signature, list) or not signature:
        return "no goal artifact signature recorded"
    return " / ".join(str(part) for part in signature)


def _extract_report_evidence(report: dict[str, Any]) -> str:
    for key in ("evidence_id", "evidence_ref_id"):
        value = report.get(key)
        if value:
            return str(value).strip()

    evidence = report.get("evidence")
    if isinstance(evidence, dict):
        for key in ("evidence_id", "id", "ref_id", "ref", "path", "value"):
            value = evidence.get(key)
            if value:
                return str(value).strip()
    elif evidence is not None:
        text = str(evidence).strip()
        if text:
            return text
    return ""


def format_latest_report_status(report: dict[str, Any]) -> str:
    if not report:
        return "no latest report loaded"
    result = str(report.get("reward_signal", {}).get("result_status", "unknown"))
    evidence = _extract_report_evidence(report)
    if evidence:
        return f"{result} — evidence {evidence}"
    return result


def format_concrete_statement(materialized: dict[str, Any]) -> str:
    statement = str(materialized.get("concrete_improvement_statement", "")).strip()
    if statement:
        return statement
    return "no concrete improvement statement recorded"


def _load_host_capabilities_uncached() -> dict[str, Any]:
    return load_json(STATE_DIR / "host_capabilities.json", {})


def load_host_capabilities() -> dict[str, Any]:
    now = time.monotonic()
    cached = _HOST_CAPS_CACHE.get("host_caps")
    loaded_at = float(_HOST_CAPS_CACHE.get("loaded_at", 0.0) or 0.0)
    if cached is not None and now - loaded_at < HOST_CAPS_CACHE_TTL_SECONDS:
        return cached
    host_caps = _load_host_capabilities_uncached()
    _HOST_CAPS_CACHE["host_caps"] = host_caps
    _HOST_CAPS_CACHE["loaded_at"] = now
    return host_caps


def scan_host_capabilities(
    host_caps: dict[str, Any],
) -> tuple[list[str], list[tuple[str, str]], str, list[tuple[str, str]], str, str]:
    focus_order = ("camera", "bluetooth", "wifi", "microphone")
    if not host_caps:
        unavailable = "host hardware status unavailable"
        return [], [], unavailable, [], unavailable, unavailable

    available_caps: list[str] = []
    capability_details: list[tuple[str, str]] = []
    focus_status_parts: list[str] = []
    focus_details: list[tuple[str, str]] = []
    missing_focus_devices: list[str] = []

    for name, info in host_caps.items():
        if not isinstance(info, dict):
            continue
        available = bool(info.get("available"))
        details = str(info.get("details", "")).strip() or "available"
        if available:
            available_caps.append(name)
            capability_details.append((name, details))

    available_caps.sort()
    capability_details.sort(key=lambda item: item[0])

    for name in focus_order:
        info = host_caps.get(name, {})
        if not isinstance(info, dict):
            info = {}
        available = bool(info.get("available"))
        details = str(info.get("details", "")).strip() or "available"
        focus_status_parts.append(f"{name}:{'✓' if available else '✗'}")
        focus_details.append((name, details))
        if not available:
            missing_focus_devices.append(name)

    available_count = len(focus_order) - len(missing_focus_devices)
    focus_status = ", ".join(focus_status_parts)
    coverage = f"{available_count}/{len(focus_order)} focus devices available"
    missing = "none" if not missing_focus_devices else ", ".join(missing_focus_devices)
    return available_caps, capability_details, focus_status, focus_details, coverage, missing


def format_host_capability_badges_html(host_capabilities: list[str]) -> str:
    if not host_capabilities:
        return "<em>None detected</em>"
    return "".join(f'<span class="cap-tag">{html.escape(capability)}</span>' for capability in host_capabilities)


def format_host_capability_details_html(details: list[tuple[str, str]]) -> str:
    detail_items = "".join(
        f'<li><strong>{html.escape(name)}</strong>: {html.escape(text or "available")}</li>'
        for name, text in details
    )
    if detail_items:
        return f"<ul class='cap-list'>{detail_items}</ul>"
    return "<em>No capability details available</em>"


def escape_html_text(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def collect_metrics_uncached() -> dict[str, Any]:
    captured_at = datetime.now(timezone.utc).isoformat()
    health = load_json(STATE_DIR / "current_health.json", {})
    materialized_path, materialized = load_latest_materialized()
    latest_report_path, latest_report, recent_rewards = scan_report_artifacts()
    reward_momentum = format_reward_momentum(recent_rewards)
    reward_average = format_reward_average(recent_rewards)
    reward_range = format_reward_range(recent_rewards)
    host_caps = load_host_capabilities()
    host_capability_probe_age_hours = file_age_hours(STATE_DIR / "host_capabilities.json")
    host_capability_probe_status = format_probe_status_from_age(host_capability_probe_age_hours)

    goal = materialized.get("goal_id", latest_report.get("goal_id", "unknown"))
    active_task = materialized.get("feedback_decision", {}).get(
        "selected_task_label",
        latest_report.get("feedback_decision", {}).get(
            "selected_task_label",
            materialized.get("task_id", latest_report.get("current_task_id", "unknown")),
        ),
    )
    approval_gate_state = materialized.get("feedback_decision", {}).get(
        "mode",
        latest_report.get("feedback_decision", {}).get("mode", "unknown"),
    )

    (
        available_caps,
        capability_details,
        host_focus_status,
        host_focus_details,
        host_capability_coverage,
        host_focus_missing,
    ) = scan_host_capabilities(host_caps)
    host_focus_names = {name for name, _ in host_focus_details}
    host_focus_name_set = host_focus_names

    queue_depth, stale_queue_requests, oldest_stale_age_hours, archived_count, oldest_stale_request_path = scan_subagent_tree_stats()

    last_cleanup_count = health.get("last_subagent_cleanup_count", "unknown")
    last_cleanup_timestamp = health.get("last_subagent_cleanup_timestamp", "unknown")
    last_cleanup_recency = format_cleanup_recency(last_cleanup_timestamp)
    last_cleanup_status = format_cleanup_status(last_cleanup_timestamp)
    queue_action = format_queue_action(
        queue_depth,
        stale_queue_requests,
        oldest_stale_age_hours,
        oldest_stale_request_path,
        last_cleanup_recency,
    )
    queue_archive_target = format_queue_archive_target(
        stale_queue_requests,
        oldest_stale_age_hours,
        oldest_stale_request_path,
    )
    queue_priority = format_queue_priority(queue_depth, stale_queue_requests, oldest_stale_age_hours)

    queue_pressure = format_queue_pressure(queue_depth, stale_queue_requests, oldest_stale_age_hours)
    queue_hygiene = format_queue_hygiene(
        queue_depth,
        stale_queue_requests,
        last_cleanup_recency,
        last_cleanup_status,
    )
    materialized_age_hours = file_age_hours(materialized_path)
    latest_report_age_hours = file_age_hours(latest_report_path)

    artifact_freshness = format_artifact_freshness(
        format_file_recency(materialized_age_hours),
        format_file_recency(latest_report_age_hours),
    )

    dashboard_summary = format_dashboard_summary({
        "queue_depth": queue_depth,
        "stale_queue_requests": stale_queue_requests,
        "archived_count": archived_count,
        "approval_gate_state": approval_gate_state,
        "host_capability_coverage": host_capability_coverage,
        "last_cleanup_recency": last_cleanup_recency,
        "last_cleanup_status": last_cleanup_status,
        "queue_hygiene": queue_hygiene,
    })
    queue_snapshot = format_queue_snapshot(
        queue_depth,
        stale_queue_requests,
        archived_count,
        approval_gate_state,
        last_cleanup_recency,
    )

    return {
        "captured_at": captured_at,
        "goal": goal,
        "active_task": active_task,
        "recent_cycles": format_recent_cycles(recent_rewards),
        "reward_trend": recent_rewards,
        "reward_momentum": reward_momentum,
        "reward_average": reward_average,
        "reward_range": reward_range,
        "operator_attention": format_operator_attention({
            "queue_depth": queue_depth,
            "stale_queue_requests": stale_queue_requests,
            "approval_gate_state": approval_gate_state,
            "reward_momentum": reward_momentum,
        }),
        "host_capability_badges_html": format_host_capability_badges_html(available_caps),
        "host_capability_details_html": format_host_capability_details_html(capability_details),
        "dashboard_summary": dashboard_summary,
        "focus_line": format_focus_line({
            "goal": goal,
            "active_task": active_task,
            "queue_depth": queue_depth,
            "stale_queue_requests": stale_queue_requests,
            "approval_gate_state": approval_gate_state,
            "reward_momentum": reward_momentum,
        }),
        "queue_snapshot": queue_snapshot,
        "materialized_cycle": format_materialized_cycle(materialized),
        "queue_depth": queue_depth,
        "stale_queue_requests": stale_queue_requests,
        "queue_freshness": format_queue_freshness(queue_depth, stale_queue_requests),
        "queue_pressure": queue_pressure,
        "queue_action": queue_action,
        "queue_archive_target": queue_archive_target,
        "queue_priority": queue_priority,
        "queue_hygiene": queue_hygiene,
        "oldest_stale_age_hours": oldest_stale_age_hours,
        "oldest_stale_request_age": format_oldest_stale_request_age(oldest_stale_age_hours),
        "oldest_stale_request_path": oldest_stale_request_path,
        "archived_count": archived_count,
        "approval_gate_state": approval_gate_state,
        "materialized_status": format_materialized_status(materialized),
        "concrete_statement": format_concrete_statement(materialized),
        "goal_artifact_signature": format_goal_artifact_signature(materialized),
        "latest_report_status": format_latest_report_status(latest_report),
        "next_bounded_candidate": format_next_candidate(materialized),
        "materialized_path": str(materialized_path) if materialized_path else None,
        "materialized_recency": format_file_recency(materialized_age_hours),
        "latest_report_path": str(latest_report_path) if latest_report_path else None,
        "latest_report_recency": format_file_recency(latest_report_age_hours),
        "artifact_freshness": artifact_freshness,
        "last_cleanup_count": last_cleanup_count,
        "last_cleanup_timestamp": last_cleanup_timestamp,
        "queue_health": format_queue_health(health),
        "last_cleanup_recency": last_cleanup_recency,
        "last_cleanup_status": last_cleanup_status,
        "host_capabilities": available_caps,
        "host_capability_details": capability_details,
        "host_focus_status": host_focus_status,
        "host_focus_details": host_focus_details,
        "host_focus_names": sorted(host_focus_names),
        "host_focus_name_set": host_focus_name_set,
        "host_capability_coverage": host_capability_coverage,
        "host_focus_missing": host_focus_missing,
        "host_capability_probe_age_hours": host_capability_probe_age_hours,
        "host_capability_probe_age": format_age_hours(host_capability_probe_age_hours),
        "host_capability_probe_status": host_capability_probe_status,
    }


def collect_metrics() -> dict[str, Any]:
    now = time.monotonic()
    cached_metrics = _METRICS_CACHE.get("metrics")
    loaded_at = float(_METRICS_CACHE.get("loaded_at", 0.0) or 0.0)
    if cached_metrics is not None and now - loaded_at < METRICS_CACHE_TTL_SECONDS:
        return cached_metrics
    metrics = collect_metrics_uncached()
    _METRICS_CACHE["metrics"] = metrics
    _METRICS_CACHE["loaded_at"] = now
    return metrics


def serialize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    serializable = dict(metrics)
    for key in ("materialized_path", "latest_report_path", "oldest_stale_request_path"):
        value = serializable.get(key)
        serializable[key] = str(value) if value else None
    host_focus_name_set = serializable.get("host_focus_name_set")
    if isinstance(host_focus_name_set, set):
        serializable["host_focus_name_set"] = sorted(host_focus_name_set)
    return serializable


def render_json(metrics: dict[str, Any]) -> str:
    return json.dumps(serialize_metrics(metrics), indent=2, sort_keys=True)


def write_snapshot(metrics: dict[str, Any], destination: Path | None = None) -> Path:
    destination = destination or Path(f"/tmp/eeebot-dashboard-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.txt")
    snapshot_lines = [
        "EeeBot Dashboard Snapshot",
        f"Captured At: {datetime.now(timezone.utc).isoformat()}",
        f"Goal: {metrics['goal']}",
        f"Active Task: {metrics['active_task']}",
        f"Queue Snapshot: {metrics['queue_snapshot']}",
        f"Recent Cycles: {metrics['recent_cycles']}",
        f"Reward Trend: {format_reward_trend(metrics['reward_trend'])}",
        f"Reward Average: {metrics['reward_average']}",
        f"Reward Range: {metrics['reward_range']}",
        f"Queue Pressure: {metrics['queue_pressure']}",
        f"Queue Hygiene: {metrics['queue_hygiene']}",
        f"Queue Action: {metrics['queue_action']}",
        f"Queue Archive Target: {metrics['queue_archive_target']}",
        f"Last Cleanup Recency: {metrics['last_cleanup_recency']}",
        f"Last Cleanup Status: {metrics['last_cleanup_status']}",
        f"Approval Gate State: {metrics['approval_gate_state']}",
        f"Materialized Improvement: {metrics['materialized_status']}",
        f"Concrete Statement: {metrics['concrete_statement']}",
        f"Latest Report Status: {metrics['latest_report_status']}",
        f"Artifact Freshness: {metrics['artifact_freshness']}",
        f"Host Focus: {metrics['host_focus_status']}",
        f"Host Focus Status: {metrics['host_focus_status']}",
        f"Host Capability Coverage: {metrics['host_capability_coverage']}",
        f"Host Capability Probe Age: {metrics['host_capability_probe_age']} ({metrics['host_capability_probe_status']})",
        f"Missing Focus Devices: {metrics['host_focus_missing']}",
        f"Captured At: {format_refresh_timestamp(metrics['captured_at'])}",
    ]
    destination.write_text("\n".join(snapshot_lines) + "\n", encoding="utf-8")
    return destination


def render_cli(m: dict[str, Any]) -> str:
    lines = [
        "EeeBot Dashboard",
        f"Summary: {m['dashboard_summary']}",
        f"Queue Snapshot: {m['queue_snapshot']}",
        f"Goal: {m['goal']}",
        f"Active Task: {m['active_task']}",
        f"Recent Cycles: {m['recent_cycles']}",
        f"Reward Trend: {format_reward_trend(m['reward_trend'])}",
        f"Reward Momentum: {m['reward_momentum']}",
        f"Reward Average: {m['reward_average']}",
        f"Reward Range: {m['reward_range']}",
        f"Subagent Queue Depth: {m['queue_depth']}",
        f"Stale Queue Requests (>24h): {m['stale_queue_requests']}",
         f"Queue Freshness: {m['queue_freshness']}",
         f"Queue Hygiene: {m['queue_hygiene']}",
         f"Queue Pressure: {m['queue_pressure']}",
         f"Queue Action: {m['queue_action']}",
         f"Queue Archive Target: {m['queue_archive_target']}",
         f"Queue Priority: {m['queue_priority']}",
         f"Operator Attention: {m['operator_attention']}",


         f"Oldest Stale Request Age: {m['oldest_stale_request_age']}",
        f"Oldest Stale Request Path: {format_oldest_stale_request_path(m['oldest_stale_request_path'])}",
         f"Last Cleanup Recency: {m['last_cleanup_recency']}",
         f"Archived Subagent Requests: {m['archived_count']}",

        f"Approval Gate State: {m['approval_gate_state']}",
        f"Materialized Cycle: {m['materialized_cycle']}",
        f"Materialized Improvement: {m['materialized_status']}",
        f"Concrete Statement: {m['concrete_statement']}",
        f"Latest Report Status: {m['latest_report_status']}",
        f"Goal Artifact Signature: {m['goal_artifact_signature']}",
        f"Next Bounded Candidate: {m['next_bounded_candidate']}",
    ]
    if m["materialized_path"]:
        lines.append(f"Materialized Artifact Path: {m['materialized_path']}")
    if m["latest_report_path"]:
        lines.append(f"Latest Report Path: {m['latest_report_path']}")
    lines.append(f"Last Cleanup Count: {m['last_cleanup_count']}")
    lines.append(f"Last Cleanup Timestamp: {m['last_cleanup_timestamp']}")
    lines.append(f"Queue Health: {m['queue_health']}")
    if m["host_capabilities"]:
        lines.append("Host Capabilities: " + ", ".join(m["host_capabilities"]))
        lines.append(f"Host Focus: {m['host_focus_status']}")
        lines.append(f"Host Focus Status: {m['host_focus_status']}")
        lines.append(f"Host Capability Coverage: {m['host_capability_coverage']}")
        lines.append(f"Missing Focus Devices: {m['host_focus_missing']}")
        for name, details in m["host_focus_details"]:
            lines.append(f"  - {name}: {details}")
        for name, details in m["host_capability_details"]:
            if name not in m["host_focus_name_set"]:
                lines.append(f"  - {name}: {details or 'available'}")
    else:
        lines.append("Host Capabilities: none detected")
        lines.append("Host Focus: host hardware status unavailable")
    lines.insert(1, f"Focus: {m['focus_line']}")
    return "\n".join(lines)


def render_html(m: dict[str, Any]) -> str:
    rewards_html = format_reward_trend_html(m["reward_trend"])

    caps_html = m["host_capability_badges_html"]
    cap_details_html = m["host_capability_details_html"]
    summary_html = escape_html_text(m["dashboard_summary"])
    focus_line_html = escape_html_text(m["focus_line"])
    goal_html = escape_html_text(m["goal"])
    recent_cycles_html = escape_html_text(m["recent_cycles"])
    materialized_cycle_html = escape_html_text(m["materialized_cycle"])
    active_task_html = escape_html_text(m["active_task"])
    queue_action_html = escape_html_text(m["queue_action"])
    queue_archive_target_html = escape_html_text(m["queue_archive_target"])
    queue_priority_html = escape_html_text(m["queue_priority"])
    queue_hygiene_html = escape_html_text(m["queue_hygiene"])
    operator_attention_html = escape_html_text(m["operator_attention"])

    queue_pressure_html = escape_html_text(m["queue_pressure"])

    queue_freshness_html = escape_html_text(m["queue_freshness"])
    oldest_stale_path_html = escape_html_text(format_oldest_stale_request_path(m["oldest_stale_request_path"]))
    reward_momentum_html = escape_html_text(m["reward_momentum"])
    reward_average_html = escape_html_text(m["reward_average"])
    reward_range_html = escape_html_text(m["reward_range"])
    latest_report_status_html = escape_html_text(m["latest_report_status"])
    concrete_statement_html = escape_html_text(m["concrete_statement"])
    artifact_freshness_html = escape_html_text(m["artifact_freshness"])
    goal_artifact_signature_html = escape_html_text(m["goal_artifact_signature"])
    next_bounded_candidate_html = escape_html_text(m["next_bounded_candidate"])
    queue_health_html = escape_html_text(m["queue_health"])
    last_cleanup_timestamp_html = escape_html_text(m["last_cleanup_timestamp"])
    host_focus_status_html = escape_html_text(m["host_focus_status"])
    host_coverage_html = escape_html_text(m["host_capability_coverage"])
    host_missing_html = escape_html_text(m["host_focus_missing"])
    host_capability_probe_age_html = escape_html_text(m["host_capability_probe_age"])
    host_capability_probe_status_html = escape_html_text(m["host_capability_probe_status"])
    approval_gate_state_html = escape_html_text(m["approval_gate_state"])
    last_cleanup_count_html = escape_html_text(m["last_cleanup_count"])
    last_cleanup_recency_html = escape_html_text(m["last_cleanup_recency"])
    queue_depth_html = escape_html_text(m["queue_depth"])
    stale_queue_requests_html = escape_html_text(m["stale_queue_requests"])
    archived_count_html = escape_html_text(m["archived_count"])
    queue_snapshot_html = escape_html_text(m["queue_snapshot"])
    materialized_status_html = escape_html_text(m["materialized_status"])
    materialized_path_html = escape_html_text(m["materialized_path"]) if m["materialized_path"] else ""
    latest_report_path_html = escape_html_text(m["latest_report_path"]) if m["latest_report_path"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="15">

    <title>EeeBot Self-Evolving Runtime Dashboard</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #0f141c;
            color: #e1e6f0;
            margin: 0;
            padding: 20px;
        }}
        .container {{
            max-width: 1000px;
            margin: 0 auto;
        }}
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid #1f2937;
            padding-bottom: 15px;
            margin-bottom: 25px;
        }}
        h1 {{
            margin: 0;
            font-size: 24px;
            color: #38bdf8;
        }}
        .refresh-indicator {{
            font-size: 12px;
            color: #64748b;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 25px;
        }}
        .card {{
            background: #1e293b;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -1px rgba(0,0,0,0.06);
            border: 1px solid #334155;
        }}
        .card h2 {{
            margin-top: 0;
            margin-bottom: 15px;
            font-size: 16px;
            color: #94a3b8;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .metric {{
            font-size: 15px;
            line-height: 1.6;
        }}
        .metric-item {{
            margin-bottom: 10px;
        }}
        .metric-label {{
            color: #94a3b8;
            font-weight: 500;
        }}
        .metric-value {{
            color: #f8fafc;
            font-weight: bold;
        }}
        .trend-container {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }}
        .trend-badge {{
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 13px;
            color: white;
        }}
        .cap-tag {{
            display: inline-block;
            background: #0f172a;
            color: #38bdf8;
            border: 1px solid #1e293b;
            padding: 4px 8px;
            border-radius: 4px;
            margin-right: 5px;
            margin-bottom: 5px;
            font-size: 12px;
        }}
        .cap-list {{
            margin: 8px 0 0 18px;
            padding: 0;
            color: #cbd5e1;
            font-size: 13px;
            line-height: 1.5;
        }}
        .status-badge {{
            display: inline-block;
            background: #1e3a8a;
            color: #93c5fd;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }}
        .path {{
            font-family: monospace;
            font-size: 12px;
            background: #0f172a;
            padding: 4px;
            border-radius: 4px;
            word-break: break-all;
            color: #cbd5e1;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>EeeBot Self-Evolving Runtime</h1>
                <div style="font-size: 13px; color: #94a3b8; margin-top: 4px;">Host: eeepc (Tailscale Node)</div>
            </div>
            <div class="refresh-indicator">Auto-refreshing every 15s</div>
        </header>

        <div class="grid">
            <div class="card">
                <h2>Active Context</h2>
                <div class="metric">
                    <div class="metric-item">
                        <span class="metric-label">Summary:</span>
                        <div class="metric-value" style="color: #34d399; margin-top: 2px;">{summary_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Focus:</span>
                        <div class="metric-value" style="color: #60a5fa; margin-top: 2px;">{focus_line_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Goal:</span>
                        <div class="metric-value" style="color: #38bdf8; margin-top: 2px;">{goal_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Queue Snapshot:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal; color: #cbd5e1;">{queue_snapshot_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Recent Cycles:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal;">{recent_cycles_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Materialized Cycle:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal;">{materialized_cycle_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Active Task:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal;">{active_task_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Queue Action:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal; color: #34d399;">{queue_action_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Queue Priority:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal; color: #f59e0b;">{queue_priority_html}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Operator Attention:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal; color: #f8fafc;">{operator_attention_html}</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>Execution & Queues</h2>
                <div class="metric">
                    <div class="metric-item">
                        <span class="metric-label">Subagent Queue Depth:</span>
                        <span class="metric-value" style="color: #f59e0b;">{queue_depth_html} pending</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Stale Queue Requests (&gt;24h):</span>
                        <span class="metric-value" style="color: #ef4444;">{stale_queue_requests_html}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Queue Freshness:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{queue_freshness_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Queue Pressure:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{queue_pressure_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Queue Hygiene:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{queue_hygiene_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Queue Action:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #34d399;">{queue_action_html}</div>
                    </div>
                     <div class="metric-item">
                         <span class="metric-label">Queue Archive Target:</span>
                         <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{queue_archive_target_html}</div>
                     </div>
                     <div class="metric-item">
                         <span class="metric-label">Queue Priority:</span>
                         <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #f59e0b;">{queue_priority_html}</div>
                     </div>

                    <div class="metric-item">
                        <span class="metric-label">Operator Attention:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #f8fafc;">{operator_attention_html}</div>
                    </div>
                     <div class="metric-item">
                         <span class="metric-label">Oldest Stale Request Age:</span>
                         <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{escape_html_text(m['oldest_stale_request_age'])}</div>
                     </div>
                     <div class="metric-item">
                         <span class="metric-label">Oldest Stale Request Path:</span>
                         <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{oldest_stale_path_html}</div>
                     </div>

                    <div class="metric-item">
                        <span class="metric-label">Reward Momentum:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{reward_momentum_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Reward Average:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{reward_average_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Reward Range:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{reward_range_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Archived Requests:</span>
                        <span class="metric-value">{archived_count_html}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Approval Gate:</span>
                        <span class="status-badge">{approval_gate_state_html}</span>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>Recent Reward Signals</h2>
                <div class="trend-container">
                    {rewards_html}
                </div>
            </div>
        </div>

        <div class="grid">
            <div class="card" style="grid-column: span 2;">
                <h2>Materialized Output</h2>
                <div class="metric">
                    <div class="metric-item">
                        <span class="metric-label">Latest Improvement:</span>
                        <div class="metric-value" style="font-weight: normal; margin-top: 2px;">{materialized_status_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Concrete Statement:</span>
                        <div class="metric-value" style="font-weight: normal; margin-top: 2px; color: #34d399;">{concrete_statement_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Latest Report Status:</span>
                        <div class="metric-value" style="font-weight: normal; margin-top: 2px; color: #f59e0b;">{latest_report_status_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Artifact Freshness:</span>
                        <div class="metric-value" style="font-weight: normal; margin-top: 2px; color: #cbd5e1;">{artifact_freshness_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Goal Artifact Signature:</span>
                        <div class="metric-value" style="font-weight: normal; color: #a855f7;">{goal_artifact_signature_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Next Bounded Candidate:</span>
                        <div class="metric-value" style="font-weight: normal; color: #10b981;">{next_bounded_candidate_html}</div>
                    </div>
                    {f'<div class="metric-item"><span class="metric-label">Materialized Artifact Path:</span><div class="path">{materialized_path_html}</div></div>' if m["materialized_path"] else ''}
                    {f'<div class="metric-item"><span class="metric-label">Latest Report Path:</span><div class="path">{latest_report_path_html}</div></div>' if m["latest_report_path"] else ''}
                </div>
            </div>

            <div class="card">
                <h2>System Health</h2>
                <div class="metric">
                    <div class="metric-item">
                        <span class="metric-label">Last Cleanup Count:</span>
                        <span class="metric-value">{last_cleanup_count_html}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Last Cleanup Time:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #94a3b8;">{last_cleanup_timestamp_html}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Queue Health:</span>
                        <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{queue_health_html}</div>
                    </div>
                     <div class="metric-item">
                         <span class="metric-label">Last Cleanup Recency:</span>
                         <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{last_cleanup_recency_html}</div>
                     </div>

                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Host Capabilities:</span>
                         <div style="margin-top: 5px;">{caps_html}</div>
                         <div class="metric-value" style="margin-top: 6px; font-weight: normal; color: #cbd5e1;">{host_coverage_html}</div>
                         <div class="metric-value" style="margin-top: 4px; font-weight: normal; color: #94a3b8;">Probe: {host_capability_probe_age_html} ({host_capability_probe_status_html})</div>
                         <div class="metric-value" style="margin-top: 4px; font-weight: normal; color: #94a3b8;">Focus: {host_focus_status_html}</div>
                         <div class="metric-value" style="margin-top: 4px; font-weight: normal; color: #94a3b8;">Missing: {host_missing_html}</div>
                         <div style="margin-top: 8px;">{cap_details_html}</div>

                    </div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""


class DashboardHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            metrics = collect_metrics()
            html = render_html(metrics)
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress request logging to avoid cluttered console output
        pass


def serve(host: str, port: int) -> None:
    print(f"Starting dashboard web server on http://{host}:{port}")
    server = http.server.HTTPServer((host, port), DashboardHTTPRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard server.")
        sys.exit(0)


def main() -> None:
    if "--snapshot" in sys.argv or "-S" in sys.argv:
        metrics = collect_metrics()
        snapshot_path = write_snapshot(metrics)
        print(f"Snapshot written to {snapshot_path}")
        return

    if "--json" in sys.argv or "-j" in sys.argv:
        print(render_json(collect_metrics()))
        return

    if "--serve" in sys.argv or "-s" in sys.argv:
        port = 8080
        host = "0.0.0.0"

        # Simple arg parsing
        for i, arg in enumerate(sys.argv):
            if arg in ("--port", "-p") and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
            if arg in ("--host", "-h") and i + 1 < len(sys.argv):
                host = sys.argv[i + 1]

        serve(host, port)
    else:
        metrics = collect_metrics()
        print(render_cli(metrics))


if __name__ == "__main__":
    main()
