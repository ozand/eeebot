"""Cycle health summary helpers for eeebot runtime state."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from nanobot.runtime.state import _json_files_sorted_by_mtime, load_runtime_state_from_root
from nanobot.runtime.state_access import ledger_window
from nanobot.runtime.schemas import CycleHealth

_DEFAULT_BRIDGE_SERVICE = "eeepc-self-evolving-subagent-bridge.service"
_SELFEVO_REPO_DIRNAME = "eeebot-self-evolving"
# One day without a ledger append means the loop is not running; the same
# horizon the retired report read used (#1222).
_LEDGER_STALE_AFTER_SECONDS = 86400
# The bridge attempts roughly one cycle every four minutes. Fifteen missed
# integrations is one hour: enough to expose a live failure streak without
# treating one transient failure as an outage.
_PROGRESS_CADENCE_SECONDS = 15 * 60
_PROGRESS_THRESHOLD_CYCLES = 4
_PROGRESS_THRESHOLD_HOURS = (_PROGRESS_CADENCE_SECONDS * _PROGRESS_THRESHOLD_CYCLES) / 3600

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _ledger_age_seconds(state_root: Path, *, now: float | None = None) -> float | None:
    """Seconds since ``ledger/cycles.jsonl`` was last appended to, or ``None``
    when the ledger is absent/unreadable. The bridge appends one event per
    cycle (``cycle_ledger.append_event``), so the file's mtime is the loop's
    heartbeat. Fail-open: never raises."""
    try:
        mtime = (state_root / "ledger" / "cycles.jsonl").stat().st_mtime
    except OSError:
        return None
    return max(0.0, (now if now is not None else time.time()) - mtime)


def read_cycle_progress(state_root: Path, *, now: float | None = None) -> dict[str, Any]:
    """Read progress from outcome rows, distinct from ledger-write activity."""
    reference = now if now is not None else time.time()
    since = datetime.fromtimestamp(reference, tz=timezone.utc) - timedelta(days=90)
    window = ledger_window(
        state_root,
        since_ts=since.isoformat().replace("+00:00", "Z"),
        phases=frozenset({"outcome"}),
    )
    rows = list(window.rows)
    if window.status == "unavailable" or not rows:
        return _unavailable_progress()
    success_rows = [
        row for row in rows
        if row.get("outcome") == "success" and _parse_ledger_timestamp(row.get("ts")) is not None
    ]
    last_success_ts = max((_parse_ledger_timestamp(row.get("ts")) for row in success_rows), default=None)
    hours_since = max(0.0, (reference - last_success_ts) / 3600) if last_success_ts is not None else None
    trailing: list[dict[str, Any]] = []
    for row in reversed(rows):
        if row.get("outcome") == "success" and _parse_ledger_timestamp(row.get("ts")) is not None:
            break
        trailing.append(row)
    reasons: dict[str, int] = {}
    for row in trailing:
        reason = str(row.get("reason") or ("invalid_success_timestamp" if row.get("outcome") == "success" else row.get("outcome")) or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
    dominant_reason = max(reasons, key=lambda reason: (reasons[reason], reason)) if reasons else None
    cycle_alert = len(trailing) >= _PROGRESS_THRESHOLD_CYCLES
    time_alert = bool(trailing) and last_success_ts is not None and hours_since >= _PROGRESS_THRESHOLD_HOURS
    state = "stalled" if cycle_alert or time_alert else "no_success_yet" if last_success_ts is None else "healthy"
    return {"state": state, "alert": state == "stalled" if state != "no_success_yet" else False, "hours_since_last_success": hours_since, "last_success_ts": _format_ledger_timestamp(last_success_ts), "consecutive_non_integrating_cycles": len(trailing), "dominant_reason": dominant_reason, "threshold_cycles": _PROGRESS_THRESHOLD_CYCLES, "threshold_hours": _PROGRESS_THRESHOLD_HOURS, "cadence_minutes": _PROGRESS_CADENCE_SECONDS / 60}


def _parse_ledger_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _format_ledger_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _unavailable_progress() -> dict[str, Any]:
    return {"state": "unavailable", "alert": None, "hours_since_last_success": None, "last_success_ts": None, "consecutive_non_integrating_cycles": None, "dominant_reason": None, "threshold_cycles": _PROGRESS_THRESHOLD_CYCLES, "threshold_hours": _PROGRESS_THRESHOLD_HOURS, "cadence_minutes": _PROGRESS_CADENCE_SECONDS / 60}


def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=10, check=False)


def _run_systemctl(runner: CommandRunner, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(["systemctl", *args, "--no-pager"])
    except Exception:
        return None


def read_service_status(
    service_name: str = _DEFAULT_BRIDGE_SERVICE,
    *,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Read a compact systemd service snapshot without raising on non-systemd hosts."""
    run = runner or _default_runner
    active = _run_systemctl(run, ["is-active", service_name])
    result = _run_systemctl(run, ["show", service_name, "-p", "Result", "-p", "ActiveState", "-p", "SubState"])

    details: dict[str, str] = {}
    if result and result.returncode == 0:
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                details[key] = value

    active_state = (active.stdout.strip() if active and active.stdout.strip() else None) or details.get("ActiveState")
    return {
        "service": service_name,
        "active_state": active_state or "unknown",
        "sub_state": details.get("SubState") or "unknown",
        "result": details.get("Result") or "unknown",
        "available": bool(active and active.returncode in {0, 3} or result and result.returncode == 0),
    }


