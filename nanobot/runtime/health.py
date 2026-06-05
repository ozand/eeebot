"""Cycle health summary helpers for eeebot runtime state."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from nanobot.runtime.state import load_runtime_state_from_root

_DEFAULT_BRIDGE_SERVICE = "eeepc-self-evolving-subagent-bridge.service"

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


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
    if runtime.get("next_hint") and runtime.get("next_hint") != "none":
        return str(runtime["next_hint"])
    promo_action = promotion_readiness.get("recommended_next_action")
    if promo_action and promotion_readiness.get("state") not in {"absent", "ready"}:
        return str(promo_action)
    material = runtime.get("material_progress") if isinstance(runtime.get("material_progress"), dict) else {}
    if material.get("state") in {"missing", "blocked"}:
        return str(material.get("blocking_reason") or "select_small_improvement_task")
    return "observe_next_timer_cycle"


def build_cycle_health_summary(
    state_root: Path,
    *,
    source_kind: str = "host_control_plane",
    service_name: str = _DEFAULT_BRIDGE_SERVICE,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Build an operator-friendly health summary from runtime state and systemd."""
    runtime = load_runtime_state_from_root(state_root, source_kind=source_kind)
    service_status = read_service_status(service_name, runner=runner)
    failed_units_count = read_failed_units_count(runner=runner)
    promotion_readiness = _promotion_readiness(runtime)
    summary = {
        "schema_version": "cycle-health-summary-v1",
        "runtime_state_source": runtime.get("runtime_state_source"),
        "runtime_state_root": runtime.get("runtime_state_root"),
        "latest_cycle_id": runtime.get("cycle_id"),
        "latest_report_path": runtime.get("report_path"),
        "latest_subagent_telemetry_id": runtime.get("subagent_telemetry_latest_id"),
        "latest_subagent_telemetry_path": runtime.get("subagent_telemetry_path"),
        "service_status": service_status,
        "failed_units_count": failed_units_count,
        "promotion_readiness": promotion_readiness,
        "next_recommended_action": _recommended_next_action(
            runtime=runtime,
            service_status=service_status,
            failed_units_count=failed_units_count,
            promotion_readiness=promotion_readiness,
        ),
    }
    return summary


def format_cycle_health_summary(summary: dict[str, Any]) -> list[str]:
    """Format cycle health summary as stable text lines."""
    service = summary.get("service_status") if isinstance(summary.get("service_status"), dict) else {}
    promotion = summary.get("promotion_readiness") if isinstance(summary.get("promotion_readiness"), dict) else {}
    return [
        "Cycle health summary:",
        f"  Runtime state source: {summary.get('runtime_state_source') or 'unknown'}",
        f"  Runtime state root: {summary.get('runtime_state_root') or 'unknown'}",
        f"  Latest cycle id: {summary.get('latest_cycle_id') or 'unknown'}",
        f"  Latest report path: {summary.get('latest_report_path') or 'unknown'}",
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
    ]


def dumps_cycle_health_summary(summary: dict[str, Any]) -> str:
    return json.dumps(summary, indent=2, ensure_ascii=False)
