#!/usr/bin/env python3
import os
import json
from pathlib import Path
from datetime import datetime, timezone

from nanobot.runtime.state import resolve_runtime_state_root
from nanobot.runtime.subagent_archive import archive_stale_requests

def main():
    # 1. Resolve workspace and state root paths dynamically
    workspace_env = os.environ.get('NANOBOT_WORKSPACE')
    if workspace_env:
        workspace = Path(workspace_env).resolve()
    else:
        # Check standard runtime fallback
        fallback = Path('/opt/eeepc-agent/runtimes/self-evolving-agent/current')
        if fallback.exists():
            workspace = fallback
        else:
            workspace = Path.cwd().resolve()

    state_root = resolve_runtime_state_root(workspace)
    print(f"Resolved workspace: {workspace}")
    print(f"Resolved state root: {state_root}")

    # 2. Archive requests older than 4 hours (14400 seconds)
    print("Starting subagent request archiving...")
    summary = archive_stale_requests(
        workspace=workspace,
        state_root=state_root,
        cutoff_seconds=14400,
        now=datetime.now(timezone.utc)
    )
    archived_count = summary.get("archived_count", 0)
    print(f"Finished. Archived {archived_count} stale requests.")

    # 3. Update state/current_health.json
    health_file = state_root / "current_health.json"
    health_data = {}
    if health_file.exists():
        try:
            health_data = json.loads(health_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Warning: Could not decode {health_file}: {e}. Starting fresh.")
    
    health_data["subagent_cleanup_count"] = health_data.get("subagent_cleanup_count", 0) + archived_count
    health_data["last_subagent_cleanup_timestamp"] = datetime.now(timezone.utc).isoformat()

    health_file.parent.mkdir(parents=True, exist_ok=True)
    health_file.write_text(json.dumps(health_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Updated {health_file} with subagent_cleanup_count: {health_data['subagent_cleanup_count']}")

if __name__ == "__main__":
    main()