def read_failed_units_count(*, runner: CommandRunner | None = None) -> int | None:
    """Return the number of failed systemd units, or None when unavailable."""
    run = runner or _default_runner
    proc = _run_systemctl(run, ["--failed", "--plain", "--legend=false"])
    if not proc or proc.returncode not in {0, 1}:
        return None
    return len([line for line in proc.stdout.splitlines() if line.strip()])


def _promotion_readiness(runtime: dict[str, Any]) -> dict[str, Any]:
    replay = runtime.get("promotion_replay_readiness")
    if isinstance(replay, dict):
        return {
            "state": replay.get("state") or "unknown",
            "reason": replay.get("reason"),
            "recommended_next_action": replay.get("recommended_next_action"),
        }
    candidate_id = runtime.get("promotion_candidate_id")
    if candidate_id:
        return {
            "state": runtime.get("review_status") or runtime.get("decision") or "pending",
            "reason": runtime.get("decision_reason"),
            "recommended_next_action": "review_promotion_candidate",
        }
    return {
        "state": "absent",
        "reason": "no_promotion_candidate",
        "recommended_next_action": "run_bounded_cycle_or_generate_candidate",
    }


def _recommended_next_action(
    *,
    runtime: dict[str, Any],
    service_status: dict[str, Any],
    failed_units_count: int | None,
    promotion_readiness: dict[str, Any],
) -> str:
    if failed_units_count and failed_units_count > 0:
        return "inspect_failed_systemd_units"
    if service_status.get("active_state") not in {"active", "inactive", "unknown"}:
        return "inspect_bridge_service_status"
    promo_action = promotion_readiness.get("recommended_next_action")
    if promo_action and promotion_readiness.get("state") not in {"absent", "ready"}:
        return str(promo_action)
    material = runtime.get("material_progress") if isinstance(runtime.get("material_progress"), dict) else {}
    if material.get("state") in {"missing", "blocked"}:
        return str(material.get("blocking_reason") or "select_small_improvement_task")
    return "observe_next_timer_cycle"


def _calculate_severity(
    *,
    runtime: dict[str, Any],
    service_status: dict[str, Any],
    failed_units_count: int | None,
    promotion_readiness: dict[str, Any],
) -> tuple[str, int]:
    if runtime.get("runtime_state_unavailable") or runtime.get("runtime_state_stale"):
        return "unknown", 3

    progress = runtime.get("cycle_progress") if isinstance(runtime.get("cycle_progress"), dict) else {}
    if progress.get("state") == "stalled":
        return "blocked", 2
    if progress.get("state") == "unavailable":
        return "unknown", 3

    if failed_units_count and failed_units_count > 0:
        return "blocked", 2
    
    if service_status.get("active_state") == "failed" or service_status.get("result") not in {"success", "unknown"}:
        return "blocked", 2

    promo_state = promotion_readiness.get("state")
    if promo_state in {"absent", "not_ready", "blocked"}:
        return "degraded", 1

    host_resources = runtime.get("host_resources")
    if isinstance(host_resources, dict) and host_resources.get("weak_host_signals"):
        return "degraded", 1

    material = runtime.get("material_progress") if isinstance(runtime.get("material_progress"), dict) else {}
    if material.get("state") in {"missing", "blocked"}:
        return "degraded", 1

    return "ok", 0


