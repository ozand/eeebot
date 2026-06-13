#!/usr/bin/env python3
"""Verify a materialized improvement artifact.

This is a lightweight, dependency-free smoke checker for the self-evolving
runtime. It validates the artifact shape, confirms the PASS result, and checks
that the follow-up semantics are explicit enough for review.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_ARTIFACT = Path(
    "/var/lib/eeepc-agent/self-evolving-agent/state/improvements/materialized-cycle-9f477a61f5bb.json"
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_artifact(data: dict[str, Any], source_path: Path) -> list[str]:
    errors: list[str] = []

    if data.get("schema_version") != "materialized-improvement-v1":
        errors.append("schema_version must be materialized-improvement-v1")

    if not str(data.get("cycle_id", "")).strip():
        errors.append("cycle_id is missing")

    if not str(data.get("goal_id", "")).strip():
        errors.append("goal_id is missing")

    if not str(data.get("task_id", "")).strip():
        errors.append("task_id is missing")

    reward_signal = data.get("reward_signal", {})
    if not isinstance(reward_signal, dict):
        errors.append("reward_signal must be a mapping")
    elif reward_signal.get("result_status") != "PASS":
        errors.append("reward_signal.result_status must be PASS")

    feedback = data.get("feedback_decision", {})
    if not isinstance(feedback, dict):
        errors.append("feedback_decision must be a mapping")
    else:
        mode = str(feedback.get("mode", "")).strip()
        if not mode:
            errors.append("feedback_decision.mode is missing")
        selected_task_id = str(feedback.get("selected_task_id", "")).strip()
        if not selected_task_id:
            errors.append("feedback_decision.selected_task_id is missing")

    concrete_statement = str(data.get("concrete_improvement_statement", "")).strip()
    if not concrete_statement:
        errors.append("concrete_improvement_statement is missing")

    next_candidate = data.get("next_bounded_candidate", {})
    if not isinstance(next_candidate, dict):
        errors.append("next_bounded_candidate must be a mapping")
    else:
        task_id = str(next_candidate.get("task_id", "")).strip()
        title = str(next_candidate.get("title", "")).strip()
        acceptance = str(next_candidate.get("acceptance", "")).strip()
        if not task_id or not title or not acceptance:
            errors.append("next_bounded_candidate must include task_id, title, and acceptance")

    if not source_path.exists():
        errors.append(f"artifact file does not exist: {source_path}")

    return errors


def summarize(data: dict[str, Any], source_path: Path) -> str:
    feedback = data.get("feedback_decision", {})
    next_candidate = data.get("next_bounded_candidate", {})
    return (
        f"PASS: {source_path.name} cycle={data.get('cycle_id', 'unknown')} "
        f"task={feedback.get('selected_task_id', 'unknown')} "
        f"mode={feedback.get('mode', 'unknown')} "
        f"next={next_candidate.get('task_id', 'unknown')}"
    )


def main(argv: list[str]) -> int:
    source_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_ARTIFACT
    try:
        data = load_json(source_path)
    except FileNotFoundError:
        print(f"ERROR: artifact not found: {source_path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON in {source_path}: {exc}", file=sys.stderr)
        return 2

    errors = validate_artifact(data, source_path)
    if errors:
        print(f"FAIL: {source_path}", file=sys.stderr)
        for error in errors:
            print(f" - {error}", file=sys.stderr)
        return 1

    print(summarize(data, source_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
