from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from nanobot.runtime.coordinator import run_self_evolving_cycle

DEFAULT_RUNTIME_STATE_SOURCE = "host_control_plane"
DEFAULT_RUNTIME_STATE_ROOT = Path("/var/lib/eeepc-agent/self-evolving-agent/state")
DEFAULT_WORKSPACE = Path.cwd()
DEFAULT_TASKS = "Run one bounded self-evolving cycle and persist canonical runtime state."


async def _execute_turn(tasks: str) -> str:
    return tasks


def _prime_runtime_defaults() -> None:
    os.environ.setdefault("NANOBOT_RUNTIME_STATE_SOURCE", DEFAULT_RUNTIME_STATE_SOURCE)
    os.environ.setdefault("NANOBOT_RUNTIME_STATE_ROOT", str(DEFAULT_RUNTIME_STATE_ROOT))


def _write_strong_reflection_artifact(*, state_root: Path, workspace: Path, summary: str) -> Path:
    """Persist durable strong-reflection evidence for dashboard and audits."""
    reflection_dir = state_root / "strong_reflection"
    reflection_dir.mkdir(parents=True, exist_ok=True)
    recorded_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": "strong-reflection-run-v1",
        "recorded_at_utc": recorded_at,
        "workspace": str(workspace),
        "summary": summary,
        "mode": "strong-reflection",
    }
    latest = reflection_dir / "latest.json"
    latest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    history = reflection_dir / f"reflection-{recorded_at.replace(':', '').replace('+', 'Z')}.json"
    history.write_text(json.dumps({**payload, "latest_path": str(latest)}, indent=2, ensure_ascii=False), encoding="utf-8")
    return latest


def main() -> int:
    previous_source = os.environ.get("NANOBOT_RUNTIME_STATE_SOURCE")
    previous_root = os.environ.get("NANOBOT_RUNTIME_STATE_ROOT")
    try:
        _prime_runtime_defaults()
        workspace_value = os.getenv("NANOBOT_WORKSPACE") or os.getenv("NANOBOT_AGENT_WORKSPACE")
        workspace = Path(workspace_value).expanduser() if workspace_value else DEFAULT_WORKSPACE
        tasks = os.getenv("NANOBOT_SELF_EVOLVING_TASKS", DEFAULT_TASKS)
        summary = asyncio.run(
            run_self_evolving_cycle(
                workspace=workspace,
                tasks=tasks,
                execute_turn=_execute_turn,
            )
        )
        if any(arg == "strong-reflection" for arg in sys.argv[1:]):
            state_root = Path(os.environ.get("NANOBOT_RUNTIME_STATE_ROOT", str(workspace / "state"))).expanduser()
            artifact_path = _write_strong_reflection_artifact(state_root=state_root, workspace=workspace, summary=summary)
            print(f"Strong reflection artifact persisted: {artifact_path}")
        print(summary)
        return 0
    finally:
        if previous_source is None:
            os.environ.pop("NANOBOT_RUNTIME_STATE_SOURCE", None)
        else:
            os.environ["NANOBOT_RUNTIME_STATE_SOURCE"] = previous_source
        if previous_root is None:
            os.environ.pop("NANOBOT_RUNTIME_STATE_ROOT", None)
        else:
            os.environ["NANOBOT_RUNTIME_STATE_ROOT"] = previous_root


if __name__ == "__main__":
    raise SystemExit(main())