def read_autonomous_commits_24h(
    state_root: Path,
    *,
    runner: CommandRunner | None = None,
) -> int | None:
    """Count commits in the last 24h in the selfevo executor repo.

    The selfevo repo lives alongside the state root at
    ``state_root.parent / "eeebot-self-evolving"`` (same sibling-layout
    convention used by ``coordinator._has_concrete_changes`` and
    ``coordinator._parse_backlog_task_from_memory``). Fails soft: returns
    None when the repo is absent or git errors, never raises.
    """
    selfevo_repo = state_root.parent / _SELFEVO_REPO_DIRNAME
    if not selfevo_repo.is_dir():
        return None
    run = runner or _default_runner
    try:
        proc = run([
            "git", "-c", f"safe.directory={selfevo_repo}",
            "-C", str(selfevo_repo),
            "log", "--oneline", "--since=24 hours ago",
        ])
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return len([line for line in proc.stdout.splitlines() if line.strip()])


def read_subagent_queue_depth(state_root: Path) -> int:
    """Count pending subagent request files under state/subagents/requests/."""
    requests_dir = state_root / "subagents" / "requests"
    return len(list(_json_files_sorted_by_mtime(False, requests_dir)))


def build_cycle_health_summary(
    state_root: Path,
    *,
    source_kind: str = "host_control_plane",
    service_name: str = _DEFAULT_BRIDGE_SERVICE,
    runner: CommandRunner | None = None,
) -> CycleHealth:
    """Build an operator-friendly health summary from runtime state and systemd."""
    runtime = load_runtime_state_from_root(state_root, source_kind=source_kind)
    # #1222: liveness comes from the cycle ledger, the file the bridge appends
    # to every cycle — not from reports/evolution-*.json, which the deleted
    # coordinator last wrote on 2026-08-22 (so "stale" was a constant).
    ledger_age = _ledger_age_seconds(state_root)
    progress = read_cycle_progress(state_root)
    runtime["runtime_state_unavailable"] = ledger_age is None
    runtime["runtime_state_stale"] = ledger_age is not None and ledger_age > _LEDGER_STALE_AFTER_SECONDS
    runtime["cycle_progress"] = progress
    live = runtime.get("live") if isinstance(runtime.get("live"), dict) else {}
    recent = live.get("recent_outcomes") if isinstance(live.get("recent_outcomes"), list) else []
    latest_cycle_id = recent[0].get("cycle_id") if recent and isinstance(recent[0], dict) else None
    service_status = read_service_status(service_name, runner=runner)
    failed_units_count = read_failed_units_count(runner=runner)
    promotion_readiness = _promotion_readiness(runtime)
    autonomous_commits_24h = read_autonomous_commits_24h(state_root, runner=runner)
    subagent_queue_depth = read_subagent_queue_depth(state_root)
    summary = {
        "schema_version": "cycle-health-summary-v2",
        "runtime_state_source": runtime.get("runtime_state_source"),
        "runtime_state_root": runtime.get("runtime_state_root"),
        "latest_cycle_id": latest_cycle_id,
        "ledger_age_seconds": ledger_age,
        "progress": progress,
        "latest_subagent_telemetry_id": runtime.get("subagent_telemetry_latest_id"),
        "latest_subagent_telemetry_path": runtime.get("subagent_telemetry_path"),
        "service_status": service_status,
        "failed_units_count": failed_units_count,
        "promotion_readiness": promotion_readiness,
        "success_signals": {
            "autonomous_commits_24h": autonomous_commits_24h,
            "subagent_queue_depth": subagent_queue_depth,
        },
    }

    severity, exit_code = _calculate_severity(
        runtime=runtime,
        service_status=service_status,
        failed_units_count=failed_units_count,
        promotion_readiness=promotion_readiness,
    )
    
    summary["severity"] = severity
    summary["exit_code"] = exit_code
    summary["next_recommended_action"] = _recommended_next_action(
        runtime=runtime,
        service_status=service_status,
        failed_units_count=failed_units_count,
        promotion_readiness=promotion_readiness,
    )
    
    return summary


