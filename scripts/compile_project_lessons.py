#!/usr/bin/env python3
"""Automated compiler to extract structured lessons and errors from coordinator history files."""
import sys
import os
import json
import yaml
from pathlib import Path

def load_yaml(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def save_yaml(path: Path, data: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)

def main():
    workspace = Path(os.environ.get("TARGET_WORKSPACE", ".")).resolve()
    state_root = Path(os.environ.get("STATE_ROOT", "/var/lib/eeepc-agent/self-evolving-agent/state")).resolve()
    history_dir = state_root / "goals" / "history"
    
    if not history_dir.exists():
        print(f"History directory not found: {history_dir}")
        sys.exit(0)
        
    local_lessons_dir = workspace / "lessons"
    errors_yaml_path = local_lessons_dir / "errors.yaml"
    lessons_yaml_path = local_lessons_dir / "lessons.yaml"
    
    existing_errors = load_yaml(errors_yaml_path)
    existing_lessons = load_yaml(lessons_yaml_path)
    
    known_error_cycles = {str(err.get("cycle_id")) for err in existing_errors if err.get("cycle_id")}
    known_lesson_cycles = {str(les.get("cycle_id")) for les in existing_lessons if les.get("cycle_id")}
    
    new_errors_count = 0
    new_lessons_count = 0
    
    # Read history files
    for history_path in history_dir.glob("cycle-*.json"):
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
            
        cycle_id = data.get("cycle_id", history_path.stem.replace("cycle-", ""))
        status = data.get("result_status") or data.get("status")
        reward_val = data.get("reward_signal", {}).get("value", 0.0)
        
        # 1. Process failures (BLOCK/ERROR)
        if status in ("BLOCK", "ERROR") and cycle_id not in known_error_cycles:
            failure_class = data.get("feedback_decision", {}).get("repeat_block_failure_class", "unknown")
            reason = data.get("summary") or data.get("feedback_decision", {}).get("reason", "No reason provided")
            next_hint = data.get("next_hint") or data.get("blocked_next_step") or ""
            
            error_entry = {
                "id": f"ERR-{cycle_id}",
                "cycle_id": cycle_id,
                "date": data.get("recorded_at_utc", "")[:10] or "2026-06-14",
                "category": failure_class,
                "title": f"Cycle {cycle_id} blocked: {failure_class}",
                "description": f"The self-evolving run failed at step '{data.get('current_task_id', 'unknown')}'. Reason: {reason}",
                "root_cause": f"System encountered a block classified as '{failure_class}'. Details: {reason}",
                "impact": "Self-evolving loop halted or required manual intervention.",
                "fix_applied": f"Required manual step: {next_hint}" if next_hint else "Investigated logs.",
                "prevention": "Inspect coordinator status rules to avoid repeating this state pattern."
            }
            # Insert new error at the beginning of the list
            existing_errors.insert(0, error_entry)
            known_error_cycles.add(cycle_id)
            new_errors_count += 1
            
            # Write Markdown detail card
            md_path = local_lessons_dir / "errors" / f"ERR-{cycle_id}.md"
            if not md_path.exists():
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_content = f"""# ERR-{cycle_id}: Cycle {cycle_id} blocked ({failure_class})

## Symptom
The cycle failed on task `{data.get('current_task_id')}`.
Summary: {reason}

## Root Cause
Failure classification: `{failure_class}`.
Decision reason: {data.get('feedback_decision', {}).get('reason')}

## Fix Applied
{f"Next hint indicated: {next_hint}" if next_hint else "Analyzed cycle logs and coordinator status."}

## Prevention
Monitor the `{failure_class}` parameters on the host.
"""
                md_path.write_text(md_content, encoding="utf-8")
                
        # 2. Process successes (PASS with high reward)
        elif status == "PASS" and reward_val >= 1.0 and cycle_id not in known_lesson_cycles:
            task_title = data.get("current_task", "Unnamed Task")
            summary = data.get("summary", "No summary provided")
            
            lesson_entry = {
                "id": f"LESS-{cycle_id}",
                "cycle_id": cycle_id,
                "date": data.get("recorded_at_utc", "")[:10] or "2026-06-14",
                "category": "successful-improvement",
                "title": f"Successful optimization: {task_title}",
                "description": summary,
                "impact": f"Yielded positive reward signal: {reward_val}",
                "approach": f"Implemented task '{data.get('current_task_id')}' successfully.",
                "reusable_insight": "Consolidate this optimization pattern in subsequent cycles."
            }
            # Insert new lesson at the beginning of the list
            existing_lessons.insert(0, lesson_entry)
            known_lesson_cycles.add(cycle_id)
            new_lessons_count += 1
            
            # Write Markdown detail card
            md_path = local_lessons_dir / "lessons" / f"LESS-{cycle_id}.md"
            if not md_path.exists():
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_content = f"""# LESS-{cycle_id}: Successful optimization ({task_title})

## Improvement Implemented
{summary}

## Impact
Reward value obtained: `{reward_val}`.

## Reusable Insights
The optimization pattern implemented in task `{data.get('current_task_id')}` can be reused when addressing similar bottlenecks.
"""
                md_path.write_text(md_content, encoding="utf-8")

    if new_errors_count > 0:
        save_yaml(errors_yaml_path, existing_errors)
        print(f"Compiled {new_errors_count} new errors into {errors_yaml_path}")
    if new_lessons_count > 0:
        save_yaml(lessons_yaml_path, existing_lessons)
        print(f"Compiled {new_lessons_count} new lessons into {lessons_yaml_path}")
        
    if new_errors_count == 0 and new_lessons_count == 0:
        print("No new lessons or errors to compile.")

if __name__ == "__main__":
    main()
