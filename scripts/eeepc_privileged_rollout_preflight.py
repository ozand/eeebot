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
DEFAULT_SELF_EVOLVING_BASE = "/opt/eeepc-agent/runtimes/self-evolving-agent"


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


def _parse_key_value_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _self_evolving_venv_guard_command(self_evolving_base: str) -> str:
    base = _quote(self_evolving_base)
    script = (
        "echo EEEBOT_VENV_GUARD=1; "
        f"BASE={base}; "
        'CUR=$(readlink -f "$BASE/current" 2>/dev/null || true); '
        'echo "current=$CUR"; '
        'if [ -n "$CUR" ]; then '
        '  LINK=$(readlink "$CUR/.venv" 2>/dev/null || true); '
        '  PY=$(readlink -f "$CUR/.venv/bin/python" 2>/dev/null || true); '
        '  echo "venv_link=$LINK"; '
        '  echo "python=$PY"; '
        '  test -x "$CUR/.venv/bin/python"; '
        'else '
        '  echo "venv_link="; echo "python="; exit 1; '
        'fi'
    )
    return f"sh -lc {_quote(script)}"


def _evaluate_venv_guard(raw: dict[str, Any], *, self_evolving_base: str) -> dict[str, Any]:
    values = _parse_key_value_lines(str(raw.get("stdout") or ""))
    current = values.get("current", "")
    venv_link = values.get("venv_link", "")
    python = values.get("python", "")
    reasons: list[str] = []
    current_venv = f"{self_evolving_base.rstrip('/')}/current/.venv"
    if not raw.get("ok"):
        reasons.append("venv_python_missing")
    if venv_link == current_venv or venv_link.endswith("/current/.venv"):
        reasons.append("venv_symlink_loop_risk")
    if current and venv_link and venv_link.startswith(f"{current.rstrip('/')}/"):
        reasons.append("venv_symlink_loop_risk")
    if not python:
        reasons.append("venv_python_missing")
    result = dict(raw)
    result.update({
        "ok": not reasons,
        "current": current or None,
        "venv_link": venv_link or None,
        "python": python or None,
        "reasons": sorted(set(reasons)),
    })
    return result


def _self_evolving_venv_guard(host: str, *, self_evolving_base: str, sudo_available: bool, key: str | None = None) -> dict[str, Any]:
    command = _self_evolving_venv_guard_command(self_evolving_base)
    raw = _ssh(host, _sudo(command) if sudo_available else command, key=key)
    return _evaluate_venv_guard(_with_via(raw, "sudo" if sudo_available else "direct"), self_evolving_base=self_evolving_base)


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


def build_preflight(*, host: str, state_root: str = DEFAULT_STATE_ROOT, key: str | None = None, nanobot_path: str = DEFAULT_NANOBOT, opencode_home: str = DEFAULT_OPENCODE_HOME, self_evolving_base: str = DEFAULT_SELF_EVOLVING_BASE) -> dict[str, Any]:
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
    self_evolving_release_venv = _self_evolving_venv_guard(host, self_evolving_base=self_evolving_base, sudo_available=sudo_available, key=key)
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
        "self_evolving_release_venv": self_evolving_release_venv,
    })
    if not sudo_available:
        blockers.append("sudo_noninteractive")
    if not opencode_home_check["ok"]:
        blockers.append("execute_opencode_nanobot")
    if not outbox["ok"]:
        blockers.append("read_authority_outbox")
    if not goals["ok"]:
        blockers.append("read_goal_registry")
    if not self_evolving_release_venv["ok"]:
        blockers.extend(self_evolving_release_venv.get("reasons") or ["self_evolving_release_venv"])

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
    parser.add_argument("--self-evolving-base", default=DEFAULT_SELF_EVOLVING_BASE)
    parser.add_argument("--json", action="store_true", help="Accepted for explicitness; output is always JSON.")
    args = parser.parse_args(argv)
    payload = build_preflight(host=args.host, state_root=args.state_root, key=args.ssh_key, nanobot_path=args.nanobot_path, opencode_home=args.opencode_home, self_evolving_base=args.self_evolving_base)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
