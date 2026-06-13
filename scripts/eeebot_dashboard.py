#!/usr/bin/env python3
"""Minimal status dashboard for the eeebot self-evolving runtime.

The dashboard intentionally stays dependency-free so it can run on the weak
host even when richer TUI libraries are unavailable.
Supports CLI plain text output and a web interface (--serve).
"""

from __future__ import annotations

import json
import sys
import http.server
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = REPO_ROOT / "state"
IMPROVEMENT_DIR = Path("/var/lib/eeepc-agent/self-evolving-agent/state/improvements")
REPORTS_DIR = Path("/var/lib/eeepc-agent/self-evolving-agent/state/reports")


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
        reports = sorted(REPORTS_DIR.glob("evolution-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return []
    trend: list[tuple[str, float]] = []
    for report in reports[:limit]:
        data = load_json(report, {})
        reward = data.get("reward_signal", {}).get("value")
        if isinstance(reward, (int, float)):
            trend.append((data.get("cycle_id", report.stem), float(reward)))
    return trend


def count_queue_depth() -> int:
    queue_root = STATE_DIR / "subagents"
    if not queue_root.exists():
        return 0
    try:
        return sum(1 for path in queue_root.rglob("*") if path.is_file() and "archive" not in path.parts)
    except Exception:
        return 0


def count_archived_requests() -> int:
    archive_root = STATE_DIR / "subagents" / "archive"
    if not archive_root.exists():
        return 0
    try:
        return sum(1 for path in archive_root.rglob("*") if path.is_file())
    except Exception:
        return 0


def format_reward_trend(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "no recent reward samples"
    return ", ".join(f"{cycle}={reward:.2f}" for cycle, reward in trend)


def format_recent_cycles(trend: list[tuple[str, float]]) -> str:
    if not trend:
        return "no recent cycles"
    return ", ".join(cycle for cycle, _ in trend)


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


def collect_metrics() -> dict[str, Any]:
    health = load_json(STATE_DIR / "current_health.json", {})
    materialized_path, materialized = load_latest_materialized()
    latest_report_path, latest_report = load_latest_report()
    recent_rewards = load_recent_rewards()
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
    if host_caps:
        available_caps = sorted([name for name, info in host_caps.items() if info.get("available")])

    return {
        "goal": goal,
        "active_task": active_task,
        "recent_cycles": format_recent_cycles(recent_rewards),
        "reward_trend": recent_rewards,
        "queue_depth": count_queue_depth(),
        "archived_count": count_archived_requests(),
        "approval_gate_state": approval_gate_state,
        "materialized_status": format_materialized_status(materialized),
        "goal_artifact_signature": format_goal_artifact_signature(materialized),
        "next_bounded_candidate": format_next_candidate(materialized),
        "materialized_path": str(materialized_path) if materialized_path else None,
        "latest_report_path": str(latest_report_path) if latest_report_path else None,
        "last_cleanup_count": health.get("last_subagent_cleanup_count", "unknown"),
        "last_cleanup_timestamp": health.get("last_subagent_cleanup_timestamp", "unknown"),
        "host_capabilities": available_caps,
    }


def render_cli(m: dict[str, Any]) -> str:
    lines = [
        "EeeBot Dashboard",
        f"Goal: {m['goal']}",
        f"Active Task: {m['active_task']}",
        f"Recent Cycles: {m['recent_cycles']}",
        f"Reward Trend: {format_reward_trend(m['reward_trend'])}",
        f"Subagent Queue Depth: {m['queue_depth']}",
        f"Archived Subagent Requests: {m['archived_count']}",
        f"Approval Gate State: {m['approval_gate_state']}",
        f"Materialized Improvement: {m['materialized_status']}",
        f"Goal Artifact Signature: {m['goal_artifact_signature']}",
        f"Next Bounded Candidate: {m['next_bounded_candidate']}",
    ]
    if m["materialized_path"]:
        lines.append(f"Materialized Artifact Path: {m['materialized_path']}")
    if m["latest_report_path"]:
        lines.append(f"Latest Report Path: {m['latest_report_path']}")
    lines.append(f"Last Cleanup Count: {m['last_cleanup_count']}")
    lines.append(f"Last Cleanup Timestamp: {m['last_cleanup_timestamp']}")
    if m["host_capabilities"]:
        lines.append("Host Capabilities: " + ", ".join(m["host_capabilities"]))
    else:
        lines.append("Host Capabilities: none detected")
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

    caps_html = "".join(f'<span class="cap-tag">{c}</span>' for c in m["host_capabilities"])
    if not caps_html:
        caps_html = "<em>None detected</em>"

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
                        <span class="metric-label">Goal:</span>
                        <div class="metric-value" style="color: #38bdf8; margin-top: 2px;">{m['goal']}</div>
                    </div>
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Active Task:</span>
                        <div class="metric-value" style="margin-top: 2px; font-weight: normal;">{m['active_task']}</div>
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
                    <div class="metric-item" style="margin-top: 15px;">
                        <span class="metric-label">Host Capabilities:</span>
                        <div style="margin-top: 5px;">{caps_html}</div>
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
