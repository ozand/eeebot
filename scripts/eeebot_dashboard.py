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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = REPO_ROOT / "state"
IMPROVEMENT_DIR = Path("/var/lib/eeepc-agent/self-evolving-agent/state/improvements")
REPORTS_DIR = Path("/var/lib/eeepc-agent/self-evolving-agent/state/reports")
METRICS_CACHE_TTL_SECONDS = 3.0
_METRICS_CACHE: dict[str, Any] = {"loaded_at": 0.0, "metrics": None}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def latest_file(directory: Path, pattern: str) -> Path | None:
    try:
        candidates = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0] if candidates else None
    except Exception:
        return None


def load_latest_materialized() -> tuple[Path | None, dict[str, Any]]:
    latest = latest_file(IMPROVEMENT_DIR, "materialized-cycle-*.json")
    if not latest:
        return None, {}
    return latest, load_json(latest, {})


def load_latest_report() -> tuple[Path | None, dict[str, Any]]:
    latest = latest_file(REPORTS_DIR, "evolution-*.json")
    if not latest:
        return None, {}
    return latest, load_json(latest, {})


def load_recent_rewards(limit: int = 5) -> list[tuple[str, float]]:
    try:
        reports = heapq.nlargest(
            limit,
            REPORTS_DIR.glob("evolution-*.json"),
            key=lambda p: p.stat().st_mtime,
        )
    except Exception:
        return []
    trend: list[tuple[str, float]] = []
    for report in reports:
        data = load_json(report, {})
        reward = data.get("reward_signal", {}).get("value")
        if isinstance(reward, (int, float)):
            trend.append((data.get("cycle_id", report.stem), float(reward)))
    return trend


def scan_subagent_tree_stats(hours: int = 24) -> tuple[int, int, float | None, int]:
    queue_root = STATE_DIR / "subagents"
    if not queue_root.exists():
        return 0, 0, None, 0
    cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
    queue_depth = 0
    stale_count = 0
    archived_count = 0
    oldest_stale: float | None = None
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
    except Exception:
        return 0, 0, None, 0
    oldest_stale_age_hours = None
    if oldest_stale is not None:
        oldest_stale_age_hours = (datetime.now(timezone.utc).timestamp() - oldest_stale) / 3600
    return queue_depth, stale_count, oldest_stale_age_hours, archived_count


def scan_queue_stats(hours: int = 24) -> tuple[int, int, float | None]:
    queue_depth, stale_count, oldest_stale_age_hours, _ = scan_subagent_tree_stats(hours)
    return queue_depth, stale_count, oldest_stale_age_hours


def count_queue_depth() -> int:
    queue_depth, _, _, _ = scan_subagent_tree_stats()
    return queue_depth


def count_stale_queue_requests(hours: int = 24) -> int:
    _, stale_count, _, _ = scan_subagent_tree_stats(hours)
    return stale_count


def count_archived_requests() -> int:
    _, _, _, archived_count = scan_subagent_tree_stats()
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


def format_operator_attention(m: dict[str, Any]) -> str:
    queue_depth = m["queue_depth"]
    stale = m["stale_queue_requests"]
    gate = m["approval_gate_state"]
    momentum = m["reward_momentum"]
    if queue_depth <= 0 and stale <= 0:
        queue_part = "queue idle"
    elif stale > 0:
        queue_part = f"{stale}/{queue_depth} stale"
    else:
        queue_part = f"{queue_depth} pending"
    return f"{queue_part} · gate={gate} · momentum={momentum}"


def format_dashboard_summary(m: dict[str, Any]) -> str:
    queue_depth = m["queue_depth"]
    stale = m["stale_queue_requests"]
    archived = m.get("archived_count", 0)
    if queue_depth <= 0 and stale <= 0:
        queue_part = "queue idle"
    elif stale > 0:
        queue_part = f"{stale}/{queue_depth} stale"
    else:
        queue_part = f"{queue_depth} pending"
    return (
        f"{queue_part} · host={m['host_capability_coverage']} · "
        f"cleanup={m['last_cleanup_recency']} · archived={archived} · gate={m['approval_gate_state']}"
    )


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


