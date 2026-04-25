"""Deterministic subagent request materialization/terminalization helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def materialize_subagent_requests(*, state_root: Path, now: datetime | None = None, limit: int | None = None) -> dict[str, Any]:
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
    skipped = 0
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
            result = {
                "schema_version": "subagent-result-v1",
                "status": "blocked",
                "result_status": "blocked",
                "terminal_reason": "local_executor_unavailable",
                "materialized_from": "queued_request_terminalizer",
                "created_at": now.isoformat().replace("+00:00", "Z"),
                "request_path": str(request_path),
                "request_status": status,
                "request_id": request.get("request_id") or request.get("id"),
                "cycle_id": request.get("cycle_id") or request.get("cycleId"),
                "task_id": request.get("task_id") or request.get("taskId"),
                "task_title": request.get("task_title") or request.get("title") or request.get("summary"),
                "profile": request.get("profile"),
                "source_artifact": request.get("source_artifact"),
                "feedback_decision": request.get("feedback_decision"),
                "summary": "Subagent request terminalized as blocked because no local executor is available",
            }
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            results.append({"path": str(result_path), **result})
            terminalized += 1
            blocked += 1
    return {
        "schema_version": "subagent-materializer-summary-v1",
        "state_root": str(state_root),
        "terminalized_count": terminalized,
        "blocked_result_count": blocked,
        "skipped_count": skipped,
        "existing_result_count": len(existing_payloads),
        "results": results,
    }
