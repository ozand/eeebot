import os
import json
import datetime

SUBAGENT_REQUESTS_DIR = "/var/lib/eeepc-agent/self-evolving-agent/state/subagents/requests/"
ARCHIVE_DIR = "/var/lib/eeepc-agent/self-evolving-agent/state/subagents/archive/"
CURRENT_HEALTH_FILE = "/var/lib/eeepc-agent/self-evolving-agent/state/current_health.json"

def archive_stale_requests():
    archived_count = 0
    now = datetime.datetime.now(datetime.timezone.utc)

    if not os.path.exists(ARCHIVE_DIR):
        os.makedirs(ARCHIVE_DIR)

    for filename in os.listdir(SUBAGENT_REQUESTS_DIR):
        if filename.startswith("request-") and filename.endswith(".json"):
            filepath = os.path.join(SUBAGENT_REQUESTS_DIR, filename)
            try:
                with open(filepath, 'r') as f:
                    request_data = json.load(f)
                
                # Assuming 'timestamp' is in ISO format (e.g., "2026-06-10T05:00:00Z")
                # Adjust this if the timestamp format is different
                timestamp_str = request_data.get("timestamp")
                if timestamp_str:
                    request_time = datetime.datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    
                    if (now - request_time).total_seconds() > 24 * 3600: # 24 hours
                        os.rename(filepath, os.path.join(ARCHIVE_DIR, filename))
                        archived_count += 1
                        print(f"Archived: {filename}")
                else:
                    print(f"Warning: No timestamp found in {filename}. Skipping.")

            except json.JSONDecodeError:
                print(f"Error decoding JSON from {filename}. Skipping.")
            except Exception as e:
                print(f"An error occurred processing {filename}: {e}")

    update_current_health(archived_count)
    return archived_count

def update_current_health(count):
    health_data = {}
    if os.path.exists(CURRENT_HEALTH_FILE):
        try:
            with open(CURRENT_HEALTH_FILE, 'r') as f:
                health_data = json.load(f)
        except json.JSONDecodeError:
            print(f"Warning: Could not decode {CURRENT_HEALTH_FILE}. Starting fresh.")
    
    health_data["subagent_cleanup_count"] = health_data.get("subagent_cleanup_count", 0) + count
    health_data["last_subagent_cleanup_timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with open(CURRENT_HEALTH_FILE, 'w') as f:
        json.dump(health_data, f, indent=2)
    print(f"Updated {CURRENT_HEALTH_FILE} with subagent_cleanup_count: {health_data['subagent_cleanup_count']}")

if __name__ == "__main__":
    print("Starting subagent request archiving...")
    archived = archive_stale_requests()
    print(f"Finished. Archived {archived} stale requests.")
