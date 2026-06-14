#!/usr/bin/env python3
"""Automated compiler to extract structured, deduplicated lessons and errors from coordinator history files."""
import sys
import os
import json
import yaml
import shutil
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

def clean_auto_generated_markdowns(lessons_dir: Path):
    """Clean previously auto-generated markdown cards that used raw cycle IDs."""
    errors_dir = lessons_dir / "errors"
    lessons_subdir = lessons_dir / "lessons"
    
    # Remove files that match the old pattern ERR-cycle-*.md or LESS-cycle-*.md
    if errors_dir.exists():
        for f in errors_dir.glob("ERR-cycle-*.md"):
            try:
                f.unlink()
            except OSError:
                pass
    if lessons_subdir.exists():
        for f in lessons_subdir.glob("LESS-cycle-*.md"):
            try:
                f.unlink()
            except OSError:
                pass

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
    
    # Keep hand-crafted or system errors (IDs starting with ERR-2026 or ERR-SYS)
    existing_items = load_yaml(errors_yaml_path)
    handwritten_errors = [item for item in existing_items if not str(item.get("id", "")).startswith("ERR-cycle-") and not str(item.get("id", "")).startswith("ERR-AUTO-")]
    
    # Clean up old markdown cards
    clean_auto_generated_markdowns(local_lessons_dir)
    
    # Dictionaries to accumulate deduplicated entries
    deduped_errors = {}
    deduped_lessons = {}
    
    # Read history files
    for history_path in sorted(history_dir.glob("cycle-*.json"), key=lambda p: p.stat().st_mtime):
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
            
        cycle_id = data.get("cycle_id", history_path.stem.replace("cycle-", ""))
        status = data.get("result_status") or data.get("status")
        
        # Extract reward details
        reward_sig = data.get("reward_signal") or {}
        reward_val = reward_sig.get("value", 0.0) if isinstance(reward_sig, dict) else 0.0
        real_work = reward_sig.get("real_work_detected", False) if isinstance(reward_sig, dict) else False
        artifact_paths = data.get("artifact_paths", []) or []
        
        # Filter out temp/reports/state artifacts from files_changed
        filtered_artifacts = []
        for path_str in artifact_paths:
            p_str = str(path_str)
            if any(term in p_str for term in ["state/reports/", "state/goals/", "state/subagents/", "state/experiments/", "state/approvals/"]):
                continue
            filtered_artifacts.append(path_str)
            
        recorded_date = data.get("recorded_at_utc", "")[:10] or "2026-06-14"
        
        # 1. Process failures (BLOCK/ERROR)
        if status in ("BLOCK", "ERROR"):
            failure_class = data.get("feedback_decision", {}).get("repeat_block_failure_class") or "unknown"
            # Skip expected/manual blockages like expired approval gates
            if "approval" in failure_class or "expired" in failure_class:
                continue
                
            reason = data.get("summary") or data.get("feedback_decision", {}).get("reason", "No reason provided")
            next_hint = data.get("next_hint") or data.get("blocked_next_step") or ""
            current_task_id = data.get("current_task_id", "unknown")
            
            # Key errors by their failure class + active task ID to keep context precise
            err_key = f"{failure_class}:{current_task_id}"
            
            if err_key not in deduped_errors:
                deduped_errors[err_key] = {
                    "id": f"ERR-AUTO-{failure_class.replace(':', '-')}-{current_task_id}",
                    "category": failure_class,
                    "title": f"Automated block on task '{current_task_id}' ({failure_class})",
                    "description": f"The self-evolving run failed at step '{current_task_id}'. Reason: {reason}",
                    "root_cause": f"System encountered a block classified as '{failure_class}'. Details: {reason}",
                    "impact": "Self-evolving loop halted or required manual intervention.",
                    "fix_applied": f"Required manual step: {next_hint}" if next_hint else "Investigated logs.",
                    "prevention": "Inspect coordinator status rules to avoid repeating this state pattern.",
                    "occurrences": 1,
                    "first_seen": recorded_date,
                    "last_seen": recorded_date,
                    "sample_cycle_id": cycle_id
                }
            else:
                deduped_errors[err_key]["occurrences"] += 1
                deduped_errors[err_key]["last_seen"] = recorded_date
                
        # 2. Process successes (PASS with real work done on code files)
        elif status == "PASS" and (real_work or len(filtered_artifacts) > 0):
            task_title = data.get("current_task", "Unnamed Task")
            summary = data.get("summary", "No summary provided")
            current_task_id = data.get("current_task_id", "unknown")
            
            # Key lessons by their task_id to group optimizations of the same code areas
            lesson_key = current_task_id
            
            if lesson_key not in deduped_lessons:
                deduped_lessons[lesson_key] = {
                    "id": f"LESS-AUTO-{current_task_id}",
                    "category": "successful-improvement",
                    "title": f"Optimization pattern: {task_title}",
                    "description": summary,
                    "impact": f"Yielded positive reward signal: {reward_val}",
                    "approach": f"Implemented task '{current_task_id}' successfully.",
                    "reusable_insight": "Consolidate this optimization pattern in subsequent cycles.",
                    "occurrences": 1,
                    "first_seen": recorded_date,
                    "last_seen": recorded_date,
                    "sample_cycle_id": cycle_id,
                    "files_changed": filtered_artifacts
                }
            else:
                deduped_lessons[lesson_key]["occurrences"] += 1
                deduped_lessons[lesson_key]["last_seen"] = recorded_date
                # Accumulate files changed if they are unique
                current_files = set(deduped_lessons[lesson_key].get("files_changed", []))
                current_files.update(filtered_artifacts)
                deduped_lessons[lesson_key]["files_changed"] = sorted(list(current_files))

    # Convert dicts to lists and sort by last_seen desc
    final_errors = list(deduped_errors.values())
    final_errors.sort(key=lambda x: x["last_seen"], reverse=True)
    
    # Prepend handwritten errors to keep manual analysis prioritized at the top of the file
    all_errors = handwritten_errors + final_errors
    
    final_lessons = list(deduped_lessons.values())
    final_lessons.sort(key=lambda x: x["last_seen"], reverse=True)
    
    # Save the cleaned databases
    save_yaml(errors_yaml_path, all_errors)
    save_yaml(lessons_yaml_path, final_lessons)
    
    # Write detail Markdown files for the newly generated unique categories
    errors_dir = local_lessons_dir / "errors"
    errors_dir.mkdir(parents=True, exist_ok=True)
    for err in final_errors:
        md_path = errors_dir / f"{err['id']}.md"
        md_content = f"""# {err['id']}: {err['title']}

## Symptom
The self-evolving loop was blocked on task `{err['category']}` (Total occurrences: {err['occurrences']}).
Sample cycle ID: `{err['sample_cycle_id']}`

## Root Cause
{err['root_cause']}

## Fix Applied
{err['fix_applied']}

## Prevention
{err['prevention']}
"""
        md_path.write_text(md_content, encoding="utf-8")
        
    lessons_subdir = local_lessons_dir / "lessons"
    lessons_subdir.mkdir(parents=True, exist_ok=True)
    for les in final_lessons:
        md_path = lessons_subdir / f"{les['id']}.md"
        md_content = f"""# {les['id']}: {les['title']}

## Successful Optimization
{les['description']} (Total implementations: {les['occurrences']}).
Sample cycle ID: `{les['sample_cycle_id']}`

## Impact
{les['impact']}

## Files Modified
{json.dumps(les['files_changed'], indent=2) if les['files_changed'] else "None recorded"}

## Reusable Insights
{les['reusable_insight']}
"""
        md_path.write_text(md_content, encoding="utf-8")
        
    print(f"Cleaned and compiled database:")
    print(f"  - Handwritten errors kept: {len(handwritten_errors)}")
    print(f"  - Unique automated error classes compiled: {len(final_errors)}")
    print(f"  - Unique automated lesson classes compiled: {len(final_lessons)}")

if __name__ == "__main__":
    main()