def format_cycle_health_summary(summary: CycleHealth) -> list[str]:
    """Format cycle health summary as stable text lines."""
    service = summary.get("service_status") if isinstance(summary.get("service_status"), dict) else {}
    promotion = summary.get("promotion_readiness") if isinstance(summary.get("promotion_readiness"), dict) else {}
    success_signals = summary.get("success_signals") if isinstance(summary.get("success_signals"), dict) else {}
    return [
        "Cycle health summary:",
        f"  Severity: {summary.get('severity') or 'unknown'} (exit_code={summary.get('exit_code')})",
        f"  Runtime state source: {summary.get('runtime_state_source') or 'unknown'}",
        f"  Runtime state root: {summary.get('runtime_state_root') or 'unknown'}",
        f"  Latest cycle id: {summary.get('latest_cycle_id') or 'unknown'}",
        "  Ledger age: "
        + (f"{summary.get('ledger_age_seconds') / 3600:.1f}h" if isinstance(summary.get('ledger_age_seconds'), (int, float)) else "no ledger"),
        "  Progress: " + _format_progress_line(summary.get("progress")),
        "  Progress threshold: " + _format_progress_threshold(summary.get("progress")),
        f"  Latest subagent telemetry id: {summary.get('latest_subagent_telemetry_id') or 'unknown'}",
        f"  Latest subagent telemetry path: {summary.get('latest_subagent_telemetry_path') or 'unknown'}",
        "  Service status: "
        f"{service.get('service') or 'unknown'} "
        f"active={service.get('active_state') or 'unknown'} "
        f"sub={service.get('sub_state') or 'unknown'} "
        f"result={service.get('result') or 'unknown'}",
        f"  Failed units count: {summary.get('failed_units_count') if summary.get('failed_units_count') is not None else 'unknown'}",
        "  Promotion readiness: "
        f"state={promotion.get('state') or 'unknown'} "
        f"reason={promotion.get('reason') or 'none'}",
        f"  Next recommended action: {summary.get('next_recommended_action') or 'unknown'}",
        "  Success signals: "
        f"autonomous_commits_24h={success_signals.get('autonomous_commits_24h') if success_signals.get('autonomous_commits_24h') is not None else 'unavailable'} "
        f"subagent_queue_depth={success_signals.get('subagent_queue_depth') if success_signals.get('subagent_queue_depth') is not None else 'unknown'}",
    ]


def _format_progress_line(progress: Any) -> str:
    if not isinstance(progress, dict) or progress.get("state") == "unavailable":
        return "unavailable"
    hours = progress.get("hours_since_last_success")
    cycles = progress.get("consecutive_non_integrating_cycles")
    reason = progress.get("dominant_reason") or "none"
    if progress.get("state") == "no_success_yet":
        return f"no success yet; non_integrating_cycles={cycles}; dominant_reason={reason}"
    return f"{hours:.1f}h since success; non_integrating_cycles={cycles}; dominant_reason={reason}"


def _format_progress_threshold(progress: Any) -> str:
    if not isinstance(progress, dict):
        return "unavailable"
    return f"{progress.get('threshold_hours', _PROGRESS_THRESHOLD_HOURS):.1f}h / {progress.get('threshold_cycles', _PROGRESS_THRESHOLD_CYCLES)} cycles at {progress.get('cadence_minutes', 4):.0f}m cadence"


def dumps_cycle_health_summary(summary: CycleHealth) -> str:
    return json.dumps(summary, indent=2, ensure_ascii=False)
