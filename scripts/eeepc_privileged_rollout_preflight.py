#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

DEFAULT_STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"
DEFAULT_NANOBOT = "/home/opencode/.venvs/nanobot/bin/nanobot"
DEFAULT_OPENCODE_HOME = "/home/opencode"


def run_command(args: Sequence[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)


def _ssh_args(host: str, remote_command: str, *, key: str | None = None) -> list[str]:
    args = ["ssh", "-F", str(Path.home() / ".ssh" / "config")]
    if key:
        args.extend(["-i", key, "-o", "IdentitiesOnly=yes"])
    args.extend(["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote_command])
    return args


def _quote(value: str) -> str:
    return shlex.quote(value)


def _ssh(host: str, remote_command: str, *, key: str | None = None, timeout: int = 20) -> dict[str, Any]:
    proc = run_command(_ssh_args(host, remote_command, key=key), timeout=timeout)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "command": remote_command,
    }


def _sudo(command: str) -> str:
    return f"sudo -n {command}"


def _with_via(result: dict[str, Any], via: str) -> dict[str, Any]:
    enriched = dict(result)
    enriched["via"] = via
    return enriched


def _effective_check(host: str, direct_command: str, sudo_command: str, *, sudo_available: bool, key: str | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    direct = _ssh(host, direct_command, key=key)
    if direct["ok"]:
        return _with_via(direct, "direct"), direct, None
    sudo_result = None
    if sudo_available:
        sudo_result = _ssh(host, _sudo(sudo_command), key=key)
        if sudo_result["ok"]:
            return _with_via(sudo_result, "sudo"), direct, sudo_result
    return _with_via(sudo_result or direct, "sudo" if sudo_available else "direct"), direct, sudo_result


def _read_latest_report(host: str, state_root: str, *, key: str | None = None, sudo_available: bool = False) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    reports_glob = f"{_quote(state_root)}/reports/evolution-*.json"
    list_command = f"sh -lc {_quote(f'ls -1t {reports_glob} 2>/dev/null | head -n 1')}"
    path_result = _ssh(host, _sudo(list_command) if sudo_available else list_command, key=key)
    report_path = path_result["stdout"].splitlines()[0] if path_result["ok"] and path_result["stdout"] else None
    if not report_path:
        return None, None, path_result
    cat_command = f"cat {_quote(report_path)}"
    cat_result = _ssh(host, _sudo(cat_command) if sudo_available else cat_command, key=key)
    if not cat_result["ok"]:
        return report_path, None, cat_result
    try:
        return report_path, json.loads(cat_result["stdout"]), None
    except json.JSONDecodeError as exc:
        return report_path, None, {"ok": False, "message": str(exc), "stage": "parse_latest_report", "path": report_path}


def build_preflight(*, host: str, state_root: str = DEFAULT_STATE_ROOT, key: str | None = None, nanobot_path: str = DEFAULT_NANOBOT, opencode_home: str = DEFAULT_OPENCODE_HOME) -> dict[str, Any]:
    collected_at = datetime.now(timezone.utc).isoformat()
    ssh_probe = _ssh(host, "hostname; whoami; id", key=key)
    blockers: list[str] = []
    checks: dict[str, Any] = {"ssh": ssh_probe}
    if not ssh_probe["ok"]:
        blockers.append("ssh_reachability")
        return {
            "schema_version": "eeepc-privileged-rollout-preflight-v1",
            "collected_at_utc": collected_at,
            "host": host,
            "state_root": state_root,
            "state": "blocked_unreachable",
            "ready": False,
            "blocked_capabilities": blockers,
            "checks": checks,
            "latest_report": None,
        }

    sudo = _ssh(host, "sudo -n true", key=key)
    sudo_available = bool(sudo["ok"])
    nanobot_command = f"test -x {_quote(nanobot_path)} && test -x {_quote(opencode_home)}"
    outbox_command = f"test -r {_quote(state_root + '/outbox/report.index.json')}"
    goals_command = f"test -r {_quote(state_root + '/goals/registry.json')}"
    opencode_home_check, direct_opencode_home_check, sudo_opencode_home_check = _effective_check(
        host, nanobot_command, f"sh -lc {_quote(nanobot_command)}", sudo_available=sudo_available, key=key
    )
    outbox, direct_outbox, sudo_outbox = _effective_check(host, outbox_command, outbox_command, sudo_available=sudo_available, key=key)
    goals, direct_goals, sudo_goals = _effective_check(host, goals_command, goals_command, sudo_available=sudo_available, key=key)
    checks.update({
        "sudo_noninteractive": sudo,
        "opencode_nanobot_executable": opencode_home_check,
        "direct_opencode_nanobot_executable": direct_opencode_home_check,
        "sudo_opencode_nanobot_executable": sudo_opencode_home_check,
        "read_authority_outbox": outbox,
        "direct_read_authority_outbox": direct_outbox,
        "sudo_read_authority_outbox": sudo_outbox,
        "read_goal_registry": goals,
        "direct_read_goal_registry": direct_goals,
        "sudo_read_goal_registry": sudo_goals,
    })
    if not sudo_available:
        blockers.append("sudo_noninteractive")
    if not opencode_home_check["ok"]:
        blockers.append("execute_opencode_nanobot")
    if not outbox["ok"]:
        blockers.append("read_authority_outbox")
    if not goals["ok"]:
        blockers.append("read_goal_registry")

    report_path, report_payload, report_error = _read_latest_report(host, state_root, key=key, sudo_available=sudo_available)
    latest_report = None
    if report_payload:
        latest_report = {
            "path": report_path,
            "result_status": report_payload.get("result_status") or report_payload.get("status"),
            "goal_id": report_payload.get("goal_id") or ((report_payload.get("goal") or {}).get("goal_id") if isinstance(report_payload.get("goal"), dict) else None),
            "feedback_decision_present": report_payload.get("feedback_decision") is not None,
            "selected_tasks": report_payload.get("selected_tasks"),
            "task_selection_source": report_payload.get("task_selection_source"),
        }
    checks["latest_readable_report"] = {"path": report_path, "ok": bool(report_payload), "error": report_error}

    state = "ready" if not blockers else ("partial_report_only" if latest_report else "blocked_privileged_access")
    if blockers:
        state = "blocked_privileged_access"
    return {
        "schema_version": "eeepc-privileged-rollout-preflight-v1",
        "collected_at_utc": collected_at,
        "host": host,
        "state_root": state_root,
        "state": state,
        "ready": not blockers,
        "blocked_capabilities": sorted(set(blockers)),
        "available_partial_proof": "latest_readable_report" if latest_report else None,
        "checks": checks,
        "latest_report": latest_report,
        "does_not_mutate_host": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit a JSON eeepc privileged rollout preflight artifact without mutating the host.")
    parser.add_argument("--host", default="eeepc")
    parser.add_argument("--state-root", default=DEFAULT_STATE_ROOT)
    parser.add_argument("--ssh-key", default=str(Path.home() / ".ssh" / "id_ed25519_eeepc"))
    parser.add_argument("--nanobot-path", default=DEFAULT_NANOBOT)
    parser.add_argument("--opencode-home", default=DEFAULT_OPENCODE_HOME)
    parser.add_argument("--json", action="store_true", help="Accepted for explicitness; output is always JSON.")
    args = parser.parse_args(argv)
    payload = build_preflight(host=args.host, state_root=args.state_root, key=args.ssh_key, nanobot_path=args.nanobot_path, opencode_home=args.opencode_home)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
