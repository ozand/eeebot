#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs" / "autonomy_control_registry.json"
STAGNATION_SCRIPT = REPO_ROOT / "scripts" / "analyze_stagnation.py"


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _hours(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_stagnation_analysis() -> dict:
    try:
        proc = subprocess.run(
            [sys.executable, str(STAGNATION_SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(proc.stdout)
    except Exception as exc:  # pragma: no cover - best-effort control plane helper
        return {
            "diagnosis": "unknown",
            "error": str(exc),
        }


def main() -> None:
    now = datetime.now(timezone.utc)
    registry = _load_json(REGISTRY_PATH)
    stagnation = _run_stagnation_analysis()

    projects = []
    ownership_gaps = []
    due_reviews = []

    for project in registry.get("projects", []):
        last_review = _parse_iso8601(project.get("last_review_utc"))
        review_interval = _hours(project.get("review_interval_hours"), _hours(registry.get("global_thresholds", {}).get("ownership_review_interval_hours"), 24))
        next_review = (last_review + timedelta(hours=review_interval)) if last_review else None
        overdue = bool(next_review and now >= next_review)
        missing_fields = [field for field in ("owner", "executor_role", "approver_role") if not project.get(field)]

        status = "healthy"
        if missing_fields:
            status = "ownership_gap"
        elif overdue:
            status = "review_overdue"

        project_summary = {
            "name": project.get("name"),
            "status": project.get("status"),
            "owner": project.get("owner"),
            "executor_role": project.get("executor_role"),
            "approver_role": project.get("approver_role"),
            "review_interval_hours": review_interval,
            "last_review_utc": project.get("last_review_utc"),
            "next_review_utc": next_review.isoformat().replace("+00:00", "Z") if next_review else None,
            "overdue": overdue,
            "control_status": status,
            "next_bounded_action": project.get("next_bounded_action"),
        }

        projects.append(project_summary)
        if missing_fields:
            ownership_gaps.append({"project": project.get("name"), "missing_fields": missing_fields})
        if overdue:
            due_reviews.append({"project": project.get("name"), "next_review_utc": project_summary["next_review_utc"]})

    nanobot = next((p for p in projects if p.get("name") == "Nanobot eeepc control loop"), None)
    nanobot_state = {
        "diagnosis": stagnation.get("diagnosis"),
        "latest": stagnation.get("latest"),
        "stagnation_flags": stagnation.get("stagnation_flags"),
        "failure_class_counts": stagnation.get("failure_class_counts"),
    }

    if nanobot and stagnation.get("diagnosis") == "stagnating_on_quality_blocker":
        next_action = (
            "Nanobot is stagnating on a quality blocker; keep the next step bounded to one file-level or config-level fix, "
            "or escalate with the exact blocker if the fix is not safe."
        )
        overall_state = "action_required"
    elif ownership_gaps or due_reviews:
        overdue_name = (due_reviews[0]["project"] if due_reviews else ownership_gaps[0]["project"])
        next_action = f"Project ownership needs attention; review {overdue_name} and refresh the owner/executor/next-action record."
        overall_state = "action_required"
    else:
        next_action = "Ownership coverage is explicit; continue the next scheduled review without waiting for a new nudge."
        overall_state = "healthy"

    result = {
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "registry_path": str(REGISTRY_PATH),
        "registry_version": registry.get("version"),
        "overall_state": overall_state,
        "projects_considered": len(projects),
        "projects": projects,
        "ownership_gaps": ownership_gaps,
        "due_reviews": due_reviews,
        "nanobot": nanobot_state,
        "next_action": next_action,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
