import hashlib
import os
import datetime
import json
import shutil

EXECUTION_QUEUE_FILE = "ops/dashboard/control/execution_queue.json"
ARCHIVE_DIR = "state/subagents/archive"
CURRENT_HEALTH_FILE = "state/current_health.json"

def cleanup_execution_queue():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    
    now = datetime.datetime.now(datetime.timezone.utc) # Use timezone-aware datetime
    stale_count = 0
    updated_tasks = []
    archived_task_ids = []

    try:
        with open(EXECUTION_QUEUE_FILE, 'r') as f:
            queue_data = json.load(f)
            tasks = queue_data.get('tasks', [])
    except FileNotFoundError:
        print(f"Error: {EXECUTION_QUEUE_FILE} not found.")
        return 0
    except json.JSONDecodeError:
        print(f"Error: {EXECUTION_QUEUE_FILE} is not valid JSON.")
        return 0

    for task in tasks:
        is_stale = task.get('stale_execution_detected', False)
        stale_detected_at_str = task.get('stale_execution_detected_at')

        if is_stale and stale_detected_at_str:
            try:
                # Parse with timezone info if present, otherwise assume UTC
                if stale_detected_at_str.endswith('Z'):
                    stale_detected_at = datetime.datetime.fromisoformat(stale_detected_at_str.replace('Z', '+00:00'))
                else:
                    stale_detected_at = datetime.datetime.fromisoformat(stale_detected_at_str)
                
                # Ensure both datetimes are timezone-aware for comparison
                if stale_detected_at.tzinfo is None:
                    stale_detected_at = stale_detected_at.replace(tzinfo=datetime.timezone.utc)

                if (now - stale_detected_at) > datetime.timedelta(hours=24):
                    # Archive the task
                    task_id_raw = task.get('dedupe_key', f"task_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}")
                    # Generate a short hash for the filename
                    task_hash = hashlib.sha256(task_id_raw.encode('utf-8')).hexdigest()[:16] # Use first 16 chars of hash
                    archive_filename = os.path.join(ARCHIVE_DIR, f"{task_hash}.json")
                    
                    with open(archive_filename, 'w') as af:
                        json.dump(task, af, indent=2)
                    
                    stale_count += 1
                    archived_task_ids.append(task_id_raw) # Keep original task_id for logging if needed
                    print(f\"Archived stale task (hash: {task_hash})\")
                else:
                    updated_tasks.append(task)
            except ValueError as e:
                print(f"Warning: Could not parse stale_execution_detected_at '{stale_detected_at_str}' for task. Error: {e}")
                updated_tasks.append(task) # Keep task if date parsing fails
        else:
            updated_tasks.append(task)

    # Write back the updated execution queue
    queue_data['tasks'] = updated_tasks
    with open(EXECUTION_QUEUE_FILE, 'w') as f:
        json.dump(queue_data, f, indent=2)
    
    print(f"Cleaned up {stale_count} stale tasks from {EXECUTION_QUEUE_FILE}.")
    return stale_count

def update_current_health(count):
    health_data = {}
    if os.path.exists(CURRENT_HEALTH_FILE):
        with open(CURRENT_HEALTH_FILE, 'r') as f:
            try:
                health_data = json.load(f)
            except json.JSONDecodeError:
                print(f"Warning: {CURRENT_HEALTH_FILE} is not valid JSON. Overwriting.")
                health_data = {}
    
    health_data['last_subagent_cleanup_count'] = count
    health_data['last_subagent_cleanup_timestamp'] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with open(CURRENT_HEALTH_FILE, 'w') as f:
        json.dump(health_data, f, indent=2)
    print(f"Updated {CURRENT_HEALTH_FILE} with cleanup count: {count}")

if __name__ == "__main__":
    count = cleanup_execution_queue()
    update_current_health(count)