def format_cleanup_recency(last_cleanup_timestamp: Any) -> str:
    age_hours = _cleanup_age_hours(last_cleanup_timestamp)
    if age_hours is None:
        return "unknown"
    if age_hours < 0:
        return "future"
    if age_hours < 1:
        return f"{age_hours * 60:.0f}m ago"
    return f"{age_hours:.1f}h ago"


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
    last_cleanup_timestamp: Any,
) -> str:
    if queue_depth <= 0:
        return "no queue work pending"
    if stale_count > 0:
        age_text = format_oldest_stale_request_age(oldest_stale_age_hours)
        return f"archive {stale_count} stale request(s) — oldest {age_text}"
    if queue_depth >= 20:
        cleanup_text = str(last_cleanup_timestamp) if last_cleanup_timestamp else "unknown"
        return f"watch queue pressure — last cleanup {cleanup_text}"
    return "queue healthy; no archive action needed"




def format_oldest_stale_request_age(oldest_stale_age_hours: float | None) -> str:
    if oldest_stale_age_hours is None:
        return "unknown"
    return f"{oldest_stale_age_hours:.1f}h"


def format_age_hours(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    return f"{age_hours:.1f}h"


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


def format_latest_report_status(report: dict[str, Any]) -> str:
    if not report:
        return "no latest report loaded"
    result = str(report.get("reward_signal", {}).get("result_status", "unknown"))
    evidence = str(report.get("evidence_id", report.get("evidence", ""))).strip()
    if evidence:
        return f"{result} — evidence {evidence}"
    return result


def format_concrete_statement(materialized: dict[str, Any]) -> str:
    statement = str(materialized.get("concrete_improvement_statement", "")).strip()
    if statement:
        return statement
    return "no concrete improvement statement recorded"


def format_host_focus_status(host_caps: dict[str, Any]) -> str:
    focus_order = ("camera", "bluetooth", "wifi", "microphone")
    if not host_caps:
        return "host hardware status unavailable"

    parts: list[str] = []
    for name in focus_order:
        info = host_caps.get(name, {})
        available = bool(info.get("available"))
        symbol = "✓" if available else "✗"
        parts.append(f"{name}:{symbol}")
    return ", ".join(parts)


def format_host_focus_details(host_caps: dict[str, Any]) -> list[tuple[str, str]]:
    focus_order = ("camera", "bluetooth", "wifi", "microphone")
    details: list[tuple[str, str]] = []
    for name in focus_order:
        info = host_caps.get(name, {})
        if not isinstance(info, dict):
            continue
        details.append((name, str(info.get("details", "")).strip() or "available"))
    return details


def format_host_capability_coverage(host_caps: dict[str, Any]) -> str:
    focus_order = ("camera", "bluetooth", "wifi", "microphone")
    if not host_caps:
        return "host hardware status unavailable"
    available = sum(1 for name in focus_order if bool(host_caps.get(name, {}).get("available")))
    return f"{available}/{len(focus_order)} focus devices available"


def format_missing_host_focus_devices(host_caps: dict[str, Any]) -> str:
    focus_order = ("camera", "bluetooth", "wifi", "microphone")
    if not host_caps:
        return "host hardware status unavailable"
    missing = [name for name in focus_order if not bool(host_caps.get(name, {}).get("available"))]
    if not missing:
        return "none"
    return ", ".join(missing)


def collect_metrics_uncached() -> dict[str, Any]:
    health = load_json(STATE_DIR / "current_health.json", {})
    materialized_path, materialized = load_latest_materialized()
    latest_report_path, latest_report = load_latest_report()
    recent_rewards = load_recent_rewards()
    reward_momentum = format_reward_momentum(recent_rewards)
    host_caps = load_json(STATE_DIR / "host_capabilities.json", {})

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

    available_caps = []
    capability_details = []
    if host_caps:
        available_caps = sorted([name for name, info in host_caps.items() if info.get("available")])
        for name in available_caps:
            details = str(host_caps.get(name, {}).get("details", "")).strip()
            capability_details.append((name, details))

    host_focus_status = format_host_focus_status(host_caps)
    host_focus_details = format_host_focus_details(host_caps)
    host_focus_names = {name for name, _ in host_focus_details}
    host_capability_coverage = format_host_capability_coverage(host_caps)
    host_focus_missing = format_missing_host_focus_devices(host_caps)

    queue_depth, stale_queue_requests, oldest_stale_age_hours, archived_count = scan_subagent_tree_stats()

    last_cleanup_count = health.get("last_subagent_cleanup_count", "unknown")
    last_cleanup_timestamp = health.get("last_subagent_cleanup_timestamp", "unknown")
    last_cleanup_recency = format_cleanup_recency(last_cleanup_timestamp)
    queue_action = format_queue_action(
        queue_depth,
        stale_queue_requests,
        oldest_stale_age_hours,
        last_cleanup_timestamp,
    )

    queue_pressure = format_queue_pressure(queue_depth, stale_queue_requests, oldest_stale_age_hours)

    dashboard_summary = format_dashboard_summary({
        "queue_depth": queue_depth,
        "stale_queue_requests": stale_queue_requests,
        "approval_gate_state": approval_gate_state,
        "host_capability_coverage": host_capability_coverage,
        "last_cleanup_recency": last_cleanup_recency,
    })

    return {
        "goal": goal,
        "active_task": active_task,
        "recent_cycles": format_recent_cycles(recent_rewards),
        "reward_trend": recent_rewards,
        "reward_momentum": reward_momentum,
        "operator_attention": format_operator_attention({
            "queue_depth": queue_depth,
            "stale_queue_requests": stale_queue_requests,
            "approval_gate_state": approval_gate_state,
            "reward_momentum": reward_momentum,
        }),
        "dashboard_summary": dashboard_summary,
        "materialized_cycle": format_materialized_cycle(materialized),
        "queue_depth": queue_depth,
        "stale_queue_requests": stale_queue_requests,
        "queue_freshness": format_queue_freshness(queue_depth, stale_queue_requests),
        "queue_pressure": queue_pressure,
        "queue_action": queue_action,
        "oldest_stale_age_hours": oldest_stale_age_hours,
        "archived_count": archived_count,
        "approval_gate_state": approval_gate_state,
        "materialized_status": format_materialized_status(materialized),
        "concrete_statement": format_concrete_statement(materialized),
        "goal_artifact_signature": format_goal_artifact_signature(materialized),
        "latest_report_status": format_latest_report_status(latest_report),
        "next_bounded_candidate": format_next_candidate(materialized),
        "materialized_path": str(materialized_path) if materialized_path else None,
        "latest_report_path": str(latest_report_path) if latest_report_path else None,
        "last_cleanup_count": last_cleanup_count,
        "last_cleanup_timestamp": last_cleanup_timestamp,
        "queue_health": format_queue_health(health),
        "last_cleanup_recency": last_cleanup_recency,
        "host_capabilities": available_caps,
        "host_capability_details": capability_details,
        "host_focus_status": host_focus_status,
        "host_focus_details": host_focus_details,
        "host_focus_names": sorted(host_focus_names),
        "host_capability_coverage": host_capability_coverage,
        "host_focus_missing": host_focus_missing,
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


def write_snapshot(metrics: dict[str, Any], destination: Path | None = None) -> Path:
    destination = destination or Path(f"/tmp/eeebot-dashboard-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.txt")
    snapshot_lines = [
        "EeeBot Dashboard Snapshot",
        f"Captured At: {datetime.now(timezone.utc).isoformat()}",
        f"Goal: {metrics['goal']}",
        f"Active Task: {metrics['active_task']}",
        f"Recent Cycles: {metrics['recent_cycles']}",
        f"Reward Trend: {format_reward_trend(metrics['reward_trend'])}",
        f"Queue Pressure: {metrics['queue_pressure']}",
        f"Queue Action: {metrics['queue_action']}",
        f"Last Cleanup Recency: {metrics['last_cleanup_recency']}",
        f"Approval Gate State: {metrics['approval_gate_state']}",
        f"Materialized Improvement: {metrics['materialized_status']}",
        f"Concrete Statement: {metrics['concrete_statement']}",
        f"Latest Report Status: {metrics['latest_report_status']}",
        f"Host Focus: {metrics['host_focus_status']}",
        f"Host Capability Coverage: {metrics['host_capability_coverage']}",
        f"Missing Focus Devices: {metrics['host_focus_missing']}",
    ]
    destination.write_text("\n".join(snapshot_lines) + "\n", encoding="utf-8")
    return destination


def render_cli(m: dict[str, Any]) -> str:
    lines = [
        "EeeBot Dashboard",
        f"Summary: {m['dashboard_summary']}",
        f"Goal: {m['goal']}",
        f"Active Task: {m['active_task']}",
        f"Recent Cycles: {m['recent_cycles']}",
        f"Reward Trend: {format_reward_trend(m['reward_trend'])}",
        f"Reward Momentum: {m['reward_momentum']}",
        f"Subagent Queue Depth: {m['queue_depth']}",
        f"Stale Queue Requests (>24h): {m['stale_queue_requests']}",
         f"Queue Freshness: {m['queue_freshness']}",
         f"Queue Pressure: {m['queue_pressure']}",
         f"Queue Action: {m['queue_action']}",
         f"Operator Attention: {m['operator_attention']}",
         f"Oldest Stale Request Age: {format_oldest_stale_request_age(m['oldest_stale_age_hours'])}",
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
        lines.append(f"Host Capability Coverage: {m['host_capability_coverage']}")
        lines.append(f"Missing Focus Devices: {m['host_focus_missing']}")
        for name, details in m["host_focus_details"]:
            lines.append(f"  - {name}: {details}")
        for name, details in m["host_capability_details"]:
            if name not in m["host_focus_names"]:
                lines.append(f"  - {name}: {details or 'available'}")
    else:
        lines.append("Host Capabilities: none detected")
        lines.append("Host Focus: host hardware status unavailable")
    return "\n".join(lines)


def render_html(m: dict[str, Any]) -> str:
    rewards_html = ""
    for cycle, reward in m["reward_trend"]:
        color = "#2ecc71" if reward >= 1.2 else ("#3498db" if reward >= 1.0 else "#e74c3c")
        rewards_html += f"""
        <div class="trend-badge" style="background: {color};">
            <strong>{cycle}</strong>: {reward:.2f}
        </div>
        """
    if not rewards_html:
        rewards_html = "<em>No recent cycles</em>"

    caps_html = "".join(
        f'<span class="cap-tag">{html.escape(c)}</span>' for c in m["host_capabilities"]
    )
    if not caps_html:
        caps_html = "<em>None detected</em>"

    cap_details_html = "".join(
        f'<li><strong>{html.escape(name)}</strong>: {html.escape(details or "available")}</li>'
        for name, details in m["host_capability_details"]
    )
    if cap_details_html:
        cap_details_html = f"<ul class='cap-list'>{cap_details_html}</ul>"
    else:
        cap_details_html = "<em>No capability details available</em>"

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
                        <div class="metric-value" style="color: #34d399; margin-top: 2px;">{m['dashboard_summary']}</div>
                    </div>
                     <div class="metric-item" style="margin-top: 15px;">
                         <span class="metric-label">Goal:</span>
                         <div class="metric-value" style="color: #38bdf8; margin-top: 2px;">{m['goal']}</div>
                     </div>
                     <div class="metric-item" style="margin-top: 15px;">
                         <span class="metric-label">Recent Cycles:</span>
                         <div class="metric-value" style="margin-top: 2px; font-weight: normal;">{m['recent_cycles']}</div>
                     </div>
                     <div class="metric-item" style="margin-top: 15px;">
                         <span class="metric-label">Materialized Cycle:</span>
                         <div class="metric-value" style="margin-top: 2px; font-weight: normal;">{m['materialized_cycle']}</div>
                     </div>

                     <div class="metric-item" style="margin-top: 15px;">
                         <span class="metric-label">Active Task:</span>
                         <div class="metric-value" style="margin-top: 2px; font-weight: normal;">{m['active_task']}</div>
                     </div>
                     <div class="metric-item" style="margin-top: 15px;">
                         <span class="metric-label">Queue Action:</span>
                         <div class="metric-value" style="margin-top: 2px; font-weight: normal; color: #34d399;">{m['queue_action']}</div>
                     </div>
                     <div class="metric-item" style="margin-top: 15px;">
                         <span class="metric-label">Operator Attention:</span>
                         <div class="metric-value" style="margin-top: 2px; font-weight: normal; color: #f8fafc;">{m['operator_attention']}</div>
                     </div>
                 </div>
             </div>

            </div>

            <div class="card">
                <h2>Execution & Queues</h2>
                <div class="metric">
                      <div class="metric-item">
                          <span class="metric-label">Subagent Queue Depth:</span>
                          <span class="metric-value" style="color: #f59e0b;">{m['queue_depth']} pending</span>
                      </div>
                      <div class="metric-item">
                          <span class="metric-label">Stale Queue Requests (&gt;24h):</span>
                          <span class="metric-value" style="color: #ef4444;">{m['stale_queue_requests']}</span>
                      </div>
                       <div class="metric-item">
                           <span class="metric-label">Queue Freshness:</span>
                           <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{m['queue_freshness']}</div>
                       </div>
                       <div class="metric-item">
                           <span class="metric-label">Queue Pressure:</span>
                           <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{html.escape(m['queue_pressure'])}</div>
                       </div>
                       <div class="metric-item">
                           <span class="metric-label">Queue Action:</span>
                           <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #34d399;">{html.escape(m['queue_action'])}</div>
                       </div>
                       <div class="metric-item">
                           <span class="metric-label">Operator Attention:</span>
                           <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #f8fafc;">{html.escape(m['operator_attention'])}</div>
                       </div>
                       <div class="metric-item">
                           <span class="metric-label">Reward Momentum:</span>


                         <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{html.escape(m['reward_momentum'])}</div>
                     </div>

                    <div class="metric-item">
                        <span class="metric-label">Archived Requests:</span>
                        <span class="metric-value">{m['archived_count']}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Approval Gate:</span>
                        <span class="status-badge">{m['approval_gate_state']}</span>
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
                         <div class="metric-value" style="font-weight: normal; margin-top: 2px;">{m['materialized_status']}</div>
                     </div>
                      <div class="metric-item">
                          <span class="metric-label">Concrete Statement:</span>
                          <div class="metric-value" style="font-weight: normal; margin-top: 2px; color: #34d399;">{m['concrete_statement']}</div>
                      </div>
                      <div class="metric-item">
                          <span class="metric-label">Latest Report Status:</span>
                          <div class="metric-value" style="font-weight: normal; margin-top: 2px; color: #f59e0b;">{m['latest_report_status']}</div>
                      </div>

                     <div class="metric-item">

                        <span class="metric-label">Goal Artifact Signature:</span>
                        <div class="metric-value" style="font-weight: normal; color: #a855f7;">{m['goal_artifact_signature']}</div>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Next Bounded Candidate:</span>
                        <div class="metric-value" style="font-weight: normal; color: #10b981;">{m['next_bounded_candidate']}</div>
                    </div>
                    {f'<div class="metric-item"><span class="metric-label">Artifact Path:</span><div class="path">{m["materialized_path"]}</div></div>' if m["materialized_path"] else ''}
                </div>
            </div>

            <div class="card">
                <h2>System Health</h2>
                <div class="metric">
                    <div class="metric-item">
                        <span class="metric-label">Last Cleanup Count:</span>
                        <span class="metric-value">{m['last_cleanup_count']}</span>
                    </div>
                     <div class="metric-item">
                         <span class="metric-label">Last Cleanup Time:</span>
                         <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #94a3b8;">{m['last_cleanup_timestamp']}</div>
                     </div>
                      <div class="metric-item">
                          <span class="metric-label">Queue Health:</span>
                          <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{m['queue_health']}</div>
                      </div>
                      <div class="metric-item">
                          <span class="metric-label">Last Cleanup Recency:</span>
                          <div class="metric-value" style="font-size: 13px; font-weight: normal; color: #cbd5e1;">{m['last_cleanup_recency']}</div>
                      </div>
                       <div class="metric-item" style="margin-top: 15px;">

                          <span class="metric-label">Host Capabilities:</span>

                         <div style="margin-top: 5px;">{caps_html}</div>
                          <div class="metric-value" style="margin-top: 6px; font-weight: normal; color: #cbd5e1;">{m['host_capability_coverage']}</div>
                          <div class="metric-value" style="margin-top: 4px; font-weight: normal; color: #94a3b8;">Missing: {m['host_focus_missing']}</div>

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
