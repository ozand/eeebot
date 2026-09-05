#!/usr/bin/env python3
"""Retired materialized-artifact verifier (#1312).

The writer is gone. Invocation reports retirement, never PASS or a missing-file
failure that would generate defect demand. Pure historical shape helpers remain
for compatibility; the entrypoint no longer reads any artifact.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

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
        selected_task_class = str(feedback.get("selected_task_class", "")).strip()
        if not selected_task_class:
            errors.append("feedback_decision.selected_task_class is missing")
        selected_task_label = str(feedback.get("selected_task_label", "")).strip()
        if not selected_task_label:
            errors.append("feedback_decision.selected_task_label is missing")

    summary = str(data.get("summary", "")).strip()
    if not summary:
        errors.append("summary is missing")
    elif "PASS" not in summary:
        errors.append("summary must record PASS status")

    goal_artifact_signature = data.get("goal_artifact_signature")
    if goal_artifact_signature is not None:
        if not isinstance(goal_artifact_signature, list) or not goal_artifact_signature:
            errors.append("goal_artifact_signature must be a non-empty list when present")
        else:
            signature_text = " ".join(str(item).strip() for item in goal_artifact_signature if str(item).strip())
            if str(data.get("goal_id", "")).strip() not in signature_text:
                errors.append("goal_artifact_signature must reference goal_id")
            selected_task_id = str(data.get("feedback_decision", {}).get("selected_task_id", "")).strip()
            if selected_task_id and selected_task_id not in signature_text:
                errors.append("goal_artifact_signature must reference selected_task_id")

    concrete_statement = str(data.get("concrete_improvement_statement", "")).strip()
    if not concrete_statement:
        errors.append("concrete_improvement_statement is missing")

    acceptance_checks = data.get("acceptance_checks", [])
    if not isinstance(acceptance_checks, list) or not acceptance_checks:
        errors.append("acceptance_checks must be a non-empty list")
    else:
        required_acceptance_checks = {
            "distinct materialized improvement artifact exists",
            "feedback decision references completion or follow-up semantics",
            "next bounded candidate is explicit and reviewable",
        }
        normalized_acceptance_checks = {str(item).strip() for item in acceptance_checks if str(item).strip()}
        missing_acceptance_checks = sorted(required_acceptance_checks - normalized_acceptance_checks)
        if missing_acceptance_checks:
            errors.append(
                "acceptance_checks missing required entries: " + ", ".join(missing_acceptance_checks)
            )

    next_candidate = data.get("next_bounded_candidate", {})
    if not isinstance(next_candidate, dict):
        errors.append("next_bounded_candidate must be a mapping")
    else:
        task_id = str(next_candidate.get("task_id", "")).strip()
        title = str(next_candidate.get("title", "")).strip()
        acceptance = str(next_candidate.get("acceptance", "")).strip()
        task_class = str(next_candidate.get("task_class", "")).strip()
        if not task_id or not title or not acceptance or not task_class:
            errors.append("next_bounded_candidate must include task_id, title, acceptance, and task_class")

    derived_candidate = data.get("derived_candidate", {})
    if not isinstance(derived_candidate, dict):
        errors.append("derived_candidate must be a mapping")
    else:
        derived_task_id = str(derived_candidate.get("task_id", "")).strip()
        derived_title = str(derived_candidate.get("title", "")).strip()
        if not derived_task_id or not derived_title:
            errors.append("derived_candidate must include task_id and title")
        elif derived_task_id != str(next_candidate.get("task_id", "")).strip():
            errors.append("derived_candidate.task_id must match next_bounded_candidate.task_id")

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
    print("retired (#1312): materialized improvement writer removed; verification unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
