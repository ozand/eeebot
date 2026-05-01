"""Deterministic subagent request materialization/terminalization helpers."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PI_DEV_PROVIDER = "hermes_pi_qwen"
PI_DEV_MODEL = "gpt-5.3-codex"
PI_DEV_PUBLIC_BASE_URL = "https://litellm.ayga.tech:9443/v1"
PI_DEV_BIN = os.path.expanduser("~/.hermes/node/bin/pi")
PI_DEV_COMMAND_ARGV = [
    PI_DEV_BIN if Path(PI_DEV_BIN).exists() else "pi",
    "--mode",
    "json",
    "-p",
    "--no-session",
    "--no-tools",
    "--provider",
    PI_DEV_PROVIDER,
    "--model",
    PI_DEV_MODEL,
]
PI_DEV_COMMAND = " ".join(shlex.quote(part) for part in PI_DEV_COMMAND_ARGV)


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _stable_request_key(request_path: Path, payload: dict[str, Any]) -> str:
    return str(payload.get("request_id") or payload.get("cycle_id") or payload.get("cycleId") or request_path.stem.replace("request-", ""))


def _result_path_for(result_dir: Path, request_path: Path, payload: dict[str, Any]) -> Path:
    key = _stable_request_key(request_path, payload)
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in key).strip("-") or request_path.stem
    return result_dir / f"result-{safe}.json"


def _redact_secret_text(value: str | None, *, limit: int = 4000) -> str:
    if not value:
        return ""
    text = str(value)[:limit]
    text = text.replace("sk-secret", "[REDACTED]")
    import re
    text = re.sub(r"sk-[A-Za-z0-9._-]+", "sk-[REDACTED]", text)
    return text


def _executor_metadata() -> dict[str, Any]:
    return {
        "provider": PI_DEV_PROVIDER,
        "model": PI_DEV_MODEL,
        "base_url": PI_DEV_PUBLIC_BASE_URL,
        "command_configured": True,
        "auth": "configured_out_of_band_redacted",
    }


def _executor_unavailable_blocker(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "subagent-executor-blocker-v1",
        "reason": "local_executor_unavailable",
        "recommended_next_action": "configure_subagent_executor",
        "executor_selection_source": "unconfigured",
        "required_env": ["NANOBOT_SUBAGENT_EXECUTOR_COMMAND", "NANOBOT_SUBAGENT_EXECUTOR=pi_dev"],
        "accepted_executor_profiles": ["research_only", "review_only", "bounded_review"],
        "request_profile": request.get("profile"),
        "config_hint": "Set NANOBOT_SUBAGENT_EXECUTOR=pi_dev for the built-in bounded Pi Dev executor, or set NANOBOT_SUBAGENT_EXECUTOR_COMMAND to an argv-compatible command that reads the task prompt from stdin.",
    }


def _executor_misconfiguration_blocker(request: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "schema_version": "subagent-executor-misconfiguration-v1",
        "reason": reason,
        "recommended_next_action": "quote_systemd_executor_command_or_set_argv_command",
        "executor_selection_source": "configured_but_invalid",
        "required_env": ["NANOBOT_SUBAGENT_EXECUTOR_COMMAND"],
        "safe_example": "NANOBOT_SUBAGENT_EXECUTOR_COMMAND=/path/to/python -m nanobot.runtime.bounded_subagent_executor",
        "systemd_hint": "When configuring via systemd Environment=, quote the full NAME=value assignment if the value contains spaces.",
        "request_profile": request.get("profile"),
    }


def _request_prompt(request: dict[str, Any]) -> str:
    title = request.get("task_title") or request.get("title") or request.get("task_id") or "subagent task"
    source = request.get("source_artifact") or "source artifact unavailable"
    return (
        f"Execute one bounded research-only subagent review for: {title}.\n"
        f"Task id: {request.get('task_id') or request.get('taskId')}.\n"
        f"Cycle id: {request.get('cycle_id') or request.get('cycleId')}.\n"
        f"Source artifact: {source}.\n"
        "Return concise findings and do not mutate files."
    )


def _executor_argv(command: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    return shlex.split(str(command))


def _bare_python_executor_reason(argv: list[str]) -> str | None:
    if len(argv) != 1:
        return None
    executable = Path(argv[0]).name.lower()
    if executable in {"python", "python3"} or executable.startswith("python3.") or executable.startswith("python2."):
        return "bare_python_executor_command"
    return None


def _run_local_executor(command: str | list[str] | tuple[str, ...], request: dict[str, Any], *, timeout_seconds: int) -> tuple[bool, dict[str, Any]]:
    argv = _executor_argv(command)
    misconfiguration_reason = _bare_python_executor_reason(argv)
    if misconfiguration_reason:
        return False, {
            "returncode": None,
            "stdout": "",
            "stderr": "executor command resolves to a bare Python interpreter; expected argv that reads stdin intentionally, for example: python -m nanobot.runtime.bounded_subagent_executor",
            "failure_reason": "local_executor_misconfigured",
            "blocker": _executor_misconfiguration_blocker(request, misconfiguration_reason),
        }
    try:
        completed = subprocess.run(
            argv,
            input=_request_prompt(request),
            text=True,
            shell=False,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return False, {
            "returncode": None,
            "stdout": _redact_secret_text(exc.stdout if isinstance(exc.stdout, str) else ""),
            "stderr": "executor timed out",
            "failure_reason": "local_executor_timeout",
        }
    except Exception as exc:
        return False, {
            "returncode": None,
            "stdout": "",
            "stderr": _redact_secret_text(str(exc)),
            "failure_reason": "local_executor_exception",
        }
    output = _redact_secret_text(completed.stdout)
    error = _redact_secret_text(completed.stderr)
    payload = {
        "returncode": completed.returncode,
        "stdout": output,
        "stderr": error,
        "failure_reason": None if completed.returncode == 0 else "local_executor_failed",
    }
    return completed.returncode == 0, payload


def materialize_subagent_requests(*, state_root: Path, now: datetime | None = None, limit: int | None = None, executor_command: str | list[str] | tuple[str, ...] | None = None, executor_timeout_seconds: int = 120) -> dict[str, Any]:
    """Terminalize queued subagent requests into durable result artifacts.

    The local product runtime does not assume a live external subagent executor is
    available. It therefore converts old/queued request artifacts into explicit
    blocked result records with stable request/result correlation. This removes
    silent stale queues from the truth surface while keeping execution authority
    honest.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    subagents_dir = state_root / "subagents"
    request_dir = subagents_dir / "requests"
    result_dir = subagents_dir / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    queued_statuses = {"queued", "pending"}
    existing_payloads = []
    existing_by_request: set[str] = set()
    for path in result_dir.glob("*.json") if result_dir.exists() else []:
        payload = _safe_read_json(path)
        if not payload:
            continue
        existing_payloads.append({"path": str(path), **payload})
        request_path = payload.get("request_path")
        if request_path:
            existing_by_request.add(str(request_path))
    results: list[dict[str, Any]] = []
    terminalized = 0
    blocked = 0
    executed = 0
    skipped = 0
    configured_executor: str | list[str] | tuple[str, ...] | None = executor_command or os.environ.get("NANOBOT_SUBAGENT_EXECUTOR_COMMAND")
    if not configured_executor and os.environ.get("NANOBOT_SUBAGENT_EXECUTOR") == "pi_dev":
        configured_executor = PI_DEV_COMMAND_ARGV
    if request_dir.exists():
        request_paths = sorted([p for p in request_dir.glob("*.json") if p.is_file()], key=lambda p: p.stat().st_mtime)
        for request_path in request_paths:
            if limit is not None and terminalized >= limit:
                break
            request = _safe_read_json(request_path)
            if not request:
                skipped += 1
                continue
            status = str(request.get("request_status") or request.get("status") or "queued").lower()
            if status not in queued_statuses:
                skipped += 1
                continue
            if str(request_path) in existing_by_request:
                skipped += 1
                continue
            result_path = _result_path_for(result_dir, request_path, request)
            if result_path.exists():
                existing_by_request.add(str(request_path))
                skipped += 1
                continue
            executor_result: dict[str, Any] | None = None
            executor_ok = False
            if configured_executor and str(request.get("profile") or "").lower() in {"research_only", "review_only", "bounded_review"}:
                executor_ok, executor_result = _run_local_executor(
                    configured_executor,
                    request,
                    timeout_seconds=executor_timeout_seconds,
                )
            terminal_reason = None if executor_ok else ((executor_result or {}).get("failure_reason") or "local_executor_unavailable")
            status_value = "completed" if executor_ok else "blocked"
            blocker = (executor_result or {}).get("blocker") if executor_result else None
            if not blocker and terminal_reason == "local_executor_unavailable" and not configured_executor:
                blocker = _executor_unavailable_blocker(request)
            summary = (executor_result or {}).get("stdout") if executor_ok else "Subagent request terminalized as blocked because no local executor is available"
            if blocker:
                required = blocker.get("required_env") if isinstance(blocker.get("required_env"), list) else []
                required_text = " or ".join(str(item) for item in required) or "a valid executor command"
                summary = f"Subagent request terminalized as blocked because the local executor is unavailable or misconfigured. Set {required_text}."
            if executor_result and not executor_ok:
                summary = "Subagent request executor failed; request was materialized as blocked"
            result = {
                "schema_version": "subagent-result-v1",
                "status": status_value,
                "result_status": status_value,
                "terminal_reason": terminal_reason,
                "materialized_from": "local_pi_dev_executor" if executor_result else "queued_request_terminalizer",
                "created_at": now.isoformat().replace("+00:00", "Z"),
                "request_path": str(request_path),
                "request_status": status,
                "request_id": request.get("request_id") or request.get("id"),
                "semantic_task_id": request.get("semantic_task_id") or request.get("task_id") or request.get("taskId"),
                "verification_task_id": request.get("verification_task_id") or request.get("request_id") or request.get("id"),
                "verification_role": request.get("verification_role"),
                "cycle_id": request.get("cycle_id") or request.get("cycleId"),
                "task_id": request.get("task_id") or request.get("taskId"),
                "task_title": request.get("task_title") or request.get("title") or request.get("summary"),
                "profile": request.get("profile"),
                "source_artifact": request.get("source_artifact"),
                "feedback_decision": request.get("feedback_decision"),
                "summary": summary,
                "recommended_next_action": blocker.get("recommended_next_action") if blocker else None,
                "blocker": blocker,
                "executor": _executor_metadata() if (executor_result or configured_executor) else None,
                "executor_result": executor_result,
            }
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            results.append({"path": str(result_path), **result})
            terminalized += 1
            if executor_ok:
                executed += 1
            else:
                blocked += 1
    return {
        "schema_version": "subagent-materializer-summary-v1",
        "state_root": str(state_root),
        "terminalized_count": terminalized,
        "executed_count": executed,
        "blocked_result_count": blocked,
        "skipped_count": skipped,
        "existing_result_count": len(existing_payloads),
        "results": results,
    }
